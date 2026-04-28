"""HTML-based Ynet providers with optional OpenAI classification/summarization.
 
 This module defines a base Provider implementation that:
 - fetches a Ynet listing page (HTML)
 - extracts candidate article links
 - fetches relevant articles and uses OpenAI to summarize/classify
 - returns a list of item dicts suitable for the generic agent loop
 """
 
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .base import Provider


logger = logging.getLogger(__name__)


class YnetAiHtmlProviderBase(Provider, ABC):
    """Base class for Ynet HTML listing providers.
 
    Subclasses configure:
    - `url`: listing page URL
    - `allowed_path_prefixes`: which link paths from the listing page are candidates
 
    The base class implements scraping, best-effort publish-time extraction, 
    and an optional OpenAI step that can both summarize and classify content.
    """
 
    def __init__(self) -> None:
        """Initialize internal bookkeeping used for OpenAI model logging."""
        self._last_logged_openai_model: str | None = None

    @property
    @abstractmethod
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        """Allowed URL path prefixes for article links found on the listing page."""
        ...

    @property
    def max_listing_items(self) -> int:
        """Maximum number of candidate links to keep from the listing page."""
        return 40

    @property
    def max_unevaluated_to_process(self) -> int:
        """Maximum number of candidate items to fetch/classify per run."""
        return 25

    @property
    def max_kept_items(self) -> int:
        """Maximum number of non-rejected items to keep after evaluation."""
        return 40

    @property
    def min_title_len(self) -> int:
        """Minimum extracted link text length to consider it a valid candidate."""
        return 10

    @property
    def days_back(self) -> int:
        """Default rolling lookback window when `last_check` is not available."""
        return 1

    def is_rtl(self) -> bool:
        """Ynet content is Hebrew; render human-facing output as RTL by default."""
        return True

    def fetch(self) -> dict:
        """Fetch the listing page HTML.
 
        Returns a dict containing:
        - `html`: raw HTML
        - `fetched_at`: ISO timestamp (UTC)
        """
        resp = requests.get(
            self.url,
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
        return {"html": resp.text, "fetched_at": datetime.utcnow().isoformat()}

    def parse(self, data: dict) -> list[dict]:
        """Parse listing HTML into a list of candidate item dicts.
 
        Each item contains:
        - `id`: absolute URL (used for de-duplication)
        - `title`: link text
        - `url`: absolute URL
        """
        html = data.get("html") or ""
        soup = BeautifulSoup(html, "html.parser")

        items: list[dict] = []
        dedup_ids: set[str] = set()

        def normalize_href(href: str) -> str:
            """Convert an <a href> value to a canonical absolute URL (no fragment)."""
            abs_url = urljoin(self.url, href)
            parsed = urlparse(abs_url)
            return parsed._replace(fragment="").geturl()

        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("javascript:") or href.startswith("#"):
                continue

            abs_url = normalize_href(href)
            parsed = urlparse(abs_url)
            if parsed.netloc and "ynet.co.il" not in parsed.netloc:
                continue

            path = parsed.path or ""
            if not any(path.startswith(p) for p in self.allowed_path_prefixes):
                continue

            title = " ".join(a.get_text(" ", strip=True).split())
            if len(title) < self.min_title_len:
                continue

            item_id = abs_url
            if item_id in dedup_ids:
                continue
            dedup_ids.add(item_id)

            items.append({"id": item_id, "title": title, "url": abs_url})
            if len(items) >= self.max_listing_items:
                break

        return items

    def reject_items(self, items: list[dict]) -> tuple[list[dict], set[str]]:
        """Reject items that are too old or irrelevant; sets `published_at` (always) and `summary` (if not rejected).

        Rejections due to relevancy are persisted by the agent loop via `rejected_ids` so we don't fetch, summarize, or classify the same URL repeatedly.

        Time zone semantics:
        - `effective_cutoff_dt` comes from `Provider.cutoff_dt()`.
          - when derived from `last_check`, it is **naive UTC**.
          - when derived from `days_back`, it is **tz-naive local time**.
        - `published_at` is stored as a raw string extracted from the page.
        - For cutoff comparisons in this method, `published_at` is parsed and normalized to **naive UTC**:
          - tz-aware values are converted to UTC and made naive
          - tz-naive values are assumed to be local time and converted to naive UTC
        """
        items_to_keep: list[dict] = []
        rejected_ids_to_save: set[str] = set()

        effective_cutoff_dt = self.cutoff_dt()
        logger.debug(
            "[%s] reject_items: effective_cutoff_dt=%s (naive UTC)",
            self.name,
            effective_cutoff_dt.isoformat(timespec="seconds") if effective_cutoff_dt else None,
        )

        for it in items[: self.max_unevaluated_to_process]:
            url = str(it.get("url") or "")
            title = str(it.get("title") or "")
            item_id = str(it.get("id") or url)
            if not url or not item_id:
                continue

            try:
                logger.debug("[%s] Fetching article: %s", self.name, url)
                soup = self._fetch_article_soup(url)
                published_at = self._extract_published_at(soup)
                if published_at:
                    it["published_at"] = published_at
                    try:
                        dt_raw = str(published_at).strip().replace("Z", "+00:00")
                        published_dt = datetime.fromisoformat(dt_raw)
                        local_tz = datetime.now().astimezone().tzinfo
                        if published_dt.tzinfo is None and local_tz is not None:
                            published_dt = published_dt.replace(tzinfo=local_tz)
                        if published_dt.tzinfo is not None:
                            published_dt = published_dt.astimezone(timezone.utc).replace(tzinfo=None)

                        logger.debug(
                            "[%s] Parsed published_at: url=%s raw=%r normalized_utc=%s cutoff_utc=%s",
                            self.name,
                            url,
                            published_at,
                            published_dt.isoformat(timespec="seconds"),
                            effective_cutoff_dt.isoformat(timespec="seconds") if effective_cutoff_dt else None,
                        )
                        if effective_cutoff_dt is not None and published_dt < effective_cutoff_dt:
                            logger.debug(
                                "[%s] Filtered out (too old): %s published_at=%s cutoff=%s",
                                self.name,
                                url,
                                published_at,
                                effective_cutoff_dt.isoformat(timespec="seconds"),
                            )
                            continue
                    except Exception:
                        pass
                text = self._extract_article_text(soup)
            except Exception as exc:
                logger.warning("[%s] Failed to fetch article: %s (%s)", self.name, url, exc)
                text = ""

            analysis: dict = {}
            if self._openai_api_key() and text:
                try:
                    logger.debug("[%s] Summarizing/classifying via OpenAI: %s", self.name, url)
                    analysis = self._openai_analyze_article(title=title, url=url, text=text)
                except Exception as exc:
                    logger.warning(
                        "[%s] OpenAI analysis failed: %s (%s)",
                        self.name,
                        url,
                        exc.__class__.__name__,
                        exc_info=True,
                    )
                    analysis = {}

            if not self.is_relevant(title=title, url=url, text=text, analysis=analysis):
                logger.debug("[%s] Filtered out (irrelevant): %s", self.name, url)
                rejected_ids_to_save.add(item_id)
                continue

            summary = (analysis.get("summary") or "").strip() if analysis else ""
            if summary:
                it["summary"] = summary

            items_to_keep.append(it)
            logger.debug("[%s] Kept: %s", self.name, url)
            if len(items_to_keep) >= self.max_kept_items:
                break

        return items_to_keep, rejected_ids_to_save

    def is_relevant(self, title: str, url: str, text: str, analysis: dict) -> bool:
        """Provider-specific relevancy filter.
 
        Subclasses can use `analysis` (when OpenAI is enabled) and/or fallback to a keyword-based rule.
        """
        return True

    def get_day_label(self, data: dict) -> str:
        """Return a display day label for emails/logs.

        Time zone semantics:
        - Uses `datetime.now()` which reflects the machine's **local time zone**.
        """
        return datetime.now().strftime("%Y-%m-%d")

    def get_only_completed_ids(self, items: list[dict]) -> set[str]:
        """All parsed URL items are considered immediately complete."""
        return {str(i.get("id")) for i in items if i.get("id")}

    def item_to_text(self, item: dict) -> str:
        """Render a single item as console-friendly text (title/url + optional summary)."""
        title = item.get("title", "")
        url = item.get("url", "")
        summary = item.get("summary", "")
        published_at = self._format_published_at(str(item.get("published_at") or ""))

        header = title
        if published_at:
            header = f"[{published_at}] {title}"

        if summary:
            return f"{header}\n{url}\n\n{summary}".strip()
        return f"{header}\n{url}".strip()

    def items_to_html_table(self, items: list[dict]) -> str:
        """Render items as a simple HTML table suitable for an email body."""
        rows = []
        for it in items:
            title = (it.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
            summary = (it.get("summary") or "").replace("<", "&lt;").replace(">", "&gt;")
            published_at = self._format_published_at(str(it.get("published_at") or ""))
            url = it.get("url") or ""
            if summary:
                rows.append(
                    "<tr><td>"
                    f"<a href='{url}'>{title}</a>"
                    + (
                        f"<div style='margin-top:4px;color:#666;font-size:12px'>{published_at}</div>"
                        if published_at
                        else ""
                    )
                    + f"<div style='margin-top:6px;color:#333;font-size:13px;line-height:1.35'>{summary}</div>"
                    "</td></tr>"
                )
            else:
                rows.append(
                    "<tr><td>"
                    f"<a href='{url}'>{title}</a>"
                    + (
                        f"<div style='margin-top:4px;color:#666;font-size:12px'>{published_at}</div>"
                        if published_at
                        else ""
                    )
                    + "</td></tr>"
                )

        return (
            "<table dir='rtl' border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;direction:rtl;text-align:right'>"
            "<tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    def openai_system_prompt(self) -> str:
        """OpenAI system prompt used when summarizing/classifying articles."""
        return "Return ONLY valid JSON with keys: summary (string)."

    def openai_user_prompt_prefix(self) -> str:
        """Optional extra instruction prepended to the user prompt."""
        return ""

    def openai_summary_instruction(self) -> str:
        """Instruction describing the desired summary style/language."""
        return "Write a concise 8-12 sentence summary in Hebrew."

    def openai_article_text_label(self) -> str:
        """Label used for the article text section inside the user prompt."""
        return "Text"

    def openai_user_prompt(self, title: str, url: str, text: str) -> str:
        """Build the OpenAI user prompt for a given article."""
        prefix = (self.openai_user_prompt_prefix() or "").strip()
        if prefix:
            prefix = prefix + "\n\n"

        summary_instruction = (self.openai_summary_instruction() or "").strip()
        text_label = (self.openai_article_text_label() or "Text").strip()

        return (
            f"{prefix}{summary_instruction}\n\n"
            f"Title: {title}\n"
            f"URL: {url}\n"
            f"{text_label}: {text}"
        )

    def _fetch_article_soup(self, url: str) -> BeautifulSoup:
        """Fetch an article URL and return a BeautifulSoup document."""
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
        return BeautifulSoup(resp.text, "html.parser")

    def _extract_article_text(self, soup: BeautifulSoup) -> str:
        """Extract article text (best-effort) from a parsed HTML soup."""
        return self._html_to_text(str(soup))

    def _extract_published_at(self, soup: BeautifulSoup) -> str:
        """Extract a best-effort publish/modified timestamp string from an article page."""
        for script in soup.find_all("script"):
            txt = script.string or script.get_text(" ", strip=True)
            if not txt:
                continue
            m = re.search(
                r"['\"]dateModified['\"]\s*:\s*['\"]([^'\"]+)['\"]",
                txt,
            )
            if m:
                raw = m.group(1).strip()
                raw = raw.replace("/", "-")
                if "T" not in raw and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", raw):
                    raw = raw.replace(" ", "T", 1)
                return raw

        updated_meta_candidates = [
            ("property", "article:modified_time"),
            ("property", "og:updated_time"),
            ("name", "lastmod"),
            ("name", "last-modified"),
        ]

        published_meta_candidates = [
            ("property", "article:published_time"),
            ("property", "og:published_time"),
            ("name", "publish_date"),
            ("name", "pubdate"),
            ("name", "date"),
            ("name", "dc.date"),
            ("name", "DC.date.issued"),
        ]

        for attr, key in updated_meta_candidates:
            tag = soup.find("meta", attrs={attr: key})
            if tag and tag.get("content"):
                return str(tag.get("content") or "").strip()

        page_text = soup.get_text(" ", strip=True)
        m = re.search(r"עודכן\s*:??\s*(\d{1,2}:\d{2})", page_text)
        if m:
            hhmm = m.group(1)
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                return f"{today}T{hhmm}:00"
            except Exception:
                return hhmm

        for attr, key in published_meta_candidates:
            tag = soup.find("meta", attrs={attr: key})
            if tag and tag.get("content"):
                return str(tag.get("content") or "").strip()

        time_tag = soup.find("time")
        if time_tag is not None:
            dt = time_tag.get("datetime")
            if dt:
                return str(dt).strip()
            text = time_tag.get_text(" ", strip=True)
            if text:
                return " ".join(text.split())

        return ""

    def _format_published_at(self, published_at: str) -> str:
        """Format a raw timestamp string into a compact display string.

        Time zone semantics:
        - If `published_at` parses as tz-aware, it is converted to the machine's **local time zone** for display.
        - If it parses as tz-naive, it is treated as tz-naive and formatted **as-is** (no implicit UTC/local conversion).
        """
        if not published_at:
            return ""

        raw = published_at.strip()
        try:
            iso = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return raw
