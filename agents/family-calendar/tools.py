"""agents/family-calendar/tools.py — Mistress Mouse's tool manifest.

Phase B: read-only tools over cached calendar events + reminders.
Phase C: producer tools — propose_event_add / move / cancel with
inline confirmation buttons.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pending_actions  # type: ignore
from subprocess_helpers import run_json_script, is_subprocess_error  # type: ignore

WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")
CACHE = os.path.join(WORKSPACE, "cache")
CONFIG_PATH = os.path.join(WORKSPACE, "calendar-config.json")
SENT_REMINDERS_PATH = os.path.join(WORKSPACE, "sent-reminders.json")
GCAL_FETCH_SCRIPT = os.path.join(WORKSPACE, "scripts", "gcal-fetch.py")

DEFAULT_TZ = "America/Los_Angeles"


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _user_timezone() -> ZoneInfo:
    """Resolve the user's timezone from calendar-config.json. Falls
    back to Pacific. The VPS runs UTC but the operator lives in PT, so date
    arithmetic like 'today' must be in the operator's wall clock, not UTC."""
    config = _read_json(CONFIG_PATH, default={})
    tz = (config or {}).get("timezone") or DEFAULT_TZ
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _user_today() -> date:
    return datetime.now(_user_timezone()).date()


def _run_gcal_fetch(start_date: str, days: int) -> dict:
    """Invoke the existing gcal-fetch.py script as subprocess and parse
    its JSON stdout. Passes --skip-meetings so Mistress Mouse's view
    EXCLUDES anything linked to Sergeant Murphy's Workflowy prep notes
    — the Mouse↔Murphy boundary is enforced at fetch time, not by
    the LLM. Events without a Workflowy link stay in, events with one
    get routed to Murphy only."""
    if not os.path.exists(GCAL_FETCH_SCRIPT):
        return {"error": f"gcal-fetch.py not found at {GCAL_FETCH_SCRIPT}"}
    try:
        proc = subprocess.run(
            ["/usr/bin/python3", GCAL_FETCH_SCRIPT,
             "--date", start_date, "--days", str(days), "--skip-meetings"],
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
        return {"error": "gcal-fetch produced non-JSON output", "stdout": proc.stdout[:500]}


def _summarize_events(events: list) -> list:
    return [
        {
            "summary": ev.get("summary"),
            "start": ev.get("start"),
            "end": ev.get("end"),
            "all_day": ev.get("all_day", False),
            "location": ev.get("location", ""),
            "calendar": ev.get("calendar_label"),
            "status": ev.get("status"),
        }
        for ev in events
    ]


def _filter_events_on_date(events: list, target: date) -> list:
    """Filter events whose start date equals target (local day)."""
    result = []
    target_iso = target.isoformat()
    for ev in events:
        start = ev.get("start") or ""
        # Event starts can be date-only (all-day) or datetime with tz.
        # Compare the date portion.
        if start.startswith(target_iso):
            result.append(ev)
    return result


def get_events_for_day(day: str = "today") -> dict:
    """Return the events for a given day via live gcal-fetch subprocess.
    `day` is 'today', 'tomorrow', or an ISO 'YYYY-MM-DD' date."""
    if day == "today":
        target = _user_today()
    elif day == "tomorrow":
        target = _user_today() + timedelta(days=1)
    else:
        try:
            target = date.fromisoformat(day)
        except ValueError:
            return {"error": f"unrecognized day: {day!r}. Use 'today', 'tomorrow', or YYYY-MM-DD."}

    data = _run_gcal_fetch(target.isoformat(), 1)
    if data.get("error"):
        return {"date": target.isoformat(), "events": [], "error": data["error"]}

    raw_events = data.get("events", [])
    return {
        "date": target.isoformat(),
        "status": data.get("status"),
        "events": _summarize_events(raw_events),
        "errors": data.get("errors", []),
    }


def get_week() -> dict:
    """Return events for today + the next 6 days via one live gcal-fetch
    call with --days 7. Groups events by date."""
    start = _user_today()
    data = _run_gcal_fetch(start.isoformat(), 7)
    if data.get("error"):
        return {"start": start.isoformat(), "days": [], "error": data["error"]}

    raw_events = data.get("events", [])
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        day_events = _filter_events_on_date(raw_events, d)
        days.append({
            "date": d.isoformat(),
            "count": len(day_events),
            "events": _summarize_events(day_events),
        })
    return {
        "start": start.isoformat(),
        "status": data.get("status"),
        "days": days,
        "errors": data.get("errors", []),
    }


def get_configured_calendars() -> dict:
    """Return the list of Google Calendars Mistress Mouse is watching
    — useful when the operator asks which family members are tracked."""
    config = _read_json(CONFIG_PATH, default={})
    return {
        "calendars": config.get("calendars", []),
        "timezone": config.get("timezone"),
    }


def get_recent_reminders_sent(limit: int = 10) -> dict:
    """Return the last N reminders Mistress Mouse has already pushed
    to Telegram, so the operator can see what she's been flagging."""
    sent = _read_json(SENT_REMINDERS_PATH, default={})
    if isinstance(sent, dict):
        items = list(sent.items())
    elif isinstance(sent, list):
        items = [(str(i), v) for i, v in enumerate(sent)]
    else:
        items = []
    items = items[-limit:]
    return {"count": len(items), "reminders": items}


GCAL_WRITE_SCRIPT = os.path.join(WORKSPACE, "scripts", "gcal-write.py")


# ---------------------------------------------------------------------------
# Phase C producer tools
# ---------------------------------------------------------------------------


def propose_event_add(
    calendar_id: str, summary: str, start: str,
    end: str = "", location: str = "",
) -> dict:
    payload = {
        "calendar_id": calendar_id, "summary": summary,
        "start": start, "end": end, "location": location,
    }
    return pending_actions.stage(
        "family-calendar", "calendar_add", payload,
        f"Create '{summary}' on {start}",
        confirm_label="\U0001f4c5 Create event", cancel_label="Skip",
    )


def propose_event_move(
    calendar_id: str, event_id: str,
    new_start: str, new_end: str = "",
) -> dict:
    payload = {
        "calendar_id": calendar_id, "event_id": event_id,
        "new_start": new_start, "new_end": new_end,
    }
    return pending_actions.stage(
        "family-calendar", "calendar_move", payload,
        f"Move event to {new_start}",
        confirm_label="\U0001f4c5 Move it", cancel_label="Keep",
    )


def propose_event_cancel(calendar_id: str, event_id: str) -> dict:
    payload = {"calendar_id": calendar_id, "event_id": event_id}
    return pending_actions.stage(
        "family-calendar", "calendar_cancel", payload,
        f"Cancel event {event_id}",
        confirm_label="\U0001f4c5 Cancel event", cancel_label="Keep",
    )


def confirm_calendar_add(
    calendar_id: str, summary: str, start: str,
    end: str = "", location: str = "",
) -> dict:
    args = ["create", "--calendar-id", calendar_id,
            "--summary", summary, "--start", start, "--confirm"]
    if end:
        args.extend(["--end", end])
    if location:
        args.extend(["--location", location])
    result = run_json_script(GCAL_WRITE_SCRIPT, *args)
    if is_subprocess_error(result):
        return {"status": "error", "error": result["__error__"]}
    return result


def confirm_calendar_move(
    calendar_id: str, event_id: str,
    new_start: str, new_end: str = "",
) -> dict:
    args = ["move", "--calendar-id", calendar_id,
            "--event-id", event_id, "--new-start", new_start, "--confirm"]
    if new_end:
        args.extend(["--new-end", new_end])
    result = run_json_script(GCAL_WRITE_SCRIPT, *args)
    if is_subprocess_error(result):
        return {"status": "error", "error": result["__error__"]}
    return result


def confirm_calendar_cancel(calendar_id: str, event_id: str) -> dict:
    result = run_json_script(
        GCAL_WRITE_SCRIPT, "remove",
        "--calendar-id", calendar_id, "--event-id", event_id, "--confirm",
    )
    if is_subprocess_error(result):
        return {"status": "error", "error": result["__error__"]}
    return result


TOOLS: list[dict] = [
    {
        "type": "function",
        "name": "get_events_for_day",
        "description": (
            "Return the calendar events for a given day. Pass 'today', "
            "'tomorrow', or a YYYY-MM-DD date. Call this for "
            "'what's on my calendar', 'what does Avery have today', "
            "'anything on Friday', etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "One of 'today', 'tomorrow', or an ISO date 'YYYY-MM-DD'."
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_week",
        "description": (
            "Return an overview of events across today + the next 6 "
            "days. Call this for 'show me the week', 'what does this "
            "week look like', etc."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_configured_calendars",
        "description": (
            "Return the list of Google Calendars being watched, plus "
            "the timezone. Use when the operator asks which family members "
            "are on the calendar feed."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_recent_reminders_sent",
        "description": (
            "Return the last N reminders Mistress Mouse has already "
            "pushed. Useful for 'what did you remind me about today' "
            "or 'did I miss a reminder'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                           "description": "How many to return. Default 10, max 50."},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "propose_event_add",
        "description": (
            "Stage a new calendar event for the operator's confirmation. "
            "the operator will see inline buttons to create or skip. "
            "Use after confirming the details conversationally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "description": "Calendar label or email"},
                "summary": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "ISO datetime for start"},
                "end": {"type": "string", "description": "ISO datetime for end (optional)"},
                "location": {"type": "string", "description": "Location (optional)"},
            },
            "required": ["calendar_id", "summary", "start"],
        },
    },
    {
        "type": "function",
        "name": "propose_event_move",
        "description": (
            "Stage a calendar event move for the operator's confirmation. "
            "Requires the calendar_id and event_id from a prior "
            "get_events_for_day or get_week call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string"},
                "event_id": {"type": "string", "description": "Google Calendar event ID"},
                "new_start": {"type": "string", "description": "ISO datetime for new start"},
                "new_end": {"type": "string", "description": "ISO datetime for new end (optional)"},
            },
            "required": ["calendar_id", "event_id", "new_start"],
        },
    },
    {
        "type": "function",
        "name": "propose_event_cancel",
        "description": (
            "Stage a calendar event cancellation for the operator's confirmation. "
            "Requires the calendar_id and event_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string"},
                "event_id": {"type": "string"},
            },
            "required": ["calendar_id", "event_id"],
        },
    },
]


EXECUTORS: dict = {
    "get_events_for_day": get_events_for_day,
    "get_week": get_week,
    "get_configured_calendars": get_configured_calendars,
    "get_recent_reminders_sent": get_recent_reminders_sent,
    "propose_event_add": propose_event_add,
    "propose_event_move": propose_event_move,
    "propose_event_cancel": propose_event_cancel,
    # Shortcut-only executors (not LLM-callable)
    "confirm_calendar_add": confirm_calendar_add,
    "confirm_calendar_move": confirm_calendar_move,
    "confirm_calendar_cancel": confirm_calendar_cancel,
}
