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
import memory_writer  # type: ignore
import state_introspection  # type: ignore
from subprocess_helpers import run_json_script, is_subprocess_error  # type: ignore
from fuzzy_resolver import (  # type: ignore
    Candidate as _FuzzyCandidate,
    resolve_fuzzy_descriptor as _shared_resolve,
    result_to_dict as _result_to_dict,
)

# Put the agent dir on sys.path so we can import agent-root modules.
# dispatcher.py loads tools.py via spec_from_file_location, which doesn't
# adjust sys.path; without this shim ``from task_callback import ...``
# would fail even though the module sits right next to us.
import sys as _sys
from pathlib import Path as _Path
_AGENT_DIR = _Path(__file__).resolve().parent
if str(_AGENT_DIR) not in _sys.path:
    _sys.path.insert(0, str(_AGENT_DIR))

from task_callback import handle_task_callback  # type: ignore  # noqa: E402

AGENT_ID = "family-calendar"

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


def propose_remember(rule: str, category: str = "General") -> dict:
    return memory_writer.propose_pending_remember(AGENT_ID, rule, category)


def confirm_remember(rule: str, category: str) -> dict:
    return memory_writer.append_rule(AGENT_ID, rule, category)


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


# ---------------------------------------------------------------------------
# Event-descriptor resolver — P0 application of the shared fuzzy-
# resolver. Operators describe events ('my 3pm dentist', 'Avery's
# pickup') rather than typing GCal event_ids.
# ---------------------------------------------------------------------------


