"""agents/meetings-coach/tools.py — Sergeant Murphy's tool manifest.

Phase B: read-only tools over cached meeting state. Phase C will add
confirm_action_item / dismiss_action_item producer tools.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = os.path.expanduser("~/.clawford/meetings-coach-workspace")
CACHE = os.path.join(WORKSPACE, "cache")
CONFIG_PATH = os.path.join(WORKSPACE, "meeting-config.json")
LAST_COMMITMENT_PATH = os.path.join(CACHE, "last-commitment.json")
COACHING_HISTORY_PATH = os.path.join(CACHE, "coaching-history.json")
GCAL_FETCH_SCRIPT = os.path.join(WORKSPACE, "scripts", "gcal-fetch.py")

DEFAULT_TZ = "America/Los_Angeles"


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _user_timezone() -> ZoneInfo:
    """VPS runs UTC, the operator lives in PT. Resolve his wall-clock TZ from
    meeting-config.json so 'today' means the operator's today."""
    config = _read_json(CONFIG_PATH, default={})
    tz = (config or {}).get("timezone") or DEFAULT_TZ
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _user_today() -> date:
    return datetime.now(_user_timezone()).date()


def _run_gcal_fetch(start_date: str, days: int) -> dict:
    """Live gcal-fetch subprocess. See family-calendar/tools.py for why
    we don't trust the per-day cache files directly — they're keyed
    by fetch start date with multi-day windows inside, which mismatches
    the 'query by day' shape the LLM tools want."""
    if not os.path.exists(GCAL_FETCH_SCRIPT):
        return {"error": f"gcal-fetch.py not found at {GCAL_FETCH_SCRIPT}"}
    try:
        proc = subprocess.run(
            ["/usr/bin/python3", GCAL_FETCH_SCRIPT,
             "--date", start_date, "--days", str(days)],
            capture_output=True, text=True, timeout=45, cwd=WORKSPACE,
        )
    except subprocess.TimeoutExpired:
        return {"error": "gcal-fetch timed out after 45s"}
    except Exception as exc:
        return {"error": f"gcal-fetch subprocess failed: {exc}"}

    if proc.returncode != 0 and not proc.stdout:
        return {"error": proc.stderr.strip() or f"gcal-fetch exit {proc.returncode}"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "gcal-fetch non-JSON output", "stdout": proc.stdout[:500]}


def _summarize_event(ev: dict) -> dict:
    return {
        "summary": ev.get("summary"),
        "start": ev.get("start"),
        "end": ev.get("end"),
        "attendees": [
            (a.get("name") or a.get("email")) if isinstance(a, dict) else str(a)
            for a in (ev.get("attendees") or [])[:10]
        ],
        "has_video": bool(ev.get("conference_link") or ev.get("hangoutLink")),
        "location": ev.get("location", ""),
        "workflowy_node": ev.get("workflowy_node"),
    }


def _filter_real_meetings(events: list) -> list:
    """Sergeant Murphy's domain = real meetings only. Defined as events
    where gcal-fetch.py tagged is_real_meeting=True (attendees or video,
    not on the skip_titles list). The Mouse/Murphy boundary is enforced
    here at the tool level, not by the LLM, so the operator never sees 'Pick
    up meds' bleeding into a meetings reply."""
    return [ev for ev in events if ev.get("is_real_meeting") is True]


def _filter_events_on_date(events: list, target: date) -> list:
    target_iso = target.isoformat()
    return [ev for ev in events if (ev.get("start") or "").startswith(target_iso)]


def get_meetings_for_day(day: str = "today") -> dict:
    """Return work meetings for a given day. `day` is 'today',
    'tomorrow', or an ISO date."""
    if day == "today":
        target = _user_today()
    elif day == "tomorrow":
        target = _user_today() + timedelta(days=1)
    else:
        try:
            target = date.fromisoformat(day)
        except ValueError:
            return {"error": f"unrecognized day: {day!r}"}

    data = _run_gcal_fetch(target.isoformat(), 1)
    if data.get("error"):
        return {"date": target.isoformat(), "meetings": [], "error": data["error"]}
    real = _filter_real_meetings(data.get("events", []))
    return {
        "date": target.isoformat(),
        "status": data.get("status"),
        "meetings": [_summarize_event(e) for e in real],
        "errors": data.get("errors", []),
    }


