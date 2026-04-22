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

import brain  # type: ignore
import memory_writer  # type: ignore
import pending_actions  # type: ignore
import state_introspection  # type: ignore
import subprocess_helpers  # type: ignore

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
PENDING_REVIEW_QUEUE_PATH = os.path.join(CACHE, "pending-review-queue.jsonl")

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


def get_person(name_or_slug: str) -> dict:
    """Look up a person file by name or slug. Returns the parsed frontmatter
    plus the raw markdown so the LLM can surface any field the operator asks
    about (tone, last_interaction, circles, birthday, etc.)."""
    hit = brain.get_person(name_or_slug)
    if hit is None:
        return {"status": "not_found", "query": name_or_slug}
    return {
        "status": "found",
        "slug": hit["slug"],
        "name": hit["name"],
        "fields": hit["fields"],
        "raw": hit["raw"],
    }


_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
_COMMITMENT_SCAN = str(_SCRIPTS_DIR / "commitment-scan.py")
_PEOPLE_SCAN = str(_SCRIPTS_DIR / "people-scan.py")
_NOTES_TRIAGE = str(_SCRIPTS_DIR / "notes-triage.py")
_DRAFT_COMPOSE = str(_SCRIPTS_DIR / "draft-compose.py")


def get_commitments() -> dict:
    """Return the unified open-commitment view across all agents by shelling
    out to scripts/commitment-scan.py. The script aggregates from
    ~/Dropbox/openclaw-backup/commitments/active.md and enriches with
    overdue / approaching flags."""
    result = subprocess_helpers.run_json_script(_COMMITMENT_SCAN, timeout=30)
    if subprocess_helpers.is_subprocess_error(result):
        return {"status": "error", "error": result.get("__error__", "script error")}
    return result


def force_nudge() -> dict:
    """Run people-scan.py on demand and return the overdue/approaching
    breakdown. Used for `/nudge` — the LLM then renders the scan data
    into a human-readable summary. Does NOT overwrite the 5 AM PT
    morning-brief-ready.txt delivery file."""
    result = subprocess_helpers.run_json_script(_PEOPLE_SCAN, timeout=60)
    if subprocess_helpers.is_subprocess_error(result):
        return {"status": "error", "error": result.get("__error__", "script error")}
    # people-scan.py returns {overdue, approaching, healthy, summary}
    return {"status": "ok", **result}


def draft_reply(name: str, inbound_text: str | None = None) -> dict:
    """Generate a draft message for a person: check-in if no inbound_text,
    reply otherwise. Shells out to scripts/draft-compose.py which loads the
    recipient's facts, filters by audience, composes voice guidance, and
    asks the LLM for a draft.

    The draft is for the operator to review — nothing is sent."""
    name = (name or "").strip()
    if not name:
        return {"status": "error", "error": "name is required"}

    args = ["--recipient", name]
    if inbound_text:
        args.extend(["--inbound-text", inbound_text])

    result = subprocess_helpers.run_json_script(_DRAFT_COMPOSE, *args, timeout=120)
    if subprocess_helpers.is_subprocess_error(result):
        return {"status": "error", "error": result.get("__error__", "script error")}
    return result


def force_triage() -> dict:
    """Run notes-triage.py on demand and return untriaged notes. Used
    for `/triage`. The LLM then classifies each entry and stages them
    for confirmation."""
    result = subprocess_helpers.run_json_script(_NOTES_TRIAGE, timeout=30)
    if subprocess_helpers.is_subprocess_error(result):
        return {"status": "error", "error": result.get("__error__", "script error")}
    return {"status": "ok", **result}


def propose_add_note(text: str, triaged: bool = False) -> dict:
    """Stage a note for the operator's confirmation. On /confirm the note is
    appended to notes/inbox.md with timestamp + triaged flag."""
    text = (text or "").strip()
    if not text:
        return {"status": "error", "error": "empty note text"}
    return pending_actions.stage(
        AGENT_ID,
        "add_note",
        {"text": text, "triaged": bool(triaged)},
        f"Add note: {text[:80]}{'…' if len(text) > 80 else ''}",
    )


def confirm_add_note(text: str, triaged: bool = False) -> dict:
    """Append the staged note to notes/inbox.md (executor for
    add_note pending action)."""
    result = brain.append_inbox_note(AGENT_ID, text, triaged=bool(triaged))
    return {"status": "ok", "id": result["id"], "path": result["path"]}


