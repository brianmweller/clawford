"""agents/meetings-coach/tools.py — Sergeant Murphy's tool manifest.

Phase B: read-only tools over cached meeting state. Phase C will add
confirm_action_item / dismiss_action_item producer tools.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = os.path.expanduser("~/.clawford/meetings-coach-workspace")
CACHE = os.path.join(WORKSPACE, "cache")
CONFIG_PATH = os.path.join(WORKSPACE, "meeting-config.json")
LAST_COMMITMENT_PATH = os.path.join(CACHE, "last-commitment.json")
COACHING_HISTORY_PATH = os.path.join(CACHE, "coaching-history.json")


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _events_path_for(d: date) -> str:
    return os.path.join(CACHE, f"events-{d.isoformat()}.json")


def _summarize_event(ev: dict) -> dict:
    return {
        "summary": ev.get("summary"),
        "start": ev.get("start"),
        "end": ev.get("end"),
        "attendees": [
            a.get("email") if isinstance(a, dict) else str(a)
            for a in (ev.get("attendees") or [])[:10]
        ],
        "has_video": bool(ev.get("hangoutLink") or ev.get("video")),
        "location": ev.get("location", ""),
        "workflowy_node": ev.get("workflowy_node"),
    }


def get_meetings_for_day(day: str = "today") -> dict:
    """Return work meetings for a given day. `day` is 'today',
    'tomorrow', or an ISO date."""
    if day == "today":
        target = date.today()
    elif day == "tomorrow":
        target = date.today() + timedelta(days=1)
    else:
        try:
            target = date.fromisoformat(day)
        except ValueError:
            return {"error": f"unrecognized day: {day!r}"}

    data = _read_json(_events_path_for(target))
    if not data:
        return {"date": target.isoformat(), "events": [], "note": "no cache"}
    return {
        "date": data.get("date"),
        "status": data.get("status"),
        "events": [_summarize_event(e) for e in data.get("events", [])],
        "errors": data.get("errors", []),
    }


def get_week_meetings() -> dict:
    """Return meetings for today + the next 4 workdays."""
    start = date.today()
    days = []
    for i in range(5):
        d = start + timedelta(days=i)
        data = _read_json(_events_path_for(d)) or {}
        days.append({
            "date": d.isoformat(),
            "count": len(data.get("events", [])),
            "events": [_summarize_event(e) for e in data.get("events", [])],
        })
    return {"start": start.isoformat(), "days": days}


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
            "Return the work meetings for a given day. Pass 'today', "
            "'tomorrow', or a 'YYYY-MM-DD' date. Call for 'what's on "
            "my schedule', 'what meetings today'."
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
