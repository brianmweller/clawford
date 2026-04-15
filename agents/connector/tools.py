"""agents/connector/tools.py — Huckle Cat's tool manifest.

Phase B: read-only tools over the relationship-tracking state Huckle
Cat maintains. Huckle Cat's model is: mine message sources for contact
touches, track days-since-last-contact, surface overdue relationships
each morning.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

WORKSPACE = os.path.expanduser("~/.clawford/connector-workspace")
CACHE = os.path.join(WORKSPACE, "cache")
CONFIG_PATH = os.path.join(WORKSPACE, "connector-config.json")
PENDING_TRIAGE_PATH = os.path.join(WORKSPACE, "pending-triage.json")
UPCOMING_MEETINGS_PATH = os.path.join(WORKSPACE, "upcoming-meetings.json")
CHECKIN_LOG_PATH = os.path.join(WORKSPACE, "checkin-log.json")
MORNING_NUDGE_PATH = os.path.join(CACHE, "morning-nudge.txt")
LAST_NUDGE_PATH = os.path.join(CACHE, "last-morning-nudge.json")


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def get_morning_nudge() -> dict:
    """Return the most recent morning relationship nudge — overdue
    contacts, approaching contacts, ranked. This is the daily Huckle
    Cat summary the operator sees at 5 AM PT."""
    text = _read_text(MORNING_NUDGE_PATH)
    meta = _read_json(LAST_NUDGE_PATH, default={}) or {}
    if not text:
        return {"error": "no morning nudge cached yet", "meta": meta}
    return {"text": text[:4000], "meta": meta}


def get_upcoming_meetings() -> dict:
    """Return the list of upcoming meetings with people Huckle Cat is
    tracking. Used so the LLM can answer 'who am I meeting next week'
    with relationship context rather than just calendar events."""
    data = _read_json(UPCOMING_MEETINGS_PATH, default={})
    if not data:
        return {"error": "no upcoming-meetings data"}
    return data


def get_pending_triage() -> dict:
    """Return notes that are staged for triage — notes Huckle Cat has
    identified as needing the operator's review (may contain commitments,
    follow-up asks, action items buried in casual chat)."""
    data = _read_json(PENDING_TRIAGE_PATH, default={"pending": {}})
    pending = data.get("pending", {})
    return {
        "count": len(pending),
        "last_pruned": data.get("last_pruned"),
        "items": list(pending.items())[:20],
    }


def get_checkin_log(limit: int = 10) -> dict:
    """Return the most recent check-in events Huckle Cat has logged —
    relationships the operator has actively reached out to."""
    data = _read_json(CHECKIN_LOG_PATH, default={"checkins": []})
    checkins = data.get("checkins", [])
    return {
        "count": len(checkins),
        "recent": checkins[-limit:],
    }


def get_config_summary() -> dict:
    """Return a summary of Huckle Cat's config — which message sources
    are enabled, reminder cadences, etc. Without leaking secrets."""
    config = _read_json(CONFIG_PATH, default={})
    return {
        "sources": list((config.get("sources") or {}).keys()),
        "reminder_thresholds": config.get("reminder_thresholds", {}),
        "cadences": config.get("cadences", {}),
    }


TOOLS: list[dict] = [
    {
        "type": "function",
        "name": "get_morning_nudge",
        "description": (
            "Return today's relationship nudge: overdue contacts, "
            "approaching contacts, ranked by urgency. This is the "
            "headline daily Huckle Cat summary. Call for 'who should "
            "I reach out to today', 'who am I overdue with'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_upcoming_meetings",
        "description": (
            "Return the list of upcoming scheduled meetings with people "
            "Huckle Cat is tracking, with email-to-date mapping. Good "
            "for 'who am I meeting next week' queries."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_pending_triage",
        "description": (
            "Return notes staged for triage — items Huckle Cat has "
            "flagged as needing the operator's review. Empty dict means "
            "nothing in the queue."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_checkin_log",
        "description": (
            "Return the most recent relationship check-in events Huckle "
            "Cat has logged. Default 10, max 50."
        ),
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_config_summary",
        "description": (
            "Return a summary of which message sources (iMessage, "
            "WhatsApp, email, etc.) Huckle Cat is tracking and what "
            "reminder cadences are configured."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


EXECUTORS: dict = {
    "get_morning_nudge": get_morning_nudge,
    "get_upcoming_meetings": get_upcoming_meetings,
    "get_pending_triage": get_pending_triage,
    "get_checkin_log": get_checkin_log,
    "get_config_summary": get_config_summary,
}
