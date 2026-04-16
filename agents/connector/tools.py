"""agents/connector/tools.py — Huckle Cat's tool manifest.

Phase B: read-only tools over the relationship-tracking state.
Phase C: mark_checkin and snooze_reminder for manual relationship management.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memory_writer  # type: ignore

AGENT_ID = "connector"

WORKSPACE = os.path.expanduser("~/.clawford/connector-workspace")
CACHE = os.path.join(WORKSPACE, "cache")
CONFIG_PATH = os.path.join(WORKSPACE, "connector-config.json")
PENDING_TRIAGE_PATH = os.path.join(WORKSPACE, "pending-triage.json")
UPCOMING_MEETINGS_PATH = os.path.join(WORKSPACE, "upcoming-meetings.json")
CHECKIN_LOG_PATH = os.path.join(WORKSPACE, "checkin-log.json")
MORNING_NUDGE_PATH = os.path.join(CACHE, "morning-nudge.txt")
LAST_NUDGE_PATH = os.path.join(CACHE, "last-morning-nudge.json")
SNOOZES_PATH = os.path.join(WORKSPACE, "snoozes.json")

# Default durations (days) for the three nudge button actions. done
# and snooze map to explicit durations; ignore is a long default so
# the person resurfaces only if they remain overdue for a year.
NUDGE_ACTION_DAYS = {"done": 30, "snoozed": 30, "ignored": 365}


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


# ---------------------------------------------------------------------------
# Phase C producer tools
# ---------------------------------------------------------------------------


def propose_remember(rule: str, category: str = "General") -> dict:
    return memory_writer.propose_pending_remember(AGENT_ID, rule, category)


def confirm_remember(rule: str, category: str) -> dict:
    return memory_writer.append_rule(AGENT_ID, rule, category)


def mark_checkin(person_name: str) -> dict:
    """Record a manual check-in for a relationship contact."""
    data = _read_json(CHECKIN_LOG_PATH, default={"checkins": []})
    if not isinstance(data.get("checkins"), list):
        data["checkins"] = []

    entry = {
        "person": person_name,
        "checked_in_at": datetime.now(timezone.utc).isoformat(),
        "source": "manual",
    }
    data["checkins"].append(entry)

    with open(CHECKIN_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return {"status": "ok", "person": person_name, "logged_at": entry["checked_in_at"]}


def snooze_reminder(person_name: str, days: int = 7) -> dict:
    """Push the next overdue reminder out by N days for a given person."""
    config = _read_json(CONFIG_PATH, default={})
    snoozes = config.setdefault("snoozes", {})
    snoozes[person_name.lower()] = {
        "until": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
        "snoozed_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    return {
        "status": "ok", "person": person_name,
        "snoozed_for_days": days,
        "until": snoozes[person_name.lower()]["until"],
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
    {
        "type": "function",
        "name": "mark_checkin",
        "description": (
            "Record that the operator has checked in with someone. Logs the "
            "contact in checkin-log.json and resets the overdue timer. "
            "Call when the operator says 'I talked to John today' or 'just "
            "caught up with Sarah'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person_name": {"type": "string", "description": "Name of the person"},
            },
            "required": ["person_name"],
        },
    },
    {
        "type": "function",
        "name": "snooze_reminder",
        "description": (
            "Snooze the overdue reminder for a person by N days. Call "
            "when the operator says 'snooze John' or 'remind me about Sarah "
            "next week instead'. Default 7 days."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person_name": {"type": "string"},
                "days": {"type": "integer", "description": "Days to snooze (default 7)"},
            },
            "required": ["person_name"],
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
]


def handle_nudge_action(slug: str, action: str) -> dict:
    """Record a button press from the morning Relationship Check.

    action ∈ {done, snoozed, ignored}. Writes/updates snoozes.json —
    people-scan.py reads this file and skips any slug whose until
    date is still in the future.

    Returns a short summary dict suitable for the Telegram toast.
    """
    from datetime import date

    action = (action or "").strip().lower()
    if action not in NUDGE_ACTION_DAYS:
        return {"status": "error", "error": f"unknown action: {action!r}"}

    slug = (slug or "").strip()
    if not slug:
        return {"status": "error", "error": "empty slug"}

    data = _read_json(SNOOZES_PATH, default={}) or {}
    if not isinstance(data, dict):
        data = {}

    days = NUDGE_ACTION_DAYS[action]
    today = date.today()
    until = (today + timedelta(days=days)).isoformat()
    data[slug] = {
        "status": action,
        "until": until,
        "set_at": datetime.now(timezone.utc).isoformat(),
    }

    # Atomic write so concurrent presses don't truncate the file.
    tmp = SNOOZES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SNOOZES_PATH)

    return {
        "status": "ok", "slug": slug, "action": action,
        "until": until, "days": days,
    }


EXECUTORS: dict = {
    "get_morning_nudge": get_morning_nudge,
    "get_upcoming_meetings": get_upcoming_meetings,
    "get_pending_triage": get_pending_triage,
    "get_checkin_log": get_checkin_log,
    "get_config_summary": get_config_summary,
    "mark_checkin": mark_checkin,
    "snooze_reminder": snooze_reminder,
    "propose_remember": propose_remember,
    "confirm_remember": confirm_remember,
    "handle_nudge_action": handle_nudge_action,
}
