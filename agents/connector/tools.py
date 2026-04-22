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
        "name": "reply_to_message",
        "description": (
            "Regenerate a draft reply for a specific Gmail thread, "
            "identified by fuzzy reference. This is the iterative-"
            "feedback path alongside the autonomous auto-compose cron: "
            "the cron writes drafts every half hour, this tool lets "
            "the operator shape a specific one with real-time context.\n"
            "\n"
            "`message_ref` accepts ANY of:\n"
            "  - person name: 'Michelle', 'Jamie Fitzgerald'\n"
            "  - sender email: 'michelle@rivierapartners.com'\n"
            "  - subject substring: 'Reddit', 'Sunday lunch'\n"
            "  - explicit Gmail thread_id (hex, 14-22 chars)\n"
            "\n"
            "Matches against the auto-compose log + triage queue. On "
            "ambiguity (e.g., 'Michelle' matches three people), returns "
            "status='ambiguous' with candidate list — the operator picks.\n"
            "\n"
            "`hint` is an optional operator override injected at the "
            "top of the compose prompt:\n"
            "  - stylistic: 'make it warmer', 'two sentences shorter'\n"
            "  - factual: 'mention I already accepted the offer', 'the "
            "hiring manager followed up separately, reference that'\n"
            "Both take precedence over voice anchors, history, and "
            "conflicting brain facts.\n"
            "\n"
            "Use when the operator says 'redo the Michelle draft shorter', "
            "'regenerate Jamie's reply, warmer', 'the Reddit recruiter "
            "email — mention I'm pausing searches'. Called without a "
            "hint, it's the 'compose now, don't wait for the cron' "
            "path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_ref": {
                    "type": "string",
                    "description": (
                        "Fuzzy reference to the thread — person name, "
                        "sender email, subject substring, or thread_id"
                    ),
                },
                "hint": {
                    "type": "string",
                    "description": (
                        "Optional operator hint — stylistic or factual "
                        "guidance. Injected at the top of the compose "
                        "prompt as an authoritative override."
                    ),
                },
            },
            "required": ["message_ref"],
        },
    },
    {
        "type": "function",
        "name": "promote_recruiter",
        "description": (
            "Promote a cold-recruiter queue entry to a real "
            "people/<slug>.md file, identified by fuzzy reference. The "
            "normal path is the ✅ Promote button on the cold-recruiter "
            "FYI — this tool is the manual alternative when the "
            "button was dismissed or the operator is acting in chat.\n"
            "\n"
            "`descriptor` accepts ANY of:\n"
            "  - person name: 'Jane Ashby', 'Michelle'\n"
            "  - sender email: 'jane@lever.co'\n"
            "  - subject substring: 'Senior Director role at Reddit'\n"
            "  - explicit Gmail thread_id\n"
            "\n"
            "Resolution is SCOPED to un-acted cold-recruiter queue "
            "entries (status=queued_cold_recruiter). Already-promoted, "
            "rejected, or known-sender threads will not match — that "
            "scope prevents accidentally re-promoting the wrong thing.\n"
            "\n"
            "Returns {status: ok, slug, name, path} on success, "
            "{status: already_promoted, ...} on duplicate, "
            "{status: ambiguous, candidates: [...]} when the "
            "descriptor matches multiple queue entries (LLM should "
            "ask the operator to pick), or {status: not_found, ...} with "
            "recent threads when nothing matches."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "descriptor": {
                    "type": "string",
                    "description": (
                        "Fuzzy reference to the cold-recruiter queue "
                        "entry — person name, sender email, subject "
                        "substring, or thread_id"
                    ),
                },
            },
            "required": ["descriptor"],
        },
    },
    {
        "type": "function",
        "name": "promote_recruiter_by_thread_id",
        "description": (
            "Back-compat: promote a cold-recruiter queue entry by "
            "explicit Gmail thread_id. Prefer promote_recruiter "
            "(fuzzy descriptor) for conversational flows — this "
            "tool is kept for programmatic callers or cached "
            "dispatcher state that references the old name."
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


def promote_recruiter(descriptor: str) -> dict:
    """Operator-invoked promotion of a cold-recruiter stub to a real
    people file, identified by fuzzy reference. Mirrors the fuzzy-
    resolver pattern from reply_to_message: accept person name, sender
    email, subject substring, or explicit thread_id.

    Scoped to un-acted queue state — only matches triage queue entries
    whose status is ``queued_cold_recruiter``. Already-promoted
    senders, known senders, and generic queued threads are NOT
    candidates (they'd fail at the promotion step anyway because the
    callback needs from_email + from_header from an un-acted queue
    entry to create the people file).

    Use when the operator says 'promote that Jane Ashby recruiter', '/promote
    the Reddit one', or wants to promote a cold-recruiter stub without
    hunting for the Telegram button. Returns the same shape as the
    recruiter:promote callback:
      {status: ok | already_promoted | not_found | ambiguous, ...}
    On ambiguous, returns candidates[] for LLM-driven disambiguation.
    """
    ref = (descriptor or "").strip()
    if not ref:
        return {"status": "error", "error": "descriptor is required"}

    resolution = _resolve_thread_descriptor(
        ref,
        only_queue_statuses=["queued_cold_recruiter"],
    )
    if resolution.get("status") != "ok":
        return {**resolution, "descriptor": ref}

    tid = resolution["thread_id"]
    result = _handle_recruiter_callback("promote", tid)
    # Preserve the resolver's matched metadata so the Telegram-side
    # confirmation can name the sender / subject without another lookup.
    matched = resolution.get("matched")
    if matched and isinstance(result, dict):
        result.setdefault("matched_descriptor", ref)
        result.setdefault("matched_from_email",
                          matched.get("from_email", ""))
        result.setdefault("matched_subject",
                          matched.get("subject", ""))
    return result


_AUTO_COMPOSE_LOG = os.path.join(CACHE, "auto-compose-log.json")
_TRIAGE_QUEUE_PATH = os.path.join(CACHE, "triage-queue.json")

_THREAD_ID_RE = __import__("re").compile(r"^[0-9a-f]{14,22}$")
_EMAIL_RE = __import__("re").compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _load_thread_candidates(
    *, only_queue_statuses: list[str] | None = None,
) -> dict:
    """Union of the auto-compose log + triage queue, keyed by
    thread_id. Each candidate carries whatever metadata either source
    had — subject, slug (from log), from_email + from_header (from
    queue). When both sources have the same thread_id we merge fields.

    only_queue_statuses: when set (e.g., ['queued_cold_recruiter']),
    restrict candidates to queue entries whose status is in the list.
    Log-only entries are excluded entirely — useful for flows that
    only operate on un-acted queue state (e.g., /promote, which
    needs from_email + from_header from the queue entry to create
    the people file).
    """
    queue_raw = _read_json(_TRIAGE_QUEUE_PATH, default={}) or {}
    queue = queue_raw.get("queued") if isinstance(queue_raw, dict) else []
    queue = queue or []

    status_filter = set(only_queue_statuses) if only_queue_statuses else None

    if status_filter is not None:
        # Queue-only path: skip the log entirely.
        candidates: dict[str, dict] = {}
        for q in queue:
            if not isinstance(q, dict):
                continue
            tid = q.get("thread_id")
            if not tid:
                continue
            if q.get("status") not in status_filter:
                continue
            candidates[tid] = {
                "thread_id": tid,
                "subject": q.get("subject", ""),
                "slug": None,
                "from_email": q.get("from_email", ""),
                "from_header": q.get("from_header", ""),
                "last_seen": q.get("date", ""),
                "status": q.get("status", ""),
            }
        return candidates

    log = _read_json(_AUTO_COMPOSE_LOG, default={}) or {}
    candidates = {}
    for tid, entry in (log or {}).items():
        if not isinstance(entry, dict):
            continue
        candidates[tid] = {
            "thread_id": tid,
            "subject": entry.get("subject", ""),
            "slug": entry.get("slug"),
            "from_email": "",
            "from_header": "",
            "last_seen": entry.get("at", ""),
            "reply_needed": entry.get("reply_needed"),
            "fit_tier": entry.get("fit_tier", ""),
            "cold_inbound": bool(entry.get("cold_inbound")),
        }
    for q in queue:
        if not isinstance(q, dict):
            continue
        tid = q.get("thread_id")
        if not tid:
            continue
        if tid in candidates:
            candidates[tid]["from_email"] = q.get("from_email", "")
            candidates[tid]["from_header"] = q.get("from_header", "")
            if not candidates[tid].get("subject"):
                candidates[tid]["subject"] = q.get("subject", "")
        else:
            candidates[tid] = {
                "thread_id": tid,
                "subject": q.get("subject", ""),
                "slug": None,
                "from_email": q.get("from_email", ""),
                "from_header": q.get("from_header", ""),
                "last_seen": q.get("date", ""),
                "reply_needed": None,
                "fit_tier": "",
                "cold_inbound": q.get("status") == "queued_cold_recruiter",
            }
    return candidates


def _resolve_thread_descriptor(
    descriptor: str,
    *,
    only_queue_statuses: list[str] | None = None,
) -> dict:
    """Turn a fuzzy operator string into a Gmail thread_id.

    Accepts any of:
      - explicit thread_id (hex, 14–22 chars) — passes through
      - full email ('jane@lever.co') — matches from_email
      - person name ('first' / 'first last') — resolved via
        brain.get_person (with first-name fallback) to slug / emails
      - subject substring — matches log + queue

    only_queue_statuses: restrict matching to queue entries whose
    status is in the list (e.g., ['queued_cold_recruiter'] for
    /promote flows that only operate on un-acted queue state).

    Returns:
      {"status": "ok", "thread_id": tid, "matched": {...}}
      {"status": "ambiguous", "candidates": [...]}
      {"status": "not_found", "reason": str, "recent_threads": [...]}
    """
    s = (descriptor or "").strip()
    if not s:
        return {"status": "not_found", "reason": "empty descriptor"}

    candidates = _load_thread_candidates(
        only_queue_statuses=only_queue_statuses,
    )

    # 1. Explicit thread_id shape — pass through (even if not in log,
    #    Gmail will 404 if invalid; better to attempt than reject).
    if _THREAD_ID_RE.match(s):
        matched = candidates.get(s, {"thread_id": s, "source": "passthrough"})
        return {"status": "ok", "thread_id": s, "matched": matched}

    # 2. Try person lookup — this handles 'Michelle' (first-name fallback)
    #    and 'Michelle Leist' (exact slug) via brain.get_person.
    target_slug = None
    target_emails: list[str] = []
    person = None
    try:
        person = brain.get_person(s)
    except Exception:
        person = None
    if person:
        target_slug = person.get("slug")
        raw = person.get("raw", "") or ""
        target_emails = [e.lower() for e in _EMAIL_RE.findall(raw)]

    # 3. Score every candidate.
    slow = s.lower()
    matches: list[dict] = []
    for c in candidates.values():
        reasons: list[str] = []
        subj = (c.get("subject") or "").lower()
        from_email = (c.get("from_email") or "").lower()
        from_header = (c.get("from_header") or "").lower()
        slug = c.get("slug") or ""

        if target_slug and slug == target_slug:
            reasons.append(f"slug={target_slug}")
        if target_emails and from_email in target_emails:
            reasons.append(f"person_email={from_email}")
        # Email match: the descriptor itself looks like an email
        if "@" in slow and slow == from_email:
            reasons.append(f"email_exact")
        # Substring on sender email / header (useful for partial names
        # in a display-name: 'michelle' matches '"Michelle Leist" <...>').
        if slow in from_email and slow != "":
            reasons.append("email_substring")
        if slow in from_header and slow != "":
            reasons.append("header_substring")
        # Subject substring
        if slow in subj and slow != "":
            reasons.append("subject_substring")

        if reasons:
            matches.append({**c, "match_reason": " + ".join(reasons)})

    if not matches:
        # Return recent threads to help the caller disambiguate —
        # auto-compose log entries sorted by most-recent 'at' timestamp.
        recent = sorted(
            candidates.values(),
            key=lambda c: c.get("last_seen", ""),
            reverse=True,
        )[:5]
        return {
            "status": "not_found",
            "reason": f"no thread matches {descriptor!r}",
            "recent_threads": [
                {"thread_id": r["thread_id"],
                 "from_email": r.get("from_email", ""),
                 "subject": r.get("subject", "")[:80],
                 "last_seen": r.get("last_seen", "")}
                for r in recent
            ],
        }
    if len(matches) == 1:
        return {"status": "ok",
                "thread_id": matches[0]["thread_id"],
                "matched": matches[0]}

    # Rank ambiguous matches: most-recent first, then slug matches
    # (strongest signal) ahead of substring-only.
    def _rank(m):
        r = m.get("match_reason", "")
        strong = ("slug=" in r) or ("person_email=" in r) or ("email_exact" in r)
        return (0 if strong else 1, -1 * (m.get("last_seen", "") or ""))
    matches_sorted = sorted(matches, key=_rank)
    return {"status": "ambiguous",
            "candidates": matches_sorted[:8]}


def reply_to_message(message_ref: str, hint: str | None = None) -> dict:
    """Operator-invoked draft regeneration for a specific Gmail thread,
    identified by fuzzy reference (person name, email, subject
    substring, or explicit thread_id), with an optional hint that
    steers the compose prompt.

    The hint is why this tool exists alongside the auto-compose cron
    — the cron writes a draft every half hour, but can't take
    iterative feedback. The hint path lets the operator shape the draft with:

      - stylistic guidance: 'make it warmer', 'two sentences shorter',
        'drop the scope questions'
      - factual context: 'mention that I already accepted', 'flag
        that I'm traveling next week', 'the hiring manager followed
        up separately, reference that'

    Hints land at the top of the compose prompt as authoritative
    overrides — they take precedence over voice anchors, history
    defaults, and any conflicting brain facts.

    Resolution: message_ref is fuzzy-matched against the
    auto-compose log + triage queue. On ambiguity (multiple matches)
    returns the candidate list so the LLM can ask the operator to pick. On
    not-found returns recent threads as disambiguation options.

    Use when the operator says 'redo the Michelle draft shorter', 'regenerate
    the reply to Jamie, warmer', 'that Reddit recruiter email —
    mention I'm pausing other searches'.
    """
    ref = (message_ref or "").strip()
    if not ref:
        return {"status": "error", "error": "message_ref is required"}
    hint_str = (hint or "").strip() or None

    resolution = _resolve_thread_descriptor(ref)
    if resolution.get("status") != "ok":
        # Pass through ambiguous/not_found so the LLM surfaces candidates.
        return {**resolution, "message_ref": ref}

    tid = resolution["thread_id"]
    scripts_dir = Path(__file__).parent / "scripts"
    inbox_triage = str(scripts_dir / "inbox-triage.py")
    auto_compose = str(scripts_dir / "auto-compose.py")

    # Step 1: classify + upsert into triage queue.
    triage = subprocess_helpers.run_json_script(
        inbox_triage, "--thread-id", tid, timeout=60,
    )
    if subprocess_helpers.is_subprocess_error(triage):
        return {"status": "error", "stage": "triage",
                "error": triage.get("__error__", "triage script error"),
                "resolved_thread_id": tid}

    # Step 2: force compose, passing the hint through if present.
    compose_args = [auto_compose, "--force", tid, "--max", "1"]
    if hint_str:
        compose_args.extend(["--operator-hint", hint_str])
    compose = subprocess_helpers.run_json_script(
        *compose_args, timeout=180,
    )
    if subprocess_helpers.is_subprocess_error(compose):
        return {"status": "error", "stage": "compose",
                "error": compose.get("__error__", "compose script error"),
                "resolved_thread_id": tid,
                "triage_classification": triage.get("classification")}

    return {
        "status": "ok",
        "message_ref": ref,
        "thread_id": tid,
        "matched": resolution.get("matched"),
        "hint_applied": bool(hint_str),
        "triage_classification": triage.get("classification"),
        "compose": compose,
    }


# Backwards-compat alias — original name before the fuzzy-resolver
# rename. Kept so any cached Telegram tool state or documentation
# that still references the old name resolves correctly. New callers
# should use reply_to_message.
def reply_to_thread(thread_id: str, hint: str | None = None) -> dict:
    return reply_to_message(thread_id, hint=hint)


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
    "promote_recruiter": promote_recruiter,
    "reply_to_message": reply_to_message,
    "reply_to_thread": reply_to_thread,  # back-compat alias
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