def get_week_meetings() -> dict:
    """Return meetings for today + the next 4 workdays via one live
    gcal-fetch --days 5 call, then grouped by date."""
    start = _user_today()
    data = _run_gcal_fetch(start.isoformat(), 5)
    if data.get("error"):
        return {"start": start.isoformat(), "days": [], "error": data["error"]}

    raw = _filter_real_meetings(data.get("events", []))
    days = []
    for i in range(5):
        d = start + timedelta(days=i)
        day_events = _filter_events_on_date(raw, d)
        days.append({
            "date": d.isoformat(),
            "count": len(day_events),
            "meetings": [_summarize_event(e) for e in day_events],
        })
    return {
        "start": start.isoformat(),
        "status": data.get("status"),
        "days": days,
        "errors": data.get("errors", []),
    }


def get_commitment_status() -> dict:
    """Return the current open-commitment summary: overdue, approaching
    due dates, total open. Commitment tracking runs daily via the
    commitment-follow-up cron."""
    data = _read_json(LAST_COMMITMENT_PATH)
    if not data:
        return {"error": "no commitment snapshot found"}
    return data


def get_coaching_config() -> dict:
    """Return the growth areas Sergeant Murphy is coaching on — the
    list of labeled behaviors he watches for in meeting transcripts."""
    config = _read_json(CONFIG_PATH, default={})
    coaching = config.get("coaching", {}) or {}
    return {
        "enabled": coaching.get("enabled", False),
        "growth_areas": [
            {"id": ga.get("id"), "label": ga.get("label")}
            for ga in coaching.get("growth_areas", [])
        ],
    }


def get_recent_coaching_entries(limit: int = 5) -> dict:
    """Return the most recent coaching entries — per-meeting analysis
    of the operator's behavior on one or more growth areas."""
    data = _read_json(COACHING_HISTORY_PATH, default=[])
    if not isinstance(data, list):
        data = data.get("entries", []) if isinstance(data, dict) else []
    return {"count": len(data), "entries": data[-limit:]}


TOOLS: list[dict] = [
    {
        "type": "function",
        "name": "get_meetings_for_day",
        "description": (
            "Return real work meetings for a given day — events with "
            "attendees or a video link, not task blocks / appointments / "
            "reminders. Pass 'today', 'tomorrow', or a 'YYYY-MM-DD' "
            "date. Call for 'what's on my schedule', 'what meetings "
            "today'. Non-meeting events (dentist, errands, focus time) "
            "live in Mistress Mouse's domain — don't report them here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "day": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_week_meetings",
        "description": (
            "Return meetings across today + the next 4 workdays. Call "
            "for 'what does the week look like' or 'am I busy this week'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_commitment_status",
        "description": (
            "Return the current summary of open meeting commitments: "
            "how many are overdue, approaching, and total. Call when "
            "the operator asks 'what do I owe people' or 'anything overdue'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_coaching_config",
        "description": (
            "Return the list of growth areas Sergeant Murphy is coaching "
            "on. Useful when the operator asks 'what are you watching for' or "
            "'what are my focus areas'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_recent_coaching_entries",
        "description": (
            "Return the last N coaching entries — per-meeting analysis "
            "of behavior on growth areas. Default 5, max 20."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
]


EXECUTORS: dict = {
    "get_meetings_for_day": get_meetings_for_day,
    "get_week_meetings": get_week_meetings,
    "get_commitment_status": get_commitment_status,
    "get_coaching_config": get_coaching_config,
    "get_recent_coaching_entries": get_recent_coaching_entries,
}
