"""agents/family-calendar/tools.py — Mistress Mouse's tool manifest.

Phase B: read-only tools over cached calendar events + reminders.
Producer tools (propose_event_add / move / cancel with inline
confirmation) come in Phase C.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")
CACHE = os.path.join(WORKSPACE, "cache")
CONFIG_PATH = os.path.join(WORKSPACE, "calendar-config.json")
SENT_REMINDERS_PATH = os.path.join(WORKSPACE, "sent-reminders.json")


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _events_path_for(d: date) -> str:
    return os.path.join(CACHE, f"events-{d.isoformat()}.json")


def get_events_for_day(day: str = "today") -> dict:
    """Return the events for a given day. `day` can be 'today',
    'tomorrow', or an ISO date string 'YYYY-MM-DD'. Reads the cached
    events-YYYY-MM-DD.json files the gcal-fetch cron writes."""
    if day == "today":
        target = date.today()
    elif day == "tomorrow":
        target = date.today() + timedelta(days=1)
    else:
        try:
            target = date.fromisoformat(day)
        except ValueError:
            return {"error": f"unrecognized day: {day!r}. Use 'today', 'tomorrow', or YYYY-MM-DD."}

    data = _read_json(_events_path_for(target))
    if not data:
        return {"date": target.isoformat(), "events": [], "note": "no cache for that day"}
    return {
        "date": data.get("date"),
        "events": [
            {
                "summary": ev.get("summary"),
                "start": ev.get("start"),
                "end": ev.get("end"),
                "all_day": ev.get("all_day", False),
                "location": ev.get("location", ""),
                "calendar": ev.get("calendar_label"),
                "status": ev.get("status"),
            }
            for ev in data.get("events", [])
        ],
    }


def get_week() -> dict:
    """Return events for today + the next 6 days. Read the most
    recent 7 cached events-YYYY-MM-DD.json files that match."""
    start = date.today()
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        data = _read_json(_events_path_for(d)) or {}
        days.append({
            "date": d.isoformat(),
            "count": len(data.get("events", [])),
            "events": [
                {"summary": ev.get("summary"), "start": ev.get("start"),
                 "all_day": ev.get("all_day", False),
                 "calendar": ev.get("calendar_label")}
                for ev in data.get("events", [])
            ],
        })
    return {"start": start.isoformat(), "days": days}


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
]


EXECUTORS: dict = {
    "get_events_for_day": get_events_for_day,
    "get_week": get_week,
    "get_configured_calendars": get_configured_calendars,
    "get_recent_reminders_sent": get_recent_reminders_sent,
}
