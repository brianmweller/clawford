#!/usr/bin/env python3
"""morning-briefing.py — Family Calendar (Mistress Mouse) morning brief.

Phase 4 liberation: replaces the OpenClaw LLM cron
`family-calendar:morning-briefing`. Runs gcal-fetch.py, groups today's
events by time block, flags tomorrow's non-routine items, and on
Mondays appends a weekly overview section (Option C — fold weekly-
overview into the morning brief instead of running a separate cron).
Writes to cache/morning-brief-ready.txt for the 5 AM PT fleet delivery
path.

Pure Python templating — the LLM isn't adding composition value for
structured calendar data. See format_brief() below for the output layout.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --- shared library sys.path shim ---
# Find the first ancestor containing agents/shared/ and prepend it to
# sys.path so `from agents.shared import X` resolves in both the local
# repo layout and the deployed <workspace>/agents/shared/ layout.
# See agents/shared/deploy.py::sync_shared_library.
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.subprocess_helpers import (  # noqa: E402
    is_subprocess_error,
    run_json_script,
)


WORKSPACE = Path(os.path.expanduser("~/.clawford/family-calendar-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
BRIEF_FILE = CACHE_DIR / "morning-brief-ready.txt"
LAST_RUN_FILE = CACHE_DIR / "last-morning-brief.json"

PACIFIC = ZoneInfo("America/Los_Angeles")
SUBPROCESS_TIMEOUT_S = 90


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


def _parse_event_dt(iso_str: str) -> datetime | None:
    """Parse an ISO-8601 event start (e.g. '2026-04-14T09:00:00-07:00')
    into an offset-aware datetime. Converts to Pacific so downstream
    code only deals with local times."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PACIFIC)


def split_today_tomorrow(events: list, now_pacific: datetime) -> tuple[list, list]:
    """Partition events into today / tomorrow buckets by Pacific date.
    Events beyond tomorrow are dropped (not useful in the morning brief)."""
    today_p = now_pacific.date()
    today_events: list = []
    tomorrow_events: list = []
    for event in events:
        if not isinstance(event, dict):
            continue
        start_dt = _parse_event_dt(event.get("start", ""))
        if start_dt is None:
            continue
        event_date = start_dt.date()
        days_ahead = (event_date - today_p).days
        if days_ahead == 0:
            today_events.append(event)
        elif days_ahead == 1:
            tomorrow_events.append(event)

    today_events.sort(key=lambda e: e.get("start", ""))
    tomorrow_events.sort(key=lambda e: e.get("start", ""))
    return today_events, tomorrow_events


def group_by_time_block(events: list) -> dict:
    """Split events into morning (<12), afternoon (12-16), evening (17+)
    buckets. Uses the Pacific hour of each event's start time."""
    groups = {"morning": [], "afternoon": [], "evening": []}
    for event in events:
        start_dt = _parse_event_dt(event.get("start", ""))
        if start_dt is None:
            continue
        h = start_dt.hour
        if h < 12:
            groups["morning"].append(event)
        elif h < 17:
            groups["afternoon"].append(event)
        else:
            groups["evening"].append(event)
    return groups


def _fmt_time(dt: datetime) -> str:
    """Render a Pacific datetime as '9:00 AM' / '3:30 PM'."""
    h12 = dt.hour % 12
    if h12 == 0:
        h12 = 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{h12}:{dt.minute:02d} {suffix}"


def _fmt_event_line(event: dict) -> str:
    start_dt = _parse_event_dt(event.get("start", ""))
    time_str = _fmt_time(start_dt) if start_dt else "??:??"
    emoji = event.get("calendar_emoji") or ""
    label = event.get("calendar_label") or ""
    summary = event.get("summary") or "(untitled)"
    location = (event.get("location") or "").strip()

    # Shape: "HH:MM  {emoji} {label} — {summary} ({location})"
    parts = [f"{time_str}"]
    if emoji:
        parts.append(emoji)
    if label:
        parts.append(label + " —")
    parts.append(summary)
    line = "  " + " ".join(parts)
    if location:
        line += f" ({location})"
    return line


def _fmt_preview_line(event: dict) -> str:
    start_dt = _parse_event_dt(event.get("start", ""))
    time_str = _fmt_time(start_dt) if start_dt else "??:??"
    emoji = event.get("calendar_emoji") or ""
    summary = event.get("summary") or "(untitled)"
    tag = f"{emoji} " if emoji else ""
    return f"  {time_str}  {tag}{summary}"


