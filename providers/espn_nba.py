"""
ESPN NBA Scoreboard provider.

All logic specific to https://www.espn.com/nba/scoreboard lives here:
URL, JSON parsing, game-completion detection, and text/HTML formatting.
"""

from datetime import datetime, timedelta, timezone
import os
import logging
import requests
from .base import Provider


logger = logging.getLogger(__name__)


class EspnNba(Provider):
    """Scrapes the ESPN NBA scoreboard via their public JSON API."""

    def __init__(self) -> None:
        """Initialize provider instance state used for OpenAI model logging."""
        self._last_logged_openai_model: str | None = None

    # -- Provider identity ---------------------------------------------------

    @property
    def name(self) -> str:
        """Human-readable provider name."""
        return "ESPN NBA"

    @property
    def state_key(self) -> str:
        """Unique key used to namespace this provider's data: state.<provider-key>.json."""
        return "espn_nba"

    @property
    def url(self) -> str:
        """Scoreboard API URL returning games for the day."""
        return "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

    @property
    def standings_url(self) -> str:
        """Standings API URL used to enrich the HTML email with conference tables."""
        return "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"

    def cutoff_dt(self) -> datetime | None:
        """Disable time-based filtering.

        ESPN's scoreboard API response is already scoped to the current day, 
        so there is no need for additional datetime cutoff filtering in the agent.
        """
        return None

    def fetch(self) -> dict:
        """Fetch scoreboard JSON and cache the standings JSON for formatting."""
        resp = requests.get(self.url, timeout=30)
        resp.raise_for_status()
        scoreboard = resp.json()

        try:
            standings_resp = requests.get(self.standings_url, timeout=30)
            standings_resp.raise_for_status()
            self._standings_data = standings_resp.json()
        except Exception:
            self._standings_data = None

        return scoreboard

    def heading(self, day_label: str) -> str:
        """Display heading for output/emails."""
        return f"NBA Scoreboard – {day_label}"

    # -- Parse ---------------------------------------------------------------

    def parse(self, data: dict) -> list[dict]:
        """Parse the ESPN JSON into a flat list of game dicts."""
        games = []
        events = data.get("events", [])
        season_type = data.get("season", {}).get("type", 2)  # 2=regular, 3=playoffs
        for event in events:
            game: dict = {"id": event["id"], "name": event.get("name", "")}

            competition = event["competitions"][0]
            game["date"] = competition.get("date", "")
            game["venue"] = competition.get("venue", {}).get("fullName", "")
            venue_address = competition.get("venue", {}).get("address", {})
            city = venue_address.get("city", "")
            state = venue_address.get("state", "")
            game["location"] = f"{city}, {state}" if city else ""

            status_obj = competition.get("status", {})
            status_type = status_obj.get("type", {})
            game["status"] = status_type.get("description", "")
            game["status_id"] = status_type.get("id", "0")  # 1=scheduled, 2=in-progress, 3=final
            game["status_state"] = status_type.get("state", "")
            game["status_completed"] = bool(status_type.get("completed", False))
            logger.debug(
                "ESPN NBA event id=%s name=%s status=%s status_id=%s status_state=%s status_completed=%s",
                game["id"],
                game["name"],
                game["status"],
                game["status_id"],
                game["status_state"],
                game["status_completed"],
            )
            game["clock"] = status_obj.get("displayClock", "")
            game["period"] = status_obj.get("period", 0)

            # Playoff series
            game["is_playoff"] = season_type == 3
            series_obj = competition.get("series", {})
            game["series_summary"] = series_obj.get("summary", "")
            game["series_title"] = series_obj.get("title", "")

            # Broadcast
            broadcasts = competition.get("broadcasts", [])
            broadcast_names = []
            for b in broadcasts:
                for n in b.get("names", []):
                    broadcast_names.append(n)
            game["broadcast"] = ", ".join(broadcast_names)

            # Odds
            odds_list = competition.get("odds", [])
            if odds_list:
                odds = odds_list[0]
                game["spread"] = odds.get("details", "")
                game["overUnder"] = odds.get("overUnder", "")
                game["provider"] = odds.get("provider", {}).get("name", "")
            else:
                game["spread"] = ""
                game["overUnder"] = ""
                game["provider"] = ""

            # Teams & scores
            teams_info = []
            for comp_team in competition.get("competitors", []):
                team_data = comp_team.get("team", {})
                record_items = comp_team.get("records", [])
                overall_record = record_items[0]["summary"] if record_items else ""
                home_away_record = record_items[1]["summary"] if len(record_items) > 1 else ""
                ha_label = comp_team.get("homeAway", "")

                # Leaders / players to watch
                leaders = []
                for leader_cat in comp_team.get("leaders", []):
                    cat_name = leader_cat.get("abbreviation", leader_cat.get("name", ""))
                    for ldr in leader_cat.get("leaders", [])[:1]:
                        athlete = ldr.get("athlete", {})
                        leaders.append({
                            "category": cat_name,
                            "value": ldr.get("displayValue", ""),
                            "player": athlete.get("displayName", ""),
                            "jersey": athlete.get("jersey", ""),
                        })

                teams_info.append({
                    "name": team_data.get("displayName", ""),
                    "abbreviation": team_data.get("abbreviation", ""),
                    "score": comp_team.get("score", ""),
                    "homeAway": ha_label,
                    "record": overall_record,
                    "homeAwayRecord": home_away_record,
                    "leaders": leaders,
                })

            game["teams"] = teams_info

            # Tickets
            tickets = competition.get("tickets", [])
            if tickets:
                game["tickets"] = tickets[0].get("summary", "")
                game["ticketLink"] = (
                    tickets[0].get("links", [{}])[0].get("href", "")
                    if tickets[0].get("links") else ""
                )
            else:
                game["tickets"] = ""
                game["ticketLink"] = ""

            games.append(game)
        return games

    def openai_summary_instruction(self) -> str:
        """Instruction describing the desired OpenAI recap summary style/language."""
        return "Write a concise 8-10 sentence recap summary in Hebrew."

    def _openai_max_recap_chars(self) -> int:
        """Maximum number of recap page characters to send to OpenAI."""
        return 8000

    def _extract_recap_text_from_summary_json(self, payload: dict) -> str:
        """Extract best-effort recap/article text from ESPN's summary JSON payload."""
        if not isinstance(payload, dict):
            return ""

        _TEXT_KEYS = ("story", "body", "description", "content", "text")

        # ESPN uses both "articles" (list) and "article" (singular dict)
        article_candidates: list[dict] = []
        articles = payload.get("articles")
        if isinstance(articles, list):
            article_candidates.extend(a for a in articles if isinstance(a, dict))
        article = payload.get("article")
        if isinstance(article, dict):
            article_candidates.append(article)

        for candidate in article_candidates:
            for key in _TEXT_KEYS:
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        recap = payload.get("recap")
        if isinstance(recap, dict):
            for key in _TEXT_KEYS:
                value = recap.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        logger.debug(
            "[%s] Summary JSON top-level keys (no recap text found): %s",
            self.name,
            list(payload.keys()),
        )
        return ""

    def _fetch_recap_text(self, game_id: str) -> tuple[str, str]:
        """Fetch recap text from ESPN's summary JSON API."""
        summary_url = (
            "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
            f"?region=us&lang=en&contentorigin=espn&event={game_id}"
        )
        recap_url = f"https://www.espn.com/nba/recap/_/gameId/{game_id}"

        try:
            resp = requests.get(
                summary_url,
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
            recap_text = self._extract_recap_text_from_summary_json(resp.json())
            recap_text = (self._html_to_text(recap_text) or "").strip()
            if recap_text and recap_text != "No Story Available":
                return recap_text, recap_url
        except Exception:
            pass

        return "", recap_url

    def _fetch_recap_summary(self, game: dict) -> str:
        """Fetch the game recap HTML and summarize it via OpenAI (best-effort)."""

        recap_text, recap_url = self._fetch_recap_text(str(game["id"]))
        if not recap_text or recap_text == "No Story Available":
            logger.debug(
                "[%s] Recap not available yet (%s): %s",
                self.name,
                "empty" if not recap_text else "No Story Available",
                recap_url,
            )
            return ""
        if len(recap_text) < 300:
            logger.warning("[%s] Recap text seems too short (len=%d): %s\ntext=%r", self.name, len(recap_text), recap_url, recap_text)

        game["recapUrl"] = recap_url

        if not self._openai_api_key():
            return ""

        max_chars = int(self._openai_max_recap_chars() or 0)
        if max_chars > 0 and len(recap_text) > max_chars:
            recap_text = recap_text[:max_chars]

        title = game.get("name")
        analysis = self._openai_analyze_article(title=title, url=recap_url, text=recap_text)
        summary = analysis.get("summary")
        if summary and len(summary) < 300:
            recap_text_preview = recap_text
            if len(recap_text_preview) > 800:
                recap_text_preview = recap_text_preview[:800] + "..."
            logger.warning(
                "[%s] Recap summary seems too short (len=%d): %s\ntext=%r\nsummary=%r",
                self.name,
                len(summary),
                recap_url,
                recap_text_preview,
                summary,
            )
        return summary

    def get_only_completed_ids(self, items: list[dict]) -> set[str]:
        """Return the set of IDs for items that are completed (final) within the given list."""
        # return {g["id"] for g in items if str(g.get("status_id")) == "3"} # status_id '3' means Final in ESPN's API
        completed = set()
        for g in items:
            status_id = str(g.get("status_id"))
            if status_id == "3":
                completed.add(g["id"])
                continue
            if bool(g.get("status_completed")):
                completed.add(g["id"])
                continue
            if str(g.get("status_state") or "").lower() == "post":
                completed.add(g["id"])
                continue
        return completed

    def enrich_completed_items(self, items: list[dict]) -> list[dict]:
        """Enrich completed games with recap summaries (best-effort).

        This does not reject any items; it only adds `recapSummary` when possible.
        """
        for g in items:
            # logging.debug(g)
            is_completed = (
                str(g.get("status_id")) == "3"
                or bool(g.get("status_completed"))
                or str(g.get("status_state") or "").lower() == "post"
            )
            if not is_completed:
                continue

            if g.get("recapSummary"):
                continue

            try:
                summary = self._fetch_recap_summary(g)
            except Exception:
                summary = ""
            if summary:
                g["recapSummary"] = summary
        return items

    def should_record_notifiable_id(self, item: dict, day_key: str) -> bool:
        """Skip recording `notified_ids` for recently-completed games with no recap yet.

        Motivation: ESPN often publishes the recap page with a delay. 
        When we email a game immediately after it goes Final but we couldn't retrieve a recap summary yet, we want to allow the next run(s) to re-notify once the recap becomes available (within a 12 hours window).
        """
        recap_summary = item.get("recapSummary")
        if recap_summary:
            return True

        raw_date = item.get("date")
        if not raw_date:
            logging.warning("No start date in a game! %s", item)
            return True

        # Align the game start date/time to UTC
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        # A game with no recap should not be recorded within 12 hours since the game start date/time
        now_utc = datetime.now(timezone.utc)
        if now_utc - dt < timedelta(hours=24):
            return False

        logger.warning(
            "[%s] No recap after 12 hours; recording as notified: id=%s name=%s date=%s",
            self.name,
            str(item.get("id")),
            str(item.get("name")),
            str(raw_date),
        )
        return True

    def get_day_label(self, data: dict) -> str:
        """Extract the day label from the ESPN scoreboard payload.

        Time zone semantics:
        - This value is taken verbatim from ESPN (`data['day']['date']`). It is a date string without an explicit time zone.
        - It should be treated as a display label only (not used for datetime arithmetic).
        """
        return data.get("day", {}).get("date", "today")

    # -- Formatting ----------------------------------------------------------

    def item_to_text(self, g: dict) -> str:
        """Render a single game as console-friendly plain text.

        Time zone semantics:
        - ESPN's `g['date']` is typically an ISO-8601 timestamp with a time zone (often `Z`).
        - When tz-aware, this method converts it to the machine's **local time zone** for display.
        - When tz-naive or unparsable, the raw string is displayed as-is.
        """
        lines: list[str] = []

        dt = g.get("date", "")
        if dt:
            try:
                dt_obj = datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone()
                dt_display = dt_obj.strftime("%I:%M %p  %b %d, %Y %Z")
            except Exception:
                dt_display = dt
        else:
            dt_display = ""

        away = next((t for t in g["teams"] if t["homeAway"] == "away"), None)
        home = next((t for t in g["teams"] if t["homeAway"] == "home"), None)

        def team_line(t: dict, label: str) -> str:
            """Render a single team's line (name/score/records) for plain-text output."""
            score_part = f"  {t['score']}" if t.get("score") else ""
            rec_part = f"  ({t['record']}  {t['homeAwayRecord']} {label})" if t.get("record") else ""
            return f"  {t['name']}{score_part}{rec_part}"

        def get_status_line(g: dict) -> str:
            """Return the game status line (status + broadcast)."""
            status_line = g["status"]
            if g.get("broadcast"):
                status_line += f"  [{g['broadcast']}]"
            return status_line

        status_line = get_status_line(g)

        lines.append(f"{dt_display}   {status_line}")
        if away:
            lines.append(team_line(away, "Away"))
        if home:
            lines.append(team_line(home, "Home"))

        if g.get("is_playoff") and g.get("series_summary"):
            series_label = g["series_summary"]
            if g.get("series_title"):
                series_label = f"{g['series_title']} – {series_label}"
            lines.append(f"  Series: {series_label}")

        if g.get("venue"):
            loc = f"{g['venue']}, {g['location']}" if g["location"] else g["venue"]
            lines.append(f"  Venue: {loc}")

        if g.get("spread") or g.get("overUnder"):
            odds_parts = []
            if g["spread"]:
                odds_parts.append(f"Spread: {g['spread']}")
            if g["overUnder"]:
                odds_parts.append(f"O/U: {g['overUnder']}")
            prov = f" ({g['provider']})" if g.get("provider") else ""
            lines.append(f"  Odds{prov}: {' | '.join(odds_parts)}")

        if g.get("tickets"):
            lines.append(f"  Tickets: {g['tickets']}")

        # Players to watch
        for t in g["teams"]:
            for ldr in t.get("leaders", []):
                lines.append(
                    f"  {t['abbreviation']} - {ldr['player']}  "
                    f"{ldr['category']}: {ldr['value']}"
                )

        recap_summary = (g.get("recapSummary") or "").strip()
        if recap_summary:
            rli = "\u2067"  # Right-to-Left Isolate
            pdi = "\u2069"  # Pop Directional Isolate
            lines.append("")
            lines.append(f"{rli}{recap_summary}{pdi}")

        return "\n".join(lines)

    def items_to_html_table(self, items: list[dict]) -> str:
        """Build an HTML table summarising all games (used in the email body).

        Time zone semantics:
        - Uses `g['date']` and, when tz-aware, converts it to the machine's **local time zone** for display in the email.
        - When tz-naive or unparsable, falls back to displaying the raw value.
        """
        def leaders_inline(team: dict) -> str:
            """Render a team's leader stats as a single-line string for the email."""
            parts = []
            for ldr in team.get("leaders", []):
                parts.append(
                    f"{ldr['player']} – "
                    f"{ldr['category']}: {ldr['value']}"
                )
            return " | ".join(parts)

        _HEADER_ROW = (
            "<tr style='background:#1a1a2e;color:#fff;'>"
            "<th>Time</th><th>Away</th><th>Score</th><th>Score</th>"
            "<th>Home</th><th>Venue</th><th>Box Score</th></tr>"
        )
        _TABLE_STYLE = (
            "border-collapse:collapse;font-family:Arial,sans-serif;"
            "border:3px solid #1a1a2e;"
        )

        game_tables = []
        for g in items:
            away = next((t for t in g["teams"] if t["homeAway"] == "away"), None)
            home = next((t for t in g["teams"] if t["homeAway"] == "home"), None)
            if not away or not home:
                continue

            dt = g.get("date", "")
            try:
                dt_obj = datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone()
                time_str = dt_obj.strftime("%I:%M %p")
            except Exception:
                time_str = dt

            box_score_url = f"https://www.espn.com/nba/boxscore/_/gameId/{g['id']}"
            recap_url = g.get("recapUrl") or ""
            recap_summary = (g.get("recapSummary") or "").replace("<", "&lt;").replace(">", "&gt;")

            away_leaders = leaders_inline(away)
            home_leaders = leaders_inline(home)
            leaders_row_html = ""
            if away_leaders or home_leaders:
                away_prefix = away.get("abbreviation") or away.get("name") or "Away"
                home_prefix = home.get("abbreviation") or home.get("name") or "Home"
                leaders_row_html = (
                    "<tr><td colspan='7' style='font-size:12px;line-height:1.35'>"
                    f"<b>{away_prefix}</b>: {away_leaders}<br>"
                    f"<b>{home_prefix}</b>: {home_leaders}"
                    "</td></tr>"
                )

            series_row_html = ""
            if g.get("is_playoff") and g.get("series_summary"):
                series_label = g["series_summary"]
                if g.get("series_title"):
                    series_label = f"{g['series_title']} – {series_label}"
                series_row_html = (
                    f"<tr><td colspan='7' style='font-size:12px;font-style:italic;color:#555'>"
                    f"{series_label}</td></tr>"
                )

            main_row = (
                f"<tr>"
                f"<td>{time_str}</td>"
                f"<td>{away['name']} ({away['record']})</td>"
                f"<td style='text-align:center;font-weight:bold'>{away.get('score', '-')}</td>"
                f"<td style='text-align:center;font-weight:bold'>{home.get('score', '-')}</td>"
                f"<td>{home['name']} ({home['record']})</td>"
                f"<td>{g.get('venue', '')}<br><span style='color:gray;font-size:11px'>"
                f"({home['abbreviation']} home)</span></td>"
                f"<td><a href='{box_score_url}'>Box Score</a>"
                + (f"<br><a href='{recap_url}'>Recap</a>" if recap_url else "")
                + "</td></tr>"
            )
            recap_row_html = (
                f"<tr><td colspan='7' dir='rtl' style='direction:rtl;text-align:right;"
                f"font-size:13px;line-height:1.35;color:#222'>{recap_summary}</td></tr>"
                if recap_summary else ""
            )
            game_tables.append(
                f"<table border='1' cellpadding='6' cellspacing='0' style='{_TABLE_STYLE}'>"
                + _HEADER_ROW
                + main_row
                + series_row_html
                + recap_row_html
                + leaders_row_html
                + "</table>"
            )

        games_table = "<br>".join(game_tables)

        standings_section = ""
        is_playoff_season = any(g.get("is_playoff") for g in items)
        standings_data = getattr(self, "_standings_data", None)
        if standings_data and not is_playoff_season:
            standings = self._parse_standings(standings_data)
            if standings and (standings.get("conferences") or []):
                season_label = standings.get("seasonDisplayName", "")
                standings_section += "<br><h3>NBA Standings" + (f" {season_label}" if season_label else "") + "</h3>"
                for conf in standings.get("conferences", []):
                    standings_section += "<h4>" + conf.get("name", "") + "</h4>"
                    standings_section += self._standings_to_html_table(conf)

        return games_table + standings_section

    def _parse_standings(self, data: dict) -> dict:
        """Parse ESPN standings payload into a conference/rows structure for rendering."""
        conferences = []
        for child in data.get("children", []):
            if not child.get("isConference"):
                continue

            standings_obj = (child.get("standings") or {})
            entries = standings_obj.get("entries", [])
            rows = []
            for e in entries:
                team = (e.get("team") or {})
                stats = e.get("stats", [])

                def stat_display(name: str) -> str:
                    """Return the human-formatted (string) display value for a given stat.

                    Uses ESPN's `displayValue` field (e.g. ".727", "W2", "4.5"), which is
                    intended for presentation.
                    """
                    for s in stats:
                        if s.get("name") == name or s.get("type") == name:
                            return str(s.get("displayValue", ""))
                    return ""

                def stat_value(name: str) -> float:
                    """Return the numeric value for a given stat (for sorting/math).

                    Uses ESPN's raw `value` field, which is suitable for comparisons and
                    ordering (e.g. winPercent as 0.72727275).
                    """
                    for s in stats:
                        if s.get("name") == name or s.get("type") == name:
                            try:
                                return float(s.get("value", 0.0) or 0.0)
                            except Exception:
                                return 0.0
                    return 0.0

                def record_summary(record_type: str) -> str:
                    """Return the record summary string for record-type stats.

                    Example: lasttengames -> "8-2".
                    """
                    for s in stats:
                        if s.get("type") == record_type:
                            return str(s.get("summary", s.get("displayValue", "")))
                    return ""

                seed = stat_display("playoffSeed")
                clincher = stat_display("clincher")
                rows.append(
                    {
                        "seed": seed,
                        "clincher": clincher,
                        "abbr": team.get("abbreviation", ""),
                        "team": team.get("displayName", ""),
                        "wins": stat_display("wins"),
                        "losses": stat_display("losses"),
                        "pct": stat_display("winPercent"),
                        "pct_value": stat_value("winPercent"),
                        "gb": stat_display("gamesBehind"),
                        "streak": stat_display("streak"),
                        "l10": record_summary("lasttengames"),
                    }
                )

            """Sort standings within the conference by winning percentage (PCT) descending.

            - Primary key: `pct_value` (float), sorted descending by using `-pct_value` (Python sorts ascending by default.)
            - Secondary key: `seed` as a stable tiebreaker to keep deterministic output when two teams have the same PCT.

            The `key` function is called once per element in `rows`.
            Each element is a single standings row dict (one team).
            """
            rows.sort(key=lambda row: (-float(row.get("pct_value", 0.0) or 0.0), str(row.get("seed", ""))))

            conferences.append(
                {
                    "name": child.get("name", ""),
                    "abbreviation": child.get("abbreviation", ""),
                    "rows": rows,
                }
            )

        season_display_name = ""
        for child in data.get("children", []):
            standings_obj = (child.get("standings") or {})
            season_display_name = standings_obj.get("seasonDisplayName") or season_display_name

        return {"seasonDisplayName": season_display_name, "conferences": conferences}

    def _standings_to_html_table(self, conf: dict) -> str:
        """Render a single conference standings table as HTML."""
        rows = []
        for r in conf.get("rows", []):
            seed = r.get("seed", "")
            clincher = r.get("clincher", "")
            seed_cell = (clincher + " " if clincher else "") + str(seed)
            rows.append(
                "<tr>"
                f"<td style='text-align:center'>{seed_cell}</td>"
                f"<td>{r.get('abbr','')} &nbsp; {r.get('team','')}</td>"
                f"<td style='text-align:center'>{r.get('wins','')}</td>"
                f"<td style='text-align:center'>{r.get('losses','')}</td>"
                f"<td style='text-align:center'>{r.get('pct','')}</td>"
                f"<td style='text-align:center'>{r.get('gb','')}</td>"
                f"<td style='text-align:center'>{r.get('streak','')}</td>"
                f"<td style='text-align:center'>{r.get('l10','')}</td>"
                "</tr>"
            )

        return (
            "<table border='1' cellpadding='6' cellspacing='0' "
            "style='border-collapse:collapse;font-family:Arial,sans-serif;'>"
            "<tr style='background:#1a1a2e;color:#fff;'>"
            "<th>Seed</th><th>Team</th><th>W</th><th>L</th><th>PCT</th><th>GB</th><th>STRK</th><th>L10</th>"
            "</tr>"
            + "\n".join(rows)
            + "</table>"
        )
