"""Email-driven URL summarization provider.

This provider polls an IMAP inbox for new messages from a designated sender,
extracts URLs from the email body, fetches each URL, extracts readable text,
optionally summarizes it via OpenAI, and returns items formatted for the
generic Scraper agent loop.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from datetime import datetime, timezone
from email.header import decode_header
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .base import Provider


logger = logging.getLogger(__name__)


class EmailUrlSummary(Provider):
    """Provider that turns incoming emails into a batch of URL summaries.

    Environment variables:
        - IMAP_HOST: IMAP server hostname.
        - IMAP_PORT: IMAP server port (default: 993).
        - IMAP_USER: IMAP username/email.
        - IMAP_PASS: IMAP password/app password.
        - IMAP_FOLDER: mailbox folder (default: INBOX).
        - EMAIL_POLL_FROM: only process emails from this address (optional).
        - EMAIL_POLL_MARK_SEEN: when true, mark processed emails as seen (default: true).
        - EMAIL_POLL_MAX_EMAILS: max number of emails to process per fetch (default: 10).
        - EMAIL_URL_IGNORE_DOMAINS: comma-delimited host prefixes/domains to ignore when extracting URLs.

    Notes:
        - Items are created per-URL. De-duplication is handled by the agent loop via item IDs.
        - OpenAI summarization is enabled when OPENAI_API_KEY is present.
    """

    @property
    def name(self) -> str:
        return "Email URL Summary"

    @property
    def state_key(self) -> str:
        return "email-url-summary"

    @property
    def url(self) -> str:
        return "imap://inbox"

    def fetch(self) -> dict:
        """Poll IMAP for new (unseen) emails and return parsed message payloads."""
        host = (os.getenv("IMAP_HOST") or "").strip()
        user = (os.getenv("IMAP_USER") or "").strip()
        password = (os.getenv("IMAP_PASS") or "").strip()
        folder = (os.getenv("IMAP_FOLDER") or "INBOX").strip()
        port = int((os.getenv("IMAP_PORT") or "993").strip() or 993)

        if not host or not user or not password:
            logger.warning("[%s] IMAP not configured (IMAP_HOST/IMAP_USER/IMAP_PASS) - skipping fetch.", self.name)
            return {"messages": [], "fetched_at": datetime.now(timezone.utc).isoformat()}

        from_filter = (os.getenv("EMAIL_POLL_FROM") or "").split("#", 1)[0].strip()
        mark_seen_raw = (os.getenv("EMAIL_POLL_MARK_SEEN") or "true").strip().lower()
        mark_seen = mark_seen_raw not in {"0", "false", "no"}
        max_emails = int((os.getenv("EMAIL_POLL_MAX_EMAILS") or "10").strip() or 10)

        messages: list[dict] = []
        ignored_email_uids = self._ignored_email_uids()

        imap = imaplib.IMAP4_SSL(host=host, port=port)
        try:
            imap.login(user, password)
            imap.select(folder)

            criteria = ["UNSEEN"]
            if from_filter:
                criteria.extend(["FROM", f'"{from_filter}"'])

            typ, data = imap.search(None, *criteria)
            if typ != "OK":
                logger.warning("[%s] IMAP search failed: %s", self.name, typ)
                return {"messages": [], "fetched_at": datetime.now(timezone.utc).isoformat()}

            uids = (data[0] or b"").split()
            if not uids:
                return {"messages": [], "fetched_at": datetime.now(timezone.utc).isoformat()}

            # Process oldest-first for stability.
            for uid in uids[:max_emails]:
                uid_str = uid.decode(errors="ignore") if isinstance(uid, (bytes, bytearray)) else str(uid)
                if uid_str in ignored_email_uids:
                    continue

                typ, msg_data = imap.fetch(uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_bytes = msg_data[0][1]
                msg = email.message_from_bytes(raw_bytes)
                subject = self._decode_mime_header(msg.get("Subject", ""))
                from_addr = self._extract_addr(msg.get("From", ""))
                body_text = self._extract_best_effort_body_text(msg)
                urls, has_ignored_urls = self._extract_urls(body_text)

                if has_ignored_urls:
                    self._remember_ignored_email_uid(uid_str)
                    logger.info("[%s] Leaving email unread because it contains URL(s) ignored by EMAIL_URL_IGNORE_DOMAINS: uid=%s", self.name, uid_str)
                    continue

                if not urls:
                    if mark_seen:
                        try:
                            imap.store(uid, "+FLAGS", "\\Seen")
                        except Exception:
                            pass
                    continue

                messages.append(
                    {
                        "uid": uid.decode(errors="ignore") if isinstance(uid, (bytes, bytearray)) else str(uid),
                        "subject": subject,
                        "from": from_addr,
                        "body": body_text,
                        "urls": urls,
                    }
                )

                if mark_seen:
                    try:
                        imap.store(uid, "+FLAGS", "\\Seen")
                    except Exception:
                        pass

            return {"messages": messages, "fetched_at": datetime.now(timezone.utc).isoformat()}
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def parse(self, data: dict) -> list[dict]:
        """Convert fetched email messages into item dicts (one item per URL)."""
        messages = data.get("messages") or []
        items: list[dict] = []
        for m in messages:
            uid = str(m.get("uid") or "")
            subject = str(m.get("subject") or "")
            from_addr = str(m.get("from") or "")
            urls = m.get("urls") or []

            for u in urls:
                url = str(u).strip()
                if not url:
                    continue

                title = subject or self._url_host_label(url)
                item_id = f"{uid}:{url}" if uid else url

                items.append(
                    {
                        "id": item_id,
                        "title": title,
                        "url": url,
                        "email_subject": subject,
                        "email_from": from_addr,
                        "email_uid": uid,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

        return items

    def openai_summary_instruction(self) -> str:
        """Instruction describing the desired summary style/language."""
        return "Write a concise 8-10 sentence summary in Hebrew."

    def is_rtl(self) -> bool:
        """Ynet content is Hebrew; render human-facing output as RTL by default."""
        return True

    def get_day_label(self, data: dict) -> str:
        """Display label for the current batch."""
        return datetime.now().strftime("%Y-%m-%d")

    def get_only_completed_ids(self, items: list[dict]) -> set[str]:
        """All parsed URL items are considered immediately complete."""
        return {str(i.get("id")) for i in items if i.get("id")}

    def enrich_completed_items(self, items: list[dict]) -> list[dict]:
        """Fetch each URL, extract text, and add a summary (OpenAI when configured)."""
        enriched: list[dict] = []
        for it in items:
            url = str(it.get("url") or "").strip()
            title = str(it.get("title") or "").strip()
            if not url:
                continue

            try:
                html = self._fetch_url_html(url)
                text = self._html_to_text(html)
            except Exception as exc:
                logger.warning("[%s] Failed fetching URL: %s (%s)", self.name, url, exc.__class__.__name__)
                text = ""

            it["text_len"] = len(text or "")

            summary = ""
            if text and self._openai_api_key():
                try:
                    analysis = self._openai_analyze_article(title=title or url, url=url, text=text)
                    summary = str((analysis or {}).get("summary") or "").strip()
                except Exception as exc:
                    logger.warning("[%s] OpenAI summary failed: %s (%s)", self.name, url, exc.__class__.__name__)
                    summary = ""

            if not summary and text:
                summary = self._fallback_summary(text)

            if summary:
                it["summary"] = summary

            enriched.append(it)

        return enriched

    def item_to_text(self, item: dict) -> str:
        """Render a single URL item as console-friendly text."""
        title = str(item.get("title") or "")
        url = str(item.get("url") or "")
        from_addr = str(item.get("email_from") or "")
        subject = str(item.get("email_subject") or "")
        summary = str(item.get("summary") or "")

        header = title
        if subject and subject != title:
            header = f"{title} (subject: {subject})"
        if from_addr:
            header = f"{header} [from: {from_addr}]"

        if summary:
            return f"{header}\n{url}\n\n{summary}".strip()
        return f"{header}\n{url}".strip()

    def items_to_html_table(self, items: list[dict]) -> str:
        """Render a compact HTML table with URL + summary."""
        rows: list[str] = []
        for it in items:
            title = self._html_escape(str(it.get("title") or ""))
            url = str(it.get("url") or "")
            summary = self._html_escape(str(it.get("summary") or ""))
            from_addr = self._html_escape(str(it.get("email_from") or ""))
            subject = self._html_escape(str(it.get("email_subject") or ""))

            meta = ""
            if from_addr or subject:
                meta_parts = []
                if from_addr:
                    meta_parts.append(f"from: {from_addr}")
                if subject:
                    meta_parts.append(f"subject: {subject}")
                meta = "<div style='margin-top:4px;color:#666;font-size:12px'>" + " | ".join(meta_parts) + "</div>"

            summary_html = f"<div style='margin-top:6px;color:#333;font-size:13px;line-height:1.35'>{summary}</div>" if summary else ""

            rows.append(
                "<tr>"
                "<td>"
                f"<a href='{url}'>{title or url}</a>"
                f"{meta}"
                f"{summary_html}"
                "</td>"
                "</tr>"
            )

        if self.is_rtl():
            return (
                "<table dir='rtl' border='1' cellpadding='6' cellspacing='0' "
                "style='border-collapse:collapse;direction:rtl;text-align:right'>"
                "<tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )

        return (
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    def _fetch_url_html(self, url: str) -> str:
        """Fetch a URL and return raw HTML/text content (best-effort)."""
        resp = requests.get(
            url,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                )
            },
        )
        resp.raise_for_status()
        return resp.text or ""

    def _fallback_summary(self, text: str) -> str:
        """Return a short best-effort summary when OpenAI is not configured."""
        cleaned = " ".join((text or "").split())
        if len(cleaned) <= 500:
            return cleaned
        return cleaned[:500].rstrip() + "..."

    def _extract_urls(self, text: str) -> tuple[list[str], bool]:
        """Extract URLs from arbitrary text and report whether any were ignored."""
        if not text:
            return [], False
        # Basic URL regex: good enough for email bodies.
        found = re.findall(r"https?://[^\s<>'\"]+", text)
        ignored_domains = self._ignored_url_domains()
        urls: list[str] = []
        seen: set[str] = set()
        has_ignored_urls = False
        for u in found:
            u = u.strip().rstrip(")].,;\"")
            if not u or u in seen:
                continue
            if self._is_ignored_url(u, ignored_domains):
                has_ignored_urls = True
                logger.info("[%s] Ignoring URL due to EMAIL_URL_IGNORE_DOMAINS: %s", self.name, u)
                continue
            seen.add(u)
            urls.append(u)
        return urls, has_ignored_urls

    def _extract_best_effort_body_text(self, msg: email.message.Message) -> str:
        """Extract a best-effort plain text body from an email message."""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                disp = (part.get("Content-Disposition") or "").lower()
                if "attachment" in disp:
                    continue
                if ctype == "text/plain":
                    return self._decode_part_payload(part)
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                disp = (part.get("Content-Disposition") or "").lower()
                if "attachment" in disp:
                    continue
                if ctype == "text/html":
                    html = self._decode_part_payload(part)
                    return self._html_to_text(html)
            return ""

        ctype = (msg.get_content_type() or "").lower()
        payload = self._decode_part_payload(msg)
        if ctype == "text/html":
            return self._html_to_text(payload)
        return payload

    def _decode_part_payload(self, part: email.message.Message) -> str:
        """Decode an email MIME part payload into a string."""
        try:
            raw = part.get_payload(decode=True)
        except Exception:
            raw = None
        if raw is None:
            return ""

        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except Exception:
            return raw.decode("utf-8", errors="replace")

    def _decode_mime_header(self, value: str) -> str:
        """Decode RFC2047 encoded headers into unicode."""
        if not value:
            return ""
        parts = decode_header(value)
        out = []
        for chunk, enc in parts:
            if isinstance(chunk, bytes):
                try:
                    out.append(chunk.decode(enc or "utf-8", errors="replace"))
                except Exception:
                    out.append(chunk.decode("utf-8", errors="replace"))
            else:
                out.append(str(chunk))
        return "".join(out).strip()

    def _extract_addr(self, from_header: str) -> str:
        """Extract email address from a From header."""
        if not from_header:
            return ""
        m = re.search(r"<([^>]+)>", from_header)
        if m:
            return m.group(1).strip()
        return from_header.strip()

    def _url_host_label(self, url: str) -> str:
        """Return a compact label for a URL host."""
        try:
            host = urlparse(url).netloc
            return host or url
        except Exception:
            return url

    def _ignored_url_domains(self) -> tuple[str, ...]:
        """Return ignored URL domain prefixes in a normalized form, e.g. ' LinkedIn, .GlassDoor ' -> ('linkedin', 'glassdoor')."""
        raw = (os.getenv("EMAIL_URL_IGNORE_DOMAINS") or "").split("#", 1)[0].strip()
        if not raw:
            return ()

        values: list[str] = []
        for part in raw.split(","):
            value = part.strip().lower().lstrip(".")
            if value:
                values.append(value)
        return tuple(values)

    def _is_ignored_url(self, url: str, ignored_domains: tuple[str, ...]) -> bool:
        """Return whether the URL host matches an ignored domain/prefix, e.g. 'www.linkedin.com' matches 'linkedin'."""
        if not ignored_domains:
            return False

        try:
            host = (urlparse(url).hostname or "").strip().lower()
        except Exception:
            return False

        if not host:
            return False

        normalized_host = host.removeprefix("www.")
        return any(normalized_host == domain or normalized_host.startswith(f"{domain}.") or normalized_host.startswith(domain) for domain in ignored_domains)

    def _ignored_email_uids_state_key(self) -> str:
        """Return the provider state key used to remember unread ignored email UIDs."""
        return "ignored_email_uids"

    def _ignored_email_uids(self) -> set[str]:
        """Return unread ignored email UIDs already remembered in provider state."""
        state = self.provider_state or {}
        values = state.get(self._ignored_email_uids_state_key()) or []
        return {str(value) for value in values if value}

    def _remember_ignored_email_uid(self, uid: str) -> None:
        """Persist an unread ignored email UID so future polls skip refetching it."""
        if not uid:
            return

        state = self.provider_state
        if not isinstance(state, dict):
            return

        key = self._ignored_email_uids_state_key()
        values = state.get(key) or []
        value_set = {str(value) for value in values if value}
        value_set.add(str(uid))
        state[key] = sorted(value_set)

    def _html_escape(self, s: str) -> str:
        """Escape text for safe HTML inclusion."""
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )
