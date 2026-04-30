# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup & Running

```bash
# One-time setup (WSL)
./setup.sh

# Run a single check for one provider
./run.sh once --provider espn-nba

# Run continuous loop for one provider
./run.sh loop --provider ynet-sport

# Run all providers in isolated subprocesses (production mode)
./run.sh loop --all

# Dry-run (no email sent) — per-provider only
./run.sh once --provider espn-nba --dry-run
```

Available provider keys: `espn-nba`, `ynet-sport`, `ynet-news`, `email-url-summary`

## Architecture

The agent polls one or more providers on a configurable interval, detects newly-completed items since the last check, and emails an HTML summary when new items are found.

**Main loop** (`scraper.py`):
- `run_loop()` — polls a single provider repeatedly at `CHECK_INTERVAL` seconds
- `run_all_isolated()` — spawns each provider as a separate subprocess (used with `--all`)
- `check_once()` — single poll cycle: fetch → parse → filter → email → save state
- State is persisted per-provider in `state.<provider-key>.json` (tracks `notified_ids` and `rejected_ids`)

**Provider system** (`providers/`):
- `base.py` — abstract `Provider` class; all providers must implement `parse()`, `get_only_completed_ids()`, `item_to_text()`, `items_to_html_table()`, `get_day_label()`, `url`, `name`, `state_key`
- `__init__.py` — provider registry (`PROVIDERS` dict); register new providers here
- `fetch()` defaults to JSON GET; override for HTML scraping (see `ynet_ai_html_base.py`)
- `is_rtl()` — controls bidi rendering in headings/emails (used by Ynet providers)

**Filtering pipeline** (in `check_once()`):
1. Fetch raw data from provider URL
2. `parse()` → all items
3. Filter to completed items only (`get_only_completed_ids()`)
4. Drop items published before `cutoff_dt()` (based on `last_check` timestamp)
5. Drop previously `rejected_ids` and `notified_ids`
6. If items remain → send email → record `notified_ids` → save state

**Environment variable resolution** (`_getenv_provider_scoped()`):
- Provider-scoped vars take precedence: `EMAIL_TO__ESPN_NBA` overrides `EMAIL_TO`
- Provider key is normalized to upper snake case: `espn-nba` → `ESPN_NBA`
- All integer env vars support inline `#` comments (e.g. `CHECK_INTERVAL=900 # 15 min`)

## Code Style

- **No excessive defensive coding.** Don't add `str()` casts on values already guaranteed to be strings (e.g. `str(game.get("id"))` inside a `%s` format), don't wrap known-present fields in `.get("key", "")` when the field is always set by the API, and don't guard with `or ""` on values that can't be None. Unnecessary defensiveness obscures intent and makes the code harder to read.

## Adding a New Provider

1. Create `providers/my_provider.py` subclassing `Provider`
2. Implement all abstract methods
3. Add to `PROVIDERS` in `providers/__init__.py`

## Docker

```bash
./docker/build-and-push.sh              # build locally
./docker/build-and-push.sh --dockerhub  # build + push to Docker Hub
./docker/deploy.sh loop --all           # run all providers in container
./docker/deploy.sh --dockerhub loop --all  # pull from Docker Hub and run
```

State is stored in a named Docker volume (`scraper_state`) and persists across redeploys.

## Environment

Copy `.env.example` to `.env`. Any variable can be overridden per-provider (e.g., `EMAIL_TO__ESPN_NBA`).

| Variable | Description |
|---|---|
| `SMTP_HOST` / `SMTP_PORT` | Gmail SMTP (defaults: `smtp.gmail.com`, 587) |
| `SMTP_USER` / `SMTP_PASS` | Gmail credentials (App Password required) |
| `EMAIL_TO` | Recipient(s), comma-separated; supports provider overrides |
| `CHECK_INTERVAL` | Poll interval in seconds (default: 300); supports provider overrides |
| `STATE_FILE` | State file path (default: `state.json`; per-provider files auto-created) |
| `IMAP_*` / `EMAIL_POLL_*` | Required only for `email-url-summary` provider |
| `LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |
| `DOCKERHUB_*` | Registry auth (only needed with `--dockerhub` flag) |
