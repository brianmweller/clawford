#!/usr/bin/env python3
"""morning-meeting-brief.py — Meetings Coach (Sergeant Murphy) morning brief.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`meetings-coach:morning-meeting-brief`. Runs gcal-fetch.py for today +
tomorrow, runs meeting-prep.py per real meeting to pick up open
commitments, and writes cache/morning-brief-ready.txt for the 5 AM PT
fleet delivery path. On Mondays appends a WEEK AHEAD section built
from --days 7 output — the Option C fold that replaces the retired
separate `weekly-review` cron.

Pure Python templating. SCRIPT_CONTRACT-compliant: always exits 0,
prints one JSON line.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break


WORKSPACE = Path(os.path.expanduser("~/.openclaw/meetings-coach-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
BRIEF_FILE = CACHE_DIR / "morning-brief-ready.txt"
LAST_RUN_FILE = CACHE_DIR / "last-morning-meeting.json"

PACIFIC = ZoneInfo("America/Los_Angeles")
SUBPROCESS_TIMEOUT_S = 90


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name)] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    stdout = (result.stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None


def _parse_event_dt(iso_str: str) -> datetime | None:
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
    """Filter real meetings and partition by today/tomorrow Pacific date."""
    today_p = now_pacific.date()
    today_events: list = []
    tomorrow_events: list = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if not event.get("is_real_meeting", False):
            continue
        start = _parse_event_dt(event.get("start", ""))
        if start is None:
            continue
        delta = (start.date() - today_p).days
        if delta == 0:
            today_events.append(event)
        elif delta == 1:
            tomorrow_events.append(event)
    today_events.sort(key=lambda e: e.get("start", ""))
    tomorrow_events.sort(key=lambda e: e.get("start", ""))
    return today_events, tomorrow_events


def _fmt_time(dt: datetime) -> str:
    h12 = dt.hour % 12
    if h12 == 0:
        h12 = 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{h12}:{dt.minute:02d} {suffix}"


def _attendee_names(event: dict) -> str:
    attendees = event.get("attendees") or []
    names = []
    for a in attendees:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or a.get("email") or "").strip()
        if name:
            names.append(name)
    return ", ".join(names)


def _open_commit_summary(prep: dict | None) -> str | None:
    if not isinstance(prep, dict):
        return None
    meetings = prep.get("meetings") or []
    if not meetings:
        return None
    ctx = (meetings[0] or {}).get("context") or {}
    commits = [c for c in ctx.get("commitments", []) if c.get("status") in ("open", "overdue")]
    if not commits:
        return None
    first = commits[0]
    to_whom = (first.get("to_whom") or "?").strip()
    what = (first.get("what") or "").strip()
    return f"Open with {to_whom}: {what}"


def _fmt_meeting_line(event: dict, prep: dict | None) -> list[str]:
    start = _parse_event_dt(event.get("start", ""))
    time_str = _fmt_time(start) if start else "??:??"
    title = event.get("summary") or "(untitled)"
    lines = [f"\U0001f4cb {time_str} — {title}"]
    names = _attendee_names(event)
    if names:
        lines.append(f"   \U0001f465 {names}")
    location = (event.get("location") or "").strip()
    if location:
        lines.append(f"   \U0001f4cd {location}")
    summary = _open_commit_summary(prep)
    if summary:
        lines.append(f"   \U0001f4cc {summary}")
    return lines


def _fmt_preview_line(event: dict) -> str:
    start = _parse_event_dt(event.get("start", ""))
    time_str = _fmt_time(start) if start else "??:??"
    title = event.get("summary") or "(untitled)"
    return f"  {time_str} — {title}"


def _format_week_ahead_lines(week_events: list, now_pacific: datetime) -> list[str]:
    lines: list[str] = []
    lines.append("")
    lines.append("\U0001f4c5 WEEK AHEAD")
    lines.append("\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501")

    today_p = now_pacific.date()
    by_day: dict[str, list[dict]] = {}
    for event in week_events:
        if not isinstance(event, dict):
            continue
        if not event.get("is_real_meeting", False):
            continue
        start = _parse_event_dt(event.get("start", ""))
        if start is None:
            continue
        delta = (start.date() - today_p).days
        if delta < 0 or delta > 6:
            continue
        key = start.strftime("%A")
        by_day.setdefault(key, []).append(event)

    import datetime as _dt
    for i in range(7):
        day_dt = now_pacific + _dt.timedelta(days=i)
        day_events = by_day.get(day_dt.strftime("%A"), [])
        if not day_events:
            continue
        label = day_dt.strftime("%A, %b %-d" if sys.platform != "win32" else "%A, %b %#d")
        lines.append(label)
        for event in day_events:
            lines.append(_fmt_preview_line(event))
    return lines


def format_brief(
    events: list,
    week_events: list | None,
    prep_lookup: dict,
    now_pacific: datetime,
) -> str:
    """Render the full morning brief.

    `events` is the raw gcal-fetch --days 2 list (both today + tomorrow).
    `week_events` is an optional 7-day list — pass on Mondays only.
    `prep_lookup` maps event_id → meeting-prep.py JSON output.
    """
    today_events, tomorrow_events = split_today_tomorrow(events, now_pacific)

    weekday = now_pacific.strftime("%A")
    month = now_pacific.strftime("%B")
    day = now_pacific.day
    header = f"\U0001f437\U0001f50d Meeting Brief — {weekday}, {month} {day}"

    lines: list[str] = [header, ""]

    if not today_events:
        lines.append("\U0001f437\U0001f50d All quiet on the calendar. No meetings today.")
        lines.append("")
    else:
        lines.append(f"{len(today_events)} meeting(s) today")
        lines.append("")
        lines.append("\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501")
        for event in today_events:
            for line in _fmt_meeting_line(event, prep_lookup.get(event.get("id", ""))):
                lines.append(line)
        lines.append("\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501")
        lines.append("")

    lines.append("\U0001f4cb TOMORROW PREVIEW")
    if tomorrow_events:
        for event in tomorrow_events:
            lines.append(_fmt_preview_line(event))
    else:
        lines.append("  No meetings")

    if now_pacific.weekday() == 0 and week_events:
        lines.extend(_format_week_ahead_lines(week_events, now_pacific))

    # Footer
    open_items = 0
    for event in today_events:
        summary = _open_commit_summary(prep_lookup.get(event.get("id", "")))
        if summary:
            open_items += 1
    lines.append("")
    lines.append(f"\U0001f437\U0001f50d {len(today_events)} meetings \u00b7 {open_items} open items")

    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)
    now_pacific = now_utc.astimezone(PACIFIC)

    daily = _run_script("gcal-fetch.py", "--days", "2")
    daily_events = (daily or {}).get("events", []) if isinstance(daily, dict) else []

    # Per-meeting prep lookups (open commitments)
    today_events, _ = split_today_tomorrow(daily_events, now_pacific)
    prep_lookup: dict = {}
    for event in today_events:
        eid = event.get("id", "")
        if not eid:
            continue
        prep = _run_script("meeting-prep.py", "--meeting-id", eid, timeout=60)
        if prep is not None:
            prep_lookup[eid] = prep

    week_events: list | None = None
    if now_pacific.weekday() == 0:
        weekly = _run_script("gcal-fetch.py", "--days", "7")
        if isinstance(weekly, dict):
            week_events = weekly.get("events", [])

    body = format_brief(daily_events, week_events, prep_lookup, now_pacific)
    _write_atomic(BRIEF_FILE, body)

    today_events, tomorrow_events = split_today_tomorrow(daily_events, now_pacific)
    _write_atomic(
        LAST_RUN_FILE,
        json.dumps(
            {
                "timestamp": now_utc.isoformat(),
                "status": "ok",
                "today_count": len(today_events),
                "tomorrow_count": len(tomorrow_events),
                "prep_count": len(prep_lookup),
                "weekly_overview_included": week_events is not None,
                "daily_source_ok": daily is not None,
            },
            indent=2,
        ),
    )

    return {
        "status": "ok",
        "brief_path": str(BRIEF_FILE),
        "today_count": len(today_events),
        "tomorrow_count": len(tomorrow_events),
        "prep_count": len(prep_lookup),
        "weekly_overview_included": week_events is not None,
        "daily_source_ok": daily is not None,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f437\U0001f50d morning-meeting-brief failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
