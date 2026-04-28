#!/usr/bin/env python3
"""
Scraper Agent
--------------
Generic scraper that polls one or more Sources (see sources/), detects newly completed events since the last check, and emails a summary when new results exist.
All URL-specific logic lives in source plugins under providers/.
"""

import sys
if sys.version_info < (3, 8):
    sys.exit("Python 3.8+ is required. Current version: " + sys.version)

import json
import logging
import os
import smtplib
import subprocess
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import re

import requests
from dotenv import load_dotenv

from providers import DEFAULT_PROVIDER_KEY, PROVIDERS
from providers.base import Provider

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log_level_name = (os.getenv("LOG_LEVEL", "INFO") or "INFO").strip()
_log_level_name = _log_level_name.split("#", 1)[0].strip().upper()
LOG_LEVEL = getattr(logging, _log_level_name, logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _getenv_int(name: str, default: int) -> int:
    """Parse an integer environment variable, tolerating inline `#` comments.

    Returns `default` if the variable is unset/empty or cannot be parsed.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = str(raw).split("#", 1)[0].strip()
    if not cleaned:
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


def _getenv_provider_scoped_int(name: str, provider_key: str, default: int) -> int:
    """Parse a provider-scoped integer env var, tolerating inline `#` comments."""
    raw = _getenv_provider_scoped(name, provider_key)
    cleaned = str(raw).split("#", 1)[0].strip()
    if not cleaned:
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


STATE_FILE = Path(os.getenv("STATE_FILE") or "state.json")
EMAIL_TO: list[str] = []
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _getenv_int("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
CHECK_INTERVAL = _getenv_int("CHECK_INTERVAL", 300)  # seconds between checks
DRY_RUN = False  # Set via --dry-run CLI flag; skips actual email sending


def _get_provider(provider_key: str) -> Provider:
    """Return the Provider instance for the given provider key."""
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        raise KeyError(f"Unknown provider '{provider_key}'. Available: {', '.join(PROVIDERS)}")
    return provider


def _provider_env_key(provider_key: str) -> str:
    """Return an env-var-safe suffix for provider-scoped configuration."""
    # Convert provider keys like "espn-nba" into a safe env-var suffix: "ESPN_NBA".
    return re.sub(r"[^a-zA-Z0-9]+", "_", provider_key.strip()).strip("_").upper()


def _getenv_provider_scoped(name: str, provider_key: str) -> str:
    """Read an environment variable, optionally overridden per provider."""
    # Resolution order:
    # - <NAME>__<PROVIDER_ENV_KEY> (e.g. EMAIL_TO__ESPN_NBA)
    # - <NAME> (global default)
    scoped = f"{name}__{_provider_env_key(provider_key)}"
    return (os.getenv(scoped) or os.getenv(name) or "").strip()


# -------------------------------------------------------------------------------------------------------------------------------
# State helpers – Persist which events have already been reported as completed between runs, using a local JSON file.
# This prevents duplicate emails: an event is only notified about once, the first time it appears as completed.
#
# State is stored per-provider in a dedicated file (e.g. state.ynet-sport.json). 
# -------------------------------------------------------------------------------------------------------------------------------
def _state_file_for_provider(provider_key: str) -> Path:
    """Return the per-provider state file path derived from STATE_FILE."""
    suffix = re.sub(r"[^a-zA-Z0-9._-]+", "_", provider_key.strip())
    return STATE_FILE.with_name(f"{STATE_FILE.stem}.{suffix}{STATE_FILE.suffix}")


def load_state(provider_key: str) -> dict:
    """Return the persisted state dict for a single provider."""
    state_file = _state_file_for_provider(provider_key)
    if state_file.exists():
        with open(state_file, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict, provider_key: str) -> None:
    """Persist the given provider state dict to disk."""
    state_file = _state_file_for_provider(provider_key)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def provider_state(state: dict, provider: Provider) -> dict:
    """Return (or create) the provider's state dict for the active provider."""

    # ---- Ensure required keys exist ----
    rejected_key = provider.rejected_ids_state_key()
    if rejected_key not in state:
        state[rejected_key] = []

    notified_key = provider.notified_ids_state_key()
    if notified_key not in state:
        state[notified_key] = {}

    if "last_check" not in state:
        state["last_check"] = None

    return state


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
def _keep_unrejected_items(items: list[dict], previous_rejected_ids: set[str]) -> list[dict]:
    """Filter out items that were previously rejected."""
    return [
        it
        for it in items
        if str(it.get("id")) not in previous_rejected_ids
    ]


def _keep_unnotified_items(items: list[dict], provider: Provider) -> list[dict]:
    """Filter out items that were already notified (emailed) in previous runs."""
    provider_state_data = provider.provider_state or {}
    notified = provider_state_data.get(provider.notified_ids_state_key(), {})

    if isinstance(notified, dict):
        notified_ids = {str(x) for day_ids in notified.values() for x in (day_ids or [])}
    elif isinstance(notified, list):
        notified_ids = {str(x) for x in notified}
    else:
        notified_ids = set()

    return [it for it in items if str(it.get("id")) not in notified_ids]


def _published_dt_for_cutoff(item: dict) -> datetime | None:
    """Extract a best-effort published datetime for cutoff comparisons.

    Time zone semantics:
    - If the source timestamp is tz-aware (e.g. ends with `Z`), it is normalized to **UTC** and returned as a **naive UTC** `datetime`.
    - If the source timestamp is tz-naive, it is assumed to be in the machine's **local time zone**, then converted to **naive UTC**.

    Returns None when no timestamp is present or parsing fails.
    """
    raw = (
        item.get("published_at")
        or item.get("date")
        or item.get("published")
        or item.get("publishedAt")
        or ""
    )
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    local_tz = datetime.now().astimezone().tzinfo
    if dt.tzinfo is None and local_tz is not None:
        dt = dt.replace(tzinfo=local_tz)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _sort_completed_items_newest_first(current_completed_items: list[dict]) -> None:
    """Sort completed items newest-first by their best-effort published datetime (items without a timestamp sort last)."""
    current_completed_items.sort(key=_published_dt_for_sort, reverse=True)


def _published_dt_for_sort(item: dict) -> datetime:
    """Best-effort published datetime extractor for cross-provider sorting.

    Supported timestamp fields (first one found wins):
    - published_at (Ynet HTML providers)
    - date (ESPN)
    - published / publishedAt (common API variants)

    Time zone semantics:
    - If the parsed value is tz-aware, it is normalized to **UTC** and returned as a **naive UTC** `datetime`.
    - If the parsed value is tz-naive, it is returned **as-is** (tz-naive). This preserves the source representation,
      which may be local time depending on the provider.

    Items without a usable timestamp are sorted last (`datetime.min`).
    """
    raw = (
        item.get("published_at")
        or item.get("date")
        or item.get("published")
        or item.get("publishedAt")
        or ""
    )
    if not raw:
        return datetime.min
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except Exception:
        return datetime.min
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _keep_only_completed_items(provider: Provider, items: list[dict]) -> list[dict]:
    """Filter items down to only those considered completed by the provider."""
    current_completed_ids = provider.get_only_completed_ids(items)
    return [item for item in items if item.get("id") in current_completed_ids]


def _keep_completed_items_published_after_last_check(provider: Provider, current_completed_items: list[dict]) -> list[dict]:
    """Keep completed items published after the provider's `cutoff_dt()`.

    Time zone semantics:
    - `_published_dt_for_cutoff()` returns **naive UTC**.
    - `provider.cutoff_dt()` is provider-defined; by convention in this codebase it is typically **naive UTC** when
      derived from `last_check`, but may be tz-naive local time when derived from a rolling `days_back` window.

    This function compares the two naive datetimes exactly as provided.
    """

    cutoff_dt = provider.cutoff_dt()
    if cutoff_dt is None:
        return current_completed_items

    logger.debug(
        "[%s] Completed candidates: %d (before cutoff filtering: cutoff_dt=%s (naive UTC))",
        provider.name,
        len(current_completed_items),
        cutoff_dt.isoformat(timespec="seconds"),
    )

    filtered: list[dict] = []
    for it in current_completed_items:
        published_dt = _published_dt_for_cutoff(it)
        if published_dt is None:
            logger.debug(
                "[%s] Notify candidate (no timestamp): id=%s",
                provider.name,
                str(it.get("id")),
            )
            filtered.append(it)
            continue

        if published_dt > cutoff_dt:
            logger.debug(
                "[%s] Notify candidate (newer than cutoff_dt): id=%s published_dt=%s cutoff_dt=%s",
                provider.name,
                str(it.get("id")),
                published_dt.isoformat(timespec="seconds"),
                cutoff_dt.isoformat(timespec="seconds"),
            )
            filtered.append(it)
        else:
            logger.debug(
                "[%s] Skip notify (not newer than cutoff_dt): id=%s published_dt=%s cutoff_dt=%s",
                provider.name,
                str(it.get("id")),
                published_dt.isoformat(timespec="seconds"),
                cutoff_dt.isoformat(timespec="seconds"),
            )

    logger.debug(
        "[%s] Newly-notifiable completed items: %d",
        provider.name,
        len(filtered),
    )
    return filtered


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(subject: str, html_body: str, plain_body: str) -> bool:
    """Send an email via SMTP (TLS). Skipped when DRY_RUN is True."""
    if DRY_RUN:
        logger.info("[DRY-RUN] Email would be sent \u2013 skipping actual send.\nSubject: %s\n%s", subject, plain_body)
        return True

    if not EMAIL_TO:
        logger.warning("EMAIL_TO not configured \u2013 skipping email.")
        logger.info("Set EMAIL_TO in .env to enable email.")
        return False

    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP credentials not configured \u2013 skipping email.")
        logger.info("Set SMTP_USER and SMTP_PASS in .env to enable email.")
        return False

    msg = MIMEMultipart("alternative")  # plain text + HTML; email client picks the best it can render
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(EMAIL_TO)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
    logger.info("Email sent to %s", ", ".join(EMAIL_TO))
    return True


# ---------------------------------------------------------------------------
# Agent loop – Generic: works with any Provider implementation.
# ---------------------------------------------------------------------------
def check_once(provider_key: str) -> None:
    """Run a single scrape-check-email cycle for the given provider."""
    provider = _get_provider(provider_key)
    state = load_state(provider_key)
    provider_state_data = provider_state(state, provider)
    provider.attach_state(provider_state_data)

    now_utc = datetime.now(timezone.utc)
    if now_utc.hour == 23:
        provider.prune_notified_ids_two_days_ago(now_utc)

    rejected_key = provider.rejected_ids_state_key()
    previous_rejected_ids: set[str] = {x for x in provider_state_data.get(rejected_key, [])}

    logger.debug(
        "[%s] State: last_check(raw)=%r last_check_dt(parsed)=%s rejected_ids=%d",
        provider.name,
        provider_state_data.get("last_check"),
        provider.last_check_dt.isoformat(timespec="seconds") if provider.last_check_dt else None,
        len(previous_rejected_ids),
    )

    data = provider.fetch()
    items = provider.parse(data)
    logger.debug("[%s] Parsed %d item(s)", provider.name, len(items))

    items = _keep_unrejected_items(items, previous_rejected_ids)
    items = _keep_unnotified_items(items, provider)
    items, rejected_ids_to_save = provider.reject_items(items)
    if rejected_ids_to_save:
        provider_state_data[rejected_key] = list(previous_rejected_ids | rejected_ids_to_save)
        previous_rejected_ids = set(provider_state_data.get(rejected_key, []))
        save_state(state, provider_key)

    current_completed_items = _keep_only_completed_items(provider, items)
    current_completed_items = _keep_completed_items_published_after_last_check(provider, current_completed_items)
    _sort_completed_items_newest_first(current_completed_items)
    current_completed_items = provider.enrich_completed_items(current_completed_items)

    day_key = datetime.now(timezone.utc).date().isoformat()
    notifiable_items = [
        it for it in current_completed_items
        if provider.should_record_notifiable_id(it, day_key)
    ]

    if not notifiable_items:
        provider_state_data["last_check"] = datetime.now(timezone.utc).isoformat()
        save_state(state, provider_key)
        logger.debug("No newly-notifiable current_completed_items. Returning.")
        return

    logger.info("[%s] %d item(s) newly notifiable \u2013 sending email \u2026", provider.name, len(notifiable_items))

    day_label = provider.get_day_label(data)
    
    first_run = provider.last_check_dt is None
    if not first_run:
        logger.info("%s", provider.items_to_plain_table(notifiable_items, provider.heading(day_label)))

    subject = f"{provider.name} Update \u2013 {day_label}"
    html_body = (
        f"<h2>{provider.heading(day_label)}</h2>"
        + provider.items_to_html_table(notifiable_items)
        + "<br><p style='color:gray;font-size:12px;'>Sent by Scraper Agent</p>"
    )
    plain_body = provider.items_to_plain_table(notifiable_items, provider.heading(day_label))
    did_send = send_email(subject, html_body, plain_body)
    if did_send:
        provider.record_notifiable_ids(notifiable_items, day_key)

    provider_state_data["last_check"] = datetime.now(timezone.utc).isoformat()
    save_state(state, provider_key)


def run_loop(provider_key: str, interval_secs: int) -> None:
    """Continuously poll the given provider at interval_secs seconds."""
    provider = _get_provider(provider_key)
    logger.info("Scraper Agent started (provider=%s, interval=%ds, recipient=%s)",
                provider.name, interval_secs, EMAIL_TO)
    while True:
        try:
            check_once(provider_key)
        except requests.RequestException as exc:
            logger.error("[%s] Network error: %s", provider.name, exc)
        except Exception as exc:
            logger.error("[%s] Unexpected error: %s", provider.name, exc)
        now = time.time()
        sleep_secs = interval_secs - (now % interval_secs)
        logger.debug("Sleeping %.0fs until next check boundary", sleep_secs)
        time.sleep(sleep_secs)


def _spawn_provider_process(provider_key: str, mode: str) -> subprocess.Popen:
    """Spawn a new scraper subprocess for a single provider."""
    script_path = str(Path(__file__).resolve())
    cmd = [sys.executable, script_path, mode, "--provider", provider_key]

    env = os.environ.copy()
    env["STATE_FILE"] = env.get("STATE_FILE") or str(STATE_FILE)
    return subprocess.Popen(cmd, env=env)


def run_all_isolated(mode: str) -> int:
    """Run each provider in a dedicated subprocess and return an exit code."""
    provider_keys = list(PROVIDERS.keys())
    logger.info("Starting %d isolated provider process(es): %s", len(provider_keys), ", ".join(provider_keys))

    procs: list[tuple[str, subprocess.Popen]] = []
    for key in provider_keys:
        procs.append((key, _spawn_provider_process(key, mode=mode)))

    try:
        while True:
            time.sleep(1)
            for key, p in procs:
                rc = p.poll()
                if rc is not None:
                    logger.error("Provider '%s' exited unexpectedly with code %s", key, rc)
                    return rc
    except KeyboardInterrupt:
        logger.info("Stopping all provider processes ...")
        for _, p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for _, p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                pass
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--dry-run" in args:
        DRY_RUN = True
        args.remove("--dry-run")
        logger.info("[DRY-RUN] Email sending disabled.")

    run_all = False
    if "--all" in args:
        run_all = True
        args.remove("--all")

    # --provider <key>  (default: espn-nba)
    provider_key = DEFAULT_PROVIDER_KEY
    if "--provider" in args:
        idx = args.index("--provider")
        provider_key = args[idx + 1]
        del args[idx:idx + 2]

    if provider_key == "all":
        run_all = True

    mode = args[0] if args else "loop"

    if mode not in {"once", "loop"}:
        logger.error("Usage: %s [once|loop] [--dry-run] [--provider <key>|all] [--all]", sys.argv[0])
        logger.error("Available providers: %s", ', '.join(PROVIDERS))
        sys.exit(1)

    if run_all:
        if mode == "once" or DRY_RUN:
            if mode == "once":
                logger.error("'once' mode is intended for per-provider testing. Use: %s once --provider <key>", sys.argv[0])
            else:
                logger.error("'--dry-run' is intended for per-provider testing. Use: %s %s --provider <key> --dry-run", sys.argv[0], mode)
            sys.exit(1)
        sys.exit(run_all_isolated(mode=mode))

    if provider_key not in PROVIDERS:
        logger.error("Unknown provider '%s'. Available: %s", provider_key, ', '.join(PROVIDERS))
        sys.exit(1)

    # Resolve recipient list after provider selection (supports provider-scoped overrides).
    EMAIL_TO = [
        addr.strip()
        for addr in _getenv_provider_scoped("EMAIL_TO", provider_key).split(",")
        if addr.strip()
    ]

    interval_secs = _getenv_provider_scoped_int("CHECK_INTERVAL", provider_key, CHECK_INTERVAL)

    if mode == "once":
        check_once(provider_key)
    else:
        run_loop(provider_key, interval_secs=interval_secs)