def _load_event_candidates() -> list[_FuzzyCandidate]:
    """Build an event-candidate pool from cached gcal-fetch results.
    Each candidate carries the full event dict in ``extras`` so the
    downstream tool can pull out ``source_calendar_id`` + ``id`` when
    it stages the pending action."""
    cache = Path(CACHE)
    if not cache.exists():
        return []
    today = datetime.now(
        ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
    ).date()
    candidates: list[_FuzzyCandidate] = []
    seen: set[str] = set()
    for path in sorted(cache.glob("events-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for ev in (data.get("events") or []):
            eid = ev.get("id", "")
            if not eid or eid in seen:
                continue
            start = ev.get("start", "")
            try:
                ev_date = datetime.fromisoformat(
                    start.replace("Z", "+00:00") if "T" in start else start
                ).date()
                delta = (ev_date - today).days
                if delta < -1 or delta > 14:
                    continue
            except (ValueError, TypeError):
                pass

            attendees = ev.get("attendees") or []
            names = [(a.get("name") or "").strip()
                     for a in attendees if isinstance(a, dict)]
            emails = [(a.get("email") or "").strip().lower()
                      for a in attendees if isinstance(a, dict)]
            seen.add(eid)
            candidates.append(_FuzzyCandidate(
                id=eid,
                display=ev.get("summary", "(untitled)"),
                last_seen=start,
                name=" ; ".join(n for n in names if n),
                email=" ; ".join(e for e in emails if e),
                subject=ev.get("summary", ""),
                extras=dict(ev),
            ))
    return candidates


def _resolve_event_descriptor(descriptor: str) -> dict:
    """Map a fuzzy operator descriptor to a GCal event_id + its
    calendar_id (extracted from the matched event's source_calendar_id).
    Returns the standardized {status, event_id/candidates/recent_events}
    shape; for status=ok the matched dict carries the full event."""
    pool = _load_event_candidates()
    result = _shared_resolve(
        descriptor,
        candidates=pool,
        id_pattern=r"^[A-Za-z0-9_]{10,}$",
    )

    def _event_fmt(c: _FuzzyCandidate) -> dict:
        return {
            "event_id": c.id,
            "title": (c.subject or "")[:80],
            "start": c.last_seen,
            "attendees": c.name,
            "match_reason": c.match_reason,
        }

    def _match_fmt(c: _FuzzyCandidate) -> dict:
        return {**(c.extras or {}), "match_reason": c.match_reason}

    if result.status == "ok":
        return _result_to_dict(
            result, id_key="event_id",
            recent_key="recent_events",
            candidate_formatter=_match_fmt,
        )
    return _result_to_dict(
        result, id_key="event_id",
        recent_key="recent_events",
        candidate_formatter=_event_fmt,
    )


def propose_event_move(
    event: str, new_start: str, new_end: str = "",
) -> dict:
    """Stage a move for an event identified by fuzzy descriptor. The
    resolver matches against today's + upcoming cached events by
    title, attendee name, or explicit event_id. On single match,
    stages a pending_action with the resolved event_id + its
    source_calendar_id. On ambiguity, returns candidates[] for
    LLM-driven disambiguation."""
    ref = (event or "").strip()
    if not ref:
        return {"status": "error", "error": "event is required"}
    resolution = _resolve_event_descriptor(ref)
    if resolution.get("status") != "ok":
        return {**resolution, "descriptor": ref}

    matched = resolution.get("matched") or {}
    event_id = resolution["event_id"]
    calendar_id = matched.get("source_calendar_id") or matched.get("calendar_id", "")
    if not calendar_id:
        return {"status": "error",
                "error": "resolved event has no calendar_id",
                "resolved_event_id": event_id}

    title = (matched.get("summary") or "")[:60]
    payload = {
        "calendar_id": calendar_id, "event_id": event_id,
        "new_start": new_start, "new_end": new_end,
    }
    return pending_actions.stage(
        "family-calendar", "calendar_move", payload,
        f"Move '{title}' to {new_start}",
        confirm_label="\U0001f4c5 Move it", cancel_label="Keep",
    )


def propose_event_cancel(event: str) -> dict:
    """Stage a cancel for an event identified by fuzzy descriptor.
    Same resolution semantics as propose_event_move."""
    ref = (event or "").strip()
    if not ref:
        return {"status": "error", "error": "event is required"}
    resolution = _resolve_event_descriptor(ref)
    if resolution.get("status") != "ok":
        return {**resolution, "descriptor": ref}

    matched = resolution.get("matched") or {}
    event_id = resolution["event_id"]
    calendar_id = matched.get("source_calendar_id") or matched.get("calendar_id", "")
    if not calendar_id:
        return {"status": "error",
                "error": "resolved event has no calendar_id",
                "resolved_event_id": event_id}

    title = (matched.get("summary") or "")[:60]
    payload = {"calendar_id": calendar_id, "event_id": event_id}
    return pending_actions.stage(
        "family-calendar", "calendar_cancel", payload,
        f"Cancel '{title}'" if title else f"Cancel event {event_id}",
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
            "Stage a calendar-event move for the operator's confirmation, "
            "identified by fuzzy descriptor. `event` accepts:\n"
            "  - event title substring ('dentist', 'Avery pickup')\n"
            "  - attendee name or email\n"
            "  - explicit GCal event_id\n"
            "\n"
            "Resolver scans the last 14 days of cached events. On "
            "ambiguity returns candidates[] for LLM to ask the operator to "
            "pick. Use for 'move my 3pm dentist to 4pm' or 'push "
            "Avery's pickup back an hour'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event": {
                    "type": "string",
                    "description": (
                        "Fuzzy reference — title substring, attendee "
                        "name, or event_id"
                    ),
                },
                "new_start": {"type": "string", "description": "ISO datetime for new start"},
                "new_end": {"type": "string", "description": "ISO datetime for new end (optional)"},
            },
            "required": ["event", "new_start"],
        },
    },
    {
        "type": "function",
        "name": "propose_event_cancel",
        "description": (
            "Stage a calendar-event cancellation for the operator's "
            "confirmation, identified by fuzzy descriptor. Same "
            "resolution semantics as propose_event_move. Use for "
            "'cancel the dentist appointment' or 'drop tomorrow's "
            "10am standup.'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event": {
                    "type": "string",
                    "description": (
                        "Fuzzy reference — title substring, attendee "
                        "name, or event_id"
                    ),
                },
            },
            "required": ["event"],
        },
    },
    {
        "type": "function",
        "name": "propose_remember",
        "description": (
            "Stage a rule for the operator's confirmation, to be added to your "
            "persistent MEMORY.md. the operator will see inline buttons to remember "
            "or skip. Use when the operator says 'remember that...', 'from now on...', "
            "or teaches you a new rule. Category is a short heading like "
            "'Grocery Defaults' or 'Alert Classification'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The rule to remember"},
                "category": {"type": "string", "description": "Category heading"},
            },
            "required": ["rule"],
        },
    },
    {
        "type": "function",
        "name": "get_recent_runs",
        "description": (
            "Return your own recent cron-run activity in the operator's "
            "timezone (PT). Use this BEFORE answering questions like "
            "'did you run today?', 'what did you do?', 'why didn't "
            "you X?'. Returns per-run status + summary counts. "
            "Default window is 24h."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since_hours": {"type": "integer", "default": 24},
            },
            "required": [],
        },
    },
]


def get_recent_runs(since_hours: int = 24) -> dict:
    return state_introspection.recent_runs(AGENT_ID, since_hours=since_hours)


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
    "propose_remember": propose_remember,
    "confirm_remember": confirm_remember,
    # Dispatcher shortcut — callback buttons on task reminders.
    "handle_task_callback": handle_task_callback,
    "get_recent_runs": get_recent_runs,
}