def propose_add_person(name: str, circle: str, **extras: str) -> dict:
    """Stage the creation of a new person file. On /confirm a markdown
    file is written under people/<slug>.md with the given frontmatter."""
    name = (name or "").strip()
    circle = (circle or "").strip()
    if not name or not circle:
        return {"status": "error", "error": "name and circle are required"}
    payload = {"name": name, "circle": circle, **extras}
    return pending_actions.stage(
        AGENT_ID,
        "add_person",
        payload,
        f"Create person '{name}' in circle '{circle}'",
    )


def confirm_add_person(name: str, circle: str, **extras: str) -> dict:
    """Create the person file (executor for add_person pending action)."""
    try:
        result = brain.create_person_file(name, circle, **extras)
    except FileExistsError as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok", "slug": result["slug"], "path": result["path"]}


def dismiss_triage_n(n: int) -> dict:
    """Remove the N-th item (1-indexed) from pending-triage.json. Used when
    the operator types `/dismiss 2` to skip a staged triage item without committing
    it to the brain."""
    try:
        index = int(n) - 1
    except (TypeError, ValueError):
        return {"status": "error", "error": f"not an integer: {n!r}"}

    data = _read_json(PENDING_TRIAGE_PATH, default=None)
    if not data or not isinstance(data, dict):
        return {"status": "error", "error": "no pending triage items"}
    items = data.get("items", [])
    if not items:
        return {"status": "error", "error": "no pending triage items"}
    if index < 0 or index >= len(items):
        return {
            "status": "error",
            "error": f"index out of range: {n} (have {len(items)})",
        }

    dismissed = items.pop(index)
    data["items"] = items
    tmp = PENDING_TRIAGE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PENDING_TRIAGE_PATH)
    return {"status": "ok", "dismissed": dismissed, "remaining": len(items)}


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
    {
        "type": "function",
        "name": "get_person",
        "description": (
            "Look up a person file by name or slug. Returns the parsed "
            "frontmatter (circles, tone, last_interaction, email, etc.) "
            "plus the raw markdown. Call for `/people [name]` and whenever "
            "the operator asks about a specific contact by name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name_or_slug": {
                    "type": "string",
                    "description": "Display name (e.g. 'Priya Rivera') or slug ('priya-rivera')",
                },
            },
            "required": ["name_or_slug"],
        },
    },
    {
        "type": "function",
        "name": "get_commitments",
        "description": (
            "Return the unified open-commitment view across ALL agents "
            "(connector, meetings-coach, shopping, family-calendar, etc.), "
            "enriched with overdue and approaching flags. Use for "
            "`/commitments`, 'what do I owe people', 'what's overdue'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "dismiss_triage_n",
        "description": (
            "Skip the N-th item (1-indexed) from the pending triage queue "
            "without committing to the shared brain. Use when the operator types "
            "`/dismiss 2` in response to a triage prompt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "1-indexed item number"},
            },
            "required": ["n"],
        },
    },
    {
        "type": "function",
        "name": "reply_to_thread",
        "description": (
            "Draft a reply for a specific Gmail thread on demand. Runs "
            "the same triage + compose pipeline as the auto-compose "
            "cron (handles both known senders and cold-recruiter "
            "inbounds, produces a staged Gmail draft) — but forces "
            "immediate processing instead of waiting for the half-hour "
            "cron tick. Use when the operator says '/reply <thread_id>' or "
            "'draft a reply to that thread now' or 'compose something "
            "for <thread_id>'. Returns the triage classification + the "
            "compose result including gmail_draft_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Gmail thread ID to reply to",
                },
            },
            "required": ["thread_id"],
        },
    },
    {
        "type": "function",
        "name": "promote_recruiter_by_thread_id",
        "description": (
            "Promote a cold-recruiter queue entry to a real people/<slug>.md "
            "file. The normal path is the ✅ Promote button on the auto-compose "
            "FYI message; use this tool only when the operator types `/promote "
            "<thread_id>` manually (e.g., because he dismissed the button or "
            "is following up via chat). Returns {status, slug, name, path} on "
            "success or {status, detail} on error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Gmail thread ID from the cold-recruiter queue entry",
                },
            },
            "required": ["thread_id"],
        },
    },
    {
        "type": "function",
        "name": "draft_reply",
        "description": (
            "Generate a draft message for a person. With no inbound text, "
            "composes a check-in — uses their context_notes, tone, visible "
            "facts, and recent interactions to write something natural-sounding. "
            "With inbound_text, drafts a reply to that message. The draft is "
            "for the operator to review and edit — nothing is sent. Use for `/draft "
            "[name]` or when the operator asks 'help me write back to X'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Recipient name or slug"},
                "inbound_text": {
                    "type": "string",
                    "description": "The message the operator is replying to (omit for a fresh check-in)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "type": "function",
        "name": "force_nudge",
        "description": (
            "Run the relationship scan on demand and return the overdue / "
            "approaching / healthy breakdown. Use for `/nudge` — then render "
            "a human-readable summary (same style as the morning nudge). "
            "Does NOT overwrite the morning-brief-ready.txt delivery file."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "force_triage",
        "description": (
            "Run notes triage on demand, returning untriaged notes from "
            "inbox.md. Use for `/triage`. Then classify each entry and "
            "stage a confirmation for the operator."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "propose_add_note",
        "description": (
            "Stage a new note for the operator's confirmation. Use when the operator says "
            "'add a note: ...', 'remind me to ...', or types `/note [text]`. "
            "The note is only written to inbox.md after the operator taps Confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note content"},
                "triaged": {"type": "boolean", "description": "Pre-mark as triaged (default false)"},
            },
            "required": ["text"],
        },
    },
    {
        "type": "function",
        "name": "propose_add_person",
        "description": (
            "Stage the creation of a new person file. Use when the operator says "
            "'add [name] to my close friends', 'start tracking Sarah', or "
            "types `/add [name] [circle]`. Circle is one of: family-inner, "
            "family-extended, close, friends, work, professional, acquaintance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name (e.g. 'Sarah Chen')"},
                "circle": {"type": "string", "description": "Circle tag"},
                "tone": {"type": "string", "description": "Optional tone (warm, professional, etc.)"},
                "email": {"type": "string", "description": "Optional email address"},
            },
            "required": ["name", "circle"],
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


def handle_nudge_action(slug: str, action: str) -> dict:
    """Record a button press from the morning Relationship Check.

    action ∈ {done, snoozed, ignored}. Writes/updates snoozes.json —
    people-scan.py reads this file and skips any slug whose until
    date is still in the future.

    For action=done, also stamps the person file's last_interaction
    to today (max-merged against any existing date). This means
    pressing ✅ done actually resets the cadence clock, not just the
    snooze window — essential because gmessages-mine only catches
    Google Messages interactions, so email/Slack/IRL contacts are
    invisible without this explicit-signal path.

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

    # ✅ done also bumps last_interaction. Best-effort — a missing
    # person file, a transient Dropbox EROFS, or a fresher existing
    # date should never break the snooze write above.
    last_interaction_bumped = False
    if action == "done":
        last_interaction_bumped = _bump_last_interaction_for_slug(slug, today.isoformat())

    return {
        "status": "ok", "slug": slug, "action": action,
        "until": until, "days": days,
        "last_interaction_bumped": last_interaction_bumped,
    }


def _bump_last_interaction_for_slug(slug: str, date_iso: str) -> bool:
    """Stamp ``people/<slug>.md`` with last_interaction=date_iso,
    max-merged so a fresher existing date isn't rewound. Returns
    True on a successful stamp, False otherwise (missing file,
    max-merge decline, OSError). Never raises — the caller's
    primary job (writing snoozes.json) already succeeded."""
    try:
        # daily_refresh owns the max-merge + atomic-write contract.
        # Import at call-time because daily-refresh.py has a dashed
        # filename and resolving the module once at import time
        # adds workspace complexity we don't need here.
        import importlib.util
        scripts_dir = Path(__file__).parent / "scripts"
        path = scripts_dir / "daily-refresh.py"
        if not path.exists():
            return False
        spec = importlib.util.spec_from_file_location("_huckle_daily_refresh", path)
        if spec is None or spec.loader is None:
            return False
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        fp = brain.dropbox_brain_root() / "people" / f"{slug}.md"
        if not fp.exists():
            return False
        return bool(mod.update_last_interaction(fp, date_iso))
    except OSError:
        return False
    except Exception:
        # Bumping last_interaction is a best-effort side channel;
        # a pathological import or parse failure should never cause
        # the button press itself to fail.
        return False


def _handle_facts_callback(action: str, arg: str) -> dict:
    """Executor wrapper for the shared dispatcher's facts:* callbacks.
    Resolves the connector's facts_dir + queue path at call time so the
    dispatcher doesn't need to know them."""
    import importlib.util
    scripts_dir = Path(__file__).parent / "scripts"
    spec = importlib.util.spec_from_file_location(
        "_facts_callback_lib", scripts_dir / "facts_callback_lib.py",
    )
    if spec is None or spec.loader is None:
        return {"status": "error", "action": action, "detail": "import failure"}
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    facts_dir = brain.dropbox_brain_root() / "facts"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return mod.handle_facts_callback(
        action, arg,
        facts_dir=facts_dir,
        queue_path=Path(PENDING_REVIEW_QUEUE_PATH),
        now_iso=now_iso,
    )


def _handle_recruiter_callback(action: str, arg: str) -> dict:
    """Executor wrapper for the shared dispatcher's recruiter:* callbacks.
    Resolves connector-scoped paths (people_dir, workspace_dir, queue)
    at call time."""
    import importlib.util
    scripts_dir = Path(__file__).parent / "scripts"
    spec = importlib.util.spec_from_file_location(
        "_recruiter_callback_lib", scripts_dir / "recruiter_callback_lib.py",
    )
    if spec is None or spec.loader is None:
        return {"status": "error", "action": action, "detail": "import failure"}
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    people_dir = brain.dropbox_brain_root() / "people"
    workspace_dir = Path(WORKSPACE)
    queue_path = workspace_dir / "cache" / "triage-queue.json"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return mod.handle_recruiter_callback(
        action, arg,
        people_dir=people_dir,
        workspace_dir=workspace_dir,
        queue_path=queue_path,
        now_iso=now_iso,
    )


def promote_recruiter_by_thread_id(thread_id: str) -> dict:
    """Manual fallback when the Telegram button is unavailable (e.g., the
    cold-recruiter FYI was dismissed or the user is replying via chat).
    Runs the same promote flow the recruiter:promote callback runs."""
    return _handle_recruiter_callback("promote", thread_id)


def reply_to_thread(thread_id: str) -> dict:
    """Operator-invoked draft composition for a specific Gmail thread.
    Runs the same pipeline the auto-compose cron does — single-thread
    triage (adds the thread to the queue + classifies known/cold), then
    forces a compose on that thread (bypassing the processed-log skip).
    Produces a Gmail draft the operator can open + send.

    Use when the operator says '/reply <thread_id>' or 'draft a reply to that
    thread now' — i.e., he doesn't want to wait for the half-hour
    auto-compose cron tick.
    """
    tid = (thread_id or "").strip()
    if not tid:
        return {"status": "error", "error": "thread_id is required"}

    scripts_dir = Path(__file__).parent / "scripts"
    inbox_triage = str(scripts_dir / "inbox-triage.py")
    auto_compose = str(scripts_dir / "auto-compose.py")

    # Step 1: classify + upsert into triage queue (handles both known
    # senders and cold-recruiter routing).
    triage = subprocess_helpers.run_json_script(
        inbox_triage, "--thread-id", tid, timeout=60,
    )
    if subprocess_helpers.is_subprocess_error(triage):
        return {"status": "error", "stage": "triage",
                "error": triage.get("__error__", "triage script error")}

    # Step 2: force compose on that specific thread.
    compose = subprocess_helpers.run_json_script(
        auto_compose, "--force", tid, "--max", "1", timeout=180,
    )
    if subprocess_helpers.is_subprocess_error(compose):
        return {"status": "error", "stage": "compose",
                "error": compose.get("__error__", "compose script error"),
                "triage_classification": triage.get("classification")}

    return {
        "status": "ok",
        "thread_id": tid,
        "triage_classification": triage.get("classification"),
        "compose": compose,
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
    "handle_facts_callback": lambda action, arg: _handle_facts_callback(action, arg),
    "handle_recruiter_callback": lambda action, arg: _handle_recruiter_callback(action, arg),
    "promote_recruiter_by_thread_id": promote_recruiter_by_thread_id,
    "reply_to_thread": reply_to_thread,
    "get_person": get_person,
    "get_commitments": get_commitments,
    "dismiss_triage_n": dismiss_triage_n,
    "propose_add_note": propose_add_note,
    "confirm_add_note": confirm_add_note,
    "propose_add_person": propose_add_person,
    "confirm_add_person": confirm_add_person,
    "force_nudge": force_nudge,
    "force_triage": force_triage,
    "draft_reply": draft_reply,
    "get_recent_runs": get_recent_runs,
}
