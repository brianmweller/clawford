"""agents/meetings-coach/tools.py — Sergeant Murphy's tool manifest.

Phase B: read-only tools over cached meeting state.
Phase C: list/confirm/dismiss pending action items from post-meeting debriefs.
"""
from __future__ import annotations

import glob as glob_mod
import importlib.util
import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import memory_writer  # type: ignore
import pending_actions  # type: ignore
import subprocess_helpers  # type: ignore


def _load_post_meeting_scan():
    """Import post-meeting-scan.py as a module so its save/dismiss
    helpers can back the debrief button executors. The script is
    SCRIPT_CONTRACT-guarded (no top-level side effects), so importing
    is safe."""
    script = os.path.join(
        os.path.expanduser("~/.clawford/meetings-coach-workspace"),
        "scripts", "post-meeting-scan.py",
    )
    if not os.path.exists(script):
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "scripts", "post-meeting-scan.py",
        )
    spec = importlib.util.spec_from_file_location("pms", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_pms = None


def _pms_mod():
    global _pms
    if _pms is None:
        _pms = _load_post_meeting_scan()
    return _pms


def save_debrief(event_id: str) -> dict:
    """Button executor: Save. Append action items to active.md and
    delete the pending file."""
    return _pms_mod().save_debrief_to_brain(event_id)


def dismiss_debrief(event_id: str) -> dict:
    """Button executor: Dismiss. Delete the pending file without
    writing to active.md."""
    return _pms_mod().dismiss_debrief(event_id)


def replace_action_items(event_id: str, items: list) -> dict:
    """LLM tool: wholesale replace the action items on a pending
    debrief with the operator's corrected list, then write them to
    ``commitments/active.md``. Call this when the operator tells you to edit
    a debrief's action items (e.g. "change item 1 to 'Steve: Talk to
    recruiting'"). Parse his reply into a list of strings (one per
    item) and pass with the event_id he's modifying — the event_id
    usually appears in the conversation as part of the most recent
    debrief message he's responding to."""
    return _pms_mod().replace_action_items(event_id, items or [])

AGENT_ID = "meetings-coach"

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


# ---------------------------------------------------------------------------
# Phase C producer tools — action item management
# ---------------------------------------------------------------------------


def propose_remember(rule: str, category: str = "General") -> dict:
    return memory_writer.propose_pending_remember(AGENT_ID, rule, category)


def confirm_remember(rule: str, category: str) -> dict:
    return memory_writer.append_rule(AGENT_ID, rule, category)


def list_pending_action_items() -> dict:
    """Read all pending-debrief-*.json files, extract action items."""
    pattern = os.path.join(CACHE, "pending-debrief-*.json")
    files = sorted(glob_mod.glob(pattern))
    items = []
    for f in files:
        data = _read_json(f)
        if not data or data.get("status") != "pending_review":
            continue
        event_id = data.get("event_id", "")
        meeting = data.get("meeting_title", "")
        for i, ai in enumerate(data.get("krisp_action_items", [])):
            items.append({
                "item_id": f"{event_id}:{i}",
                "meeting": meeting,
                "meeting_start": data.get("meeting_start"),
                "action_item": ai,
                "status": "pending",
            })
    return {"count": len(items), "items": items}


def confirm_action_item(item_id: str) -> dict:
    """Mark an action item as accepted. Writes status to the debrief file.
    No Workflowy integration per the operator's decision."""
    event_id, _, idx_str = item_id.partition(":")
    if not event_id or not idx_str:
        return {"status": "error", "error": f"invalid item_id: {item_id}"}
    debrief_path = os.path.join(CACHE, f"pending-debrief-{event_id}.json")
    data = _read_json(debrief_path)
    if not data:
        return {"status": "error", "error": f"debrief not found for {event_id}"}

    try:
        idx = int(idx_str)
    except ValueError:
        return {"status": "error", "error": f"invalid index in item_id: {item_id}"}

    accepted = data.setdefault("accepted_items", [])
    if idx not in accepted:
        accepted.append(idx)
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(debrief_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    ai_text = ""
    items = data.get("krisp_action_items", [])
    if 0 <= idx < len(items):
        ai_text = items[idx]
    return {"status": "ok", "item_id": item_id, "action_item": ai_text}


def dismiss_action_item(item_id: str) -> dict:
    """Mark an action item as dismissed."""
    event_id, _, idx_str = item_id.partition(":")
    if not event_id or not idx_str:
        return {"status": "error", "error": f"invalid item_id: {item_id}"}
    debrief_path = os.path.join(CACHE, f"pending-debrief-{event_id}.json")
    data = _read_json(debrief_path)
    if not data:
        return {"status": "error", "error": f"debrief not found for {event_id}"}

    try:
        idx = int(idx_str)
    except ValueError:
        return {"status": "error", "error": f"invalid index in item_id: {item_id}"}

    dismissed = data.setdefault("dismissed_items", [])
    if idx not in dismissed:
        dismissed.append(idx)
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(debrief_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return {"status": "ok", "item_id": item_id, "dismissed": True}


# ---------------------------------------------------------------------------
# Force-run scripts (/prep, /debrief)
# ---------------------------------------------------------------------------


_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
_MEETING_PREP = str(_SCRIPTS_DIR / "meeting-prep.py")
_POST_MEETING_SCAN = str(_SCRIPTS_DIR / "post-meeting-scan.py")


def force_prep(meeting_id: str) -> dict:
    """Run meeting-prep.py for a specific event on demand. Returns the
    prep bundle: attendees, facts, open commitments. Use for `/prep
    [meeting]` — the LLM then formats the prep for the operator."""
    meeting_id = (meeting_id or "").strip()
    if not meeting_id:
        return {"status": "error", "error": "meeting_id is required"}
    result = subprocess_helpers.run_json_script(
        _MEETING_PREP, "--meeting-id", meeting_id, timeout=60,
    )
    if subprocess_helpers.is_subprocess_error(result):
        return {"status": "error", "error": result.get("__error__", "script error")}
    return result


def force_debrief() -> dict:
    """Run post-meeting-scan.py on demand — processes any recently-ended
    meetings with transcripts, stages debriefs, sends Telegram messages.
    Use for `/debrief` — the cron runs periodically, but this forces a
    check now (e.g. right after a meeting ends)."""
    result = subprocess_helpers.run_json_script(
        _POST_MEETING_SCAN, timeout=120,
    )
    if subprocess_helpers.is_subprocess_error(result):
        return {"status": "error", "error": result.get("__error__", "script error")}
    return result


# ---------------------------------------------------------------------------
# Coaching config mutators (/coaching on, off, add, remove)
# ---------------------------------------------------------------------------


def _read_meeting_config() -> dict:
    cfg = _read_json(CONFIG_PATH, default={})
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def _write_meeting_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def propose_coaching_toggle(enabled: bool) -> dict:
    """Stage a toggle of coaching.enabled in meeting-config.json."""
    label = "ON" if enabled else "OFF"
    return pending_actions.stage(
        AGENT_ID,
        "coaching_toggle",
        {"enabled": bool(enabled)},
        f"Turn coaching {label}",
    )


def confirm_coaching_toggle(enabled: bool) -> dict:
    """Flip coaching.enabled, creating the coaching block if absent."""
    cfg = _read_meeting_config()
    coaching = cfg.setdefault("coaching", {})
    coaching["enabled"] = bool(enabled)
    coaching.setdefault("growth_areas", [])
    _write_meeting_config(cfg)
    return {"status": "ok", "enabled": bool(enabled)}


def propose_coaching_area_add(area_id: str, description: str) -> dict:
    """Stage the addition of a new growth area to coaching.growth_areas."""
    area_id = (area_id or "").strip()
    description = (description or "").strip()
    if not area_id or not description:
        return {"status": "error", "error": "area_id and description are required"}
    return pending_actions.stage(
        AGENT_ID,
        "coaching_area_add",
        {"area_id": area_id, "description": description},
        f"Add coaching area '{area_id}': {description[:80]}",
    )


def confirm_coaching_area_add(area_id: str, description: str) -> dict:
    """Append a growth area. Rejects duplicate ids."""
    cfg = _read_meeting_config()
    coaching = cfg.setdefault("coaching", {})
    areas = coaching.setdefault("growth_areas", [])
    existing_ids = {a.get("id") for a in areas}
    if area_id in existing_ids:
        return {"status": "error", "error": f"growth area already exists: {area_id!r}"}
    areas.append({"id": area_id, "label": description})
    _write_meeting_config(cfg)
    return {"status": "ok", "area_id": area_id}


def propose_coaching_area_remove(area_id: str) -> dict:
    """Stage removal of a growth area by id."""
    area_id = (area_id or "").strip()
    if not area_id:
        return {"status": "error", "error": "area_id is required"}
    return pending_actions.stage(
        AGENT_ID,
        "coaching_area_remove",
        {"area_id": area_id},
        f"Remove coaching area '{area_id}'",
    )


def confirm_coaching_area_remove(area_id: str) -> dict:
    """Remove a growth area by id. Returns error if not found."""
    cfg = _read_meeting_config()
    coaching = cfg.setdefault("coaching", {})
    areas = coaching.setdefault("growth_areas", [])
    before = len(areas)
    coaching["growth_areas"] = [a for a in areas if a.get("id") != area_id]
    if len(coaching["growth_areas"]) == before:
        return {"status": "error", "error": f"growth area not found: {area_id!r}"}
    _write_meeting_config(cfg)
    return {"status": "ok", "area_id": area_id}


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
    {
        "type": "function",
        "name": "list_pending_action_items",
        "description": (
            "Return all open action items from post-meeting debriefs. "
            "Each item has an item_id, the meeting it came from, and "
            "the action item text. Call when the operator asks 'what action "
            "items do I have' or 'anything from that meeting'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "confirm_action_item",
        "description": (
            "Mark an action item as accepted. Use the item_id from "
            "list_pending_action_items. Call when the operator says 'accept "
            "that' or 'I'll do it'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "Action item ID (event_id:index)"},
            },
            "required": ["item_id"],
        },
    },
    {
        "type": "function",
        "name": "dismiss_action_item",
        "description": (
            "Dismiss an action item — mark it as not relevant or "
            "already done. Use the item_id from list_pending_action_items."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
            },
            "required": ["item_id"],
        },
    },
    {
        "type": "function",
        "name": "replace_action_items",
        "description": (
            "Wholesale REPLACE the action items on a pending debrief with "
            "the operator's corrected list after the Modify button. Use when the "
            "most recent Modify prompt is in the conversation (it includes "
            "'event_id: <X>') and the operator has typed his corrections. Parse "
            "his reply into one item per line — each item should read "
            "like 'Who: Task description' or 'Who to do the task'. Call "
            "this tool with the event_id from the Modify prompt and the "
            "list of item strings. The tool writes directly to the brain "
            "and deletes the staged debrief; no further confirmation is "
            "needed. If the operator's reply says 'nothing to save' or similar, "
            "pass an empty list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description":
                        "The event_id from the Modify prompt (e.g. "
                        "'16oq5cfaici98moknajrdutb94')",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description":
                        "One string per action item. Format 'Who: Task' "
                        "or 'Who to do X'. Empty array clears everything.",
                },
            },
            "required": ["event_id", "items"],
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
        "name": "force_prep",
        "description": (
            "Run meeting-prep.py for a specific calendar event on demand. "
            "Returns attendees, facts about them, and open commitments. "
            "Use for `/prep [meeting name or id]` or when the operator asks "
            "'what do I need to know before my 3pm with Alice'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "meeting_id": {
                    "type": "string",
                    "description": "GCal event id from today's meetings cache",
                },
            },
            "required": ["meeting_id"],
        },
    },
    {
        "type": "function",
        "name": "force_debrief",
        "description": (
            "Run the post-meeting scan on demand — processes any recently-"
            "ended meetings with Krisp transcripts, stages debriefs. Use "
            "for `/debrief` or when the operator just got out of a meeting and "
            "wants the debrief now instead of waiting for the cron."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "propose_coaching_toggle",
        "description": (
            "Stage a toggle of coaching.enabled in meeting-config.json. Use "
            "for `/coaching on` or `/coaching off`. the operator will confirm or cancel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "New coaching state"},
            },
            "required": ["enabled"],
        },
    },
    {
        "type": "function",
        "name": "propose_coaching_area_add",
        "description": (
            "Stage the addition of a new growth area to coaching.growth_areas. "
            "Use for `/coaching add {id} {description}`. area_id is a short "
            "identifier like 'brevity' or 'closing'; description is a 1-2 "
            "sentence anchor like 'Keep intros under 60s.'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "area_id": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["area_id", "description"],
        },
    },
    {
        "type": "function",
        "name": "propose_coaching_area_remove",
        "description": (
            "Stage removal of a growth area by id. Use for `/coaching "
            "remove {id}`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "area_id": {"type": "string"},
            },
            "required": ["area_id"],
        },
    },
]


EXECUTORS: dict = {
    "get_meetings_for_day": get_meetings_for_day,
    "get_week_meetings": get_week_meetings,
    "get_commitment_status": get_commitment_status,
    "get_coaching_config": get_coaching_config,
    "get_recent_coaching_entries": get_recent_coaching_entries,
    "list_pending_action_items": list_pending_action_items,
    "confirm_action_item": confirm_action_item,
    "dismiss_action_item": dismiss_action_item,
    "propose_remember": propose_remember,
    "confirm_remember": confirm_remember,
    "save_debrief": save_debrief,
    "dismiss_debrief": dismiss_debrief,
    "replace_action_items": replace_action_items,
    "force_prep": force_prep,
    "force_debrief": force_debrief,
    "propose_coaching_toggle": propose_coaching_toggle,
    "confirm_coaching_toggle": confirm_coaching_toggle,
    "propose_coaching_area_add": propose_coaching_area_add,
    "confirm_coaching_area_add": confirm_coaching_area_add,
    "propose_coaching_area_remove": propose_coaching_area_remove,
    "confirm_coaching_area_remove": confirm_coaching_area_remove,
}