def format_brief(
    events: list,
    week_events: list | None,
    now_pacific: datetime,
) -> str:
    """Render the full morning brief: today section (time-blocked) + tomorrow preview.

    `events` is the combined today+tomorrow list from gcal-fetch --days 2.
    `week_events` is optional — pass a 7-day list only on Mondays to
    emit the appended WEEK AHEAD section.
    """
    today_events, tomorrow_events = split_today_tomorrow(events, now_pacific)

    weekday = now_pacific.strftime("%A")
    month = now_pacific.strftime("%B")
    day = now_pacific.day
    header = f"🐭📅 Family Day — {weekday}, {month} {day}"

    lines: list[str] = [header, ""]

    if not today_events:
        lines.append(f"Standard {weekday} — no exceptions.")
        lines.append("")
    else:
        grouped = group_by_time_block(today_events)
        rule = "━━━━━━━━━━━━━━━"
        if grouped["morning"]:
            lines.append("☀️ MORNING")
            lines.append(rule)
            for event in grouped["morning"]:
                lines.append(_fmt_event_line(event))
            lines.append("")
        if grouped["afternoon"]:
            lines.append("🌤️ AFTERNOON")
            lines.append(rule)
            for event in grouped["afternoon"]:
                lines.append(_fmt_event_line(event))
            lines.append("")
        if grouped["evening"]:
            lines.append("🌙 EVENING")
            lines.append(rule)
            for event in grouped["evening"]:
                lines.append(_fmt_event_line(event))
            lines.append("")

    lines.append("📋 TOMORROW PREVIEW")
    if tomorrow_events:
        for event in tomorrow_events:
            lines.append(_fmt_preview_line(event))
    else:
        tomorrow_dt = now_pacific + __import__("datetime").timedelta(days=1)
        lines.append(f"  Standard {tomorrow_dt.strftime('%A')} — no exceptions.")
    lines.append("")

    # Monday-only: weekly overview section
    if now_pacific.weekday() == 0 and week_events:
        lines.extend(_format_weekly_overview_lines(week_events, now_pacific))

    lines.append(f"🐭 {len(today_events)} event(s) today")

    return "\n".join(lines).rstrip() + "\n"


def _format_weekly_overview_lines(
    week_events: list,
    now_pacific: datetime,
) -> list[str]:
    """Render a day-by-day overview for the 7 days starting today.
    Only emits days that have at least one event — silent days collapse
    to a single 'no exceptions' entry. Used only on Mondays."""
    lines: list[str] = []
    lines.append("📅 WEEK AHEAD")
    lines.append("━━━━━━━━━━━━━━━")

    today_p = now_pacific.date()
    by_day: dict[str, list[dict]] = {}
    for event in week_events:
        start_dt = _parse_event_dt(event.get("start", ""))
        if start_dt is None:
            continue
        delta = (start_dt.date() - today_p).days
        if delta < 0 or delta > 6:
            continue
        key = start_dt.strftime("%A")
        by_day.setdefault(key, []).append(event)

    import datetime as _dt  # local alias to avoid shadowing the module
    for i in range(7):
        day_dt = now_pacific + _dt.timedelta(days=i)
        label = day_dt.strftime("%A, %b %-d" if sys.platform != "win32" else "%A, %b %#d")
        day_events = by_day.get(day_dt.strftime("%A"), [])
        if not day_events:
            continue
        lines.append(f"{label}")
        for event in day_events:
            lines.append(_fmt_preview_line(event))
    lines.append("")
    return lines


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)
    now_pacific = now_utc.astimezone(PACIFIC)

    sources_failed: list[dict] = []

    # 2-day fetch covers today + tomorrow. This is the primary data
    # source — without it, the brief is meaningless. Propagate subprocess
    # failures as top-level errors so the script-contract wrapper fires
    # a fix-it alert instead of silently writing an empty brief.
    daily = _run_script("gcal-fetch.py", "--days", "2", "--skip-meetings")
    if is_subprocess_error(daily):
        error_msg = daily["__error__"]
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "error",
                    "error": error_msg,
                    "summary": f"gcal-fetch (daily) failed: {error_msg[:120]}",
                },
                indent=2,
            ),
        )
        return {
            "status": "error",
            "error": error_msg,
            "alert": f"🐭 morning-briefing failed: {error_msg[:200]}",
        }

    daily_events = daily.get("events", []) if isinstance(daily, dict) else []

    # Weekly overview is optional — only folded in on Mondays. Track any
    # failure in sources_failed and keep going; today+tomorrow are still
    # useful without the week-ahead section.
    week_events: list | None = None
    if now_pacific.weekday() == 0:
        weekly = _run_script("gcal-fetch.py", "--days", "7", "--skip-meetings")
        if is_subprocess_error(weekly):
            sources_failed.append(
                {"source": "gcal-fetch:weekly", "error": weekly["__error__"]}
            )
        elif isinstance(weekly, dict):
            week_events = weekly.get("events", [])

    body = format_brief(daily_events, week_events, now_pacific)
    _write_atomic(BRIEF_FILE, body)

    today_events, tomorrow_events = split_today_tomorrow(daily_events, now_pacific)

    overall_status = "degraded" if sources_failed else "ok"

    _write_atomic(
        LAST_RUN_FILE,
        json.dumps(
            {
                "timestamp": now_utc.isoformat(),
                "status": overall_status,
                "today_count": len(today_events),
                "tomorrow_count": len(tomorrow_events),
                "weekly_overview_included": week_events is not None,
                "daily_source_ok": True,
                "sources_failed": sources_failed,
                "summary": f"{len(today_events)} event(s) today",
            },
            indent=2,
        ),
    )

    return {
        "status": overall_status,
        "brief_path": str(BRIEF_FILE),
        "today_count": len(today_events),
        "tomorrow_count": len(tomorrow_events),
        "weekly_overview_included": week_events is not None,
        "daily_source_ok": True,
        "sources_failed": sources_failed,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"🐭 morning-briefing failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
