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

from agents.shared.subprocess_helpers import (  # noqa: E402
    is_subprocess_error,
    run_json_script,
)


WORKSPACE = Path(os.path.expanduser("~/.clawford/meetings-coach-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
BRIEF_FILE = CACHE_DIR / "morning-brief-ready.txt"
LAST_RUN_FILE = CACHE_DIR / "last-morning-meeting.json"

PACIFIC = ZoneInfo("America/Los_Angeles")
SUBPROCESS_TIMEOUT_S = 90


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


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


def _meeting_result(prep: dict | None) -> dict | None:
    """meeting-prep.py returns {"meetings": [result_dict]} even for a single
    meeting. Extract the first result or None."""
    if not isinstance(prep, dict):
        return None
    meetings = prep.get("meetings") or []
    if not meetings:
        return None
    m = meetings[0]
    return m if isinstance(m, dict) else None


def _open_commit_summary(prep: dict | None) -> str | None:
    m = _meeting_result(prep)
    if m is None:
        return None
    ctx = m.get("context") or {}
    commits = [c for c in ctx.get("commitments", []) if c.get("status") in ("open", "overdue")]
    if not commits:
        return None
    first = commits[0]
    to_whom = (first.get("to_whom") or "?").strip()
    what = (first.get("what") or "").strip()
    return f"Open with {to_whom}: {what}"


_MEETING_TYPE_EMOJI = {
    "recruiter-screen": "\U0001f4de",   # 📞
    "hiring-manager": "\U0001f393",     # 🎓
    "hiring-panel": "\U0001f3af",       # 🎯
}


def _fmt_professional_prep(m: dict) -> list[str]:
    """Render the professional-prep block for a meeting whose
    meeting_type != 'general'. Falls through to the generic renderer
    (caller's responsibility) when llm_prep carries an error — the
    self_context header is still useful on its own."""
    lines: list[str] = []
    meeting_type = m.get("meeting_type", "")
    emoji = _MEETING_TYPE_EMOJI.get(meeting_type, "\U0001f4bc")  # 💼

    self_ctx = m.get("self_context") or {}
    target = self_ctx.get("target_company")
    stage = self_ctx.get("active_pipeline_stage")

    header_bits = [f"{emoji} {meeting_type}"]
    if isinstance(target, dict) and target.get("company"):
        tier = target.get("tier_company") or "?"
        header_bits.append(f"target: {target['company']} (tier {tier})")
    if isinstance(stage, dict) and stage.get("stage"):
        header_bits.append(f"pipeline: {stage['stage']}")
    lines.append("   " + " · ".join(header_bits))

    llm_prep = m.get("llm_prep") or {}
    if "error" in llm_prep:
        # LLM call failed — surface the header only and let the generic
        # commit_summary path (if any) render underneath.
        return lines

    prep_summary = (llm_prep.get("prep_summary") or "").strip()
    if prep_summary:
        lines.append(f"   {prep_summary}")

    objective = (llm_prep.get("objective") or "").strip()
    if objective:
        lines.append(f"   \U0001f3af Objective: {objective}")

    recipient = (llm_prep.get("recipient_model") or "").strip()
    if recipient:
        lines.append(f"   \U0001f91d They need: {recipient}")

    fit_pitch = (llm_prep.get("fit_pitch") or "").strip()
    if fit_pitch:
        lines.append("   \U0001f3a4 Pitch:")
        lines.append(f"     {fit_pitch}")

    compelling = (llm_prep.get("compelling_angle") or "").strip()
    if compelling:
        lines.append(f"   ✨ Why this: {compelling}")

    evidence = (llm_prep.get("fit_evidence")
                or llm_prep.get("talking_points") or [])
    if evidence:
        lines.append("   \U0001f4e2 Drop-in evidence:")
        for e in evidence[:3]:
            lines.append(f"     • {e}")

    questions = (llm_prep.get("evaluation_questions")
                 or llm_prep.get("questions_to_ask") or [])
    if questions:
        lines.append("   \U0001f4ac Ask:")
        for q in questions[:3]:
            lines.append(f"     • {q}")

    flags = llm_prep.get("red_flags") or []
    if flags:
        lines.append("   \U0001f6a9 Red flags:")
        for f in flags[:3]:
            lines.append(f"     • {f}")

    return lines


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

    # Professional-meeting prep block — only when classifier landed a
    # non-'general' type and self/ was loaded. Gracefully falls through
    # to the generic commit summary when the prep fails.
    m = _meeting_result(prep)
    if m is not None and m.get("meeting_type", "general") != "general":
        lines.extend(_fmt_professional_prep(m))

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

    sources_failed: list[dict] = []

    # gcal-fetch --days 2 is the primary source for "what meetings do
    # you have today/tomorrow". Without it the whole brief is a lie.
    # Propagate subprocess failures as a top-level error.
    daily = _run_script("gcal-fetch.py", "--days", "2")
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
            "alert": f"\U0001f437\U0001f50d morning-meeting-brief failed: {error_msg[:200]}",
        }

    daily_events = daily.get("events", []) if isinstance(daily, dict) else []

    # Per-meeting prep lookups (open commitments). Best-effort — a prep
    # failure for one event should just drop context for that row, not
    # kill the whole brief.
    today_events, _ = split_today_tomorrow(daily_events, now_pacific)
    prep_lookup: dict = {}
    prep_failures = 0
    for event in today_events:
        eid = event.get("id", "")
        if not eid:
            continue
        prep = _run_script("meeting-prep.py", "--meeting-id", eid, timeout=60)
        if is_subprocess_error(prep):
            prep_failures += 1
            continue
        if prep is not None:
            prep_lookup[eid] = prep
    if prep_failures:
        sources_failed.append(
            {"source": "meeting-prep", "error": f"{prep_failures} event(s) failed"}
        )

    # Weekly overview is Monday-only and optional — track failures but
    # keep emitting the brief without the week-ahead section.
    week_events: list | None = None
    if now_pacific.weekday() == 0:
        weekly = _run_script("gcal-fetch.py", "--days", "7")
        if is_subprocess_error(weekly):
            sources_failed.append(
                {"source": "gcal-fetch:weekly", "error": weekly["__error__"]}
            )
        elif isinstance(weekly, dict):
            week_events = weekly.get("events", [])

    body = format_brief(daily_events, week_events, prep_lookup, now_pacific)
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
                "prep_count": len(prep_lookup),
                "weekly_overview_included": week_events is not None,
                "daily_source_ok": True,
                "sources_failed": sources_failed,
            },
            indent=2,
        ),
    )

    return {
        "status": overall_status,
        "brief_path": str(BRIEF_FILE),
        "today_count": len(today_events),
        "tomorrow_count": len(tomorrow_events),
        "prep_count": len(prep_lookup),
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
            "alert": f"\U0001f437\U0001f50d morning-meeting-brief failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
