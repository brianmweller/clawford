"""agents/meetings-coach/tools.py — Sergeant Murphy's tool manifest.

Phase B: read-only tools over cached meeting state.
Phase C: list/confirm/dismiss pending action items from post-meeting debriefs.
"""
from __future__ import annotations

import glob as glob_mod
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import memory_writer  # type: ignore
import pending_actions  # type: ignore
import state_introspection  # type: ignore
import subprocess_helpers  # type: ignore
from fuzzy_resolver import (  # type: ignore
    Candidate as _FuzzyCandidate,
    resolve_fuzzy_descriptor as _shared_resolve,
    result_to_dict as _result_to_dict,
)

try:
    import brain as _brain  # type: ignore
except ImportError:  # pragma: no cover — workspace-install guardrail
    _brain = None


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
    the 'query by day' shape the LLM tools want.

    Parses stdout via ``parse_script_stdout`` so the SCRIPT_CONTRACT
    dual-envelope tail (data object + trailing ``{"status": "ok"}``)
    round-trips as the data object. Regression: 2026-04-22 — the
    prior naive ``json.loads(proc.stdout)`` turned every contract-
    compliant fetch into ``gcal-fetch non-JSON output`` silently,
    which in turn made the day-only on-demand fetch retry a no-op
    and the operator's 'Coinbase for tomorrow' fell through to not_found."""
    if not os.path.exists(GCAL_FETCH_SCRIPT):
        return {"error": f"gcal-fetch.py not found at {GCAL_FETCH_SCRIPT}"}
    try:
        proc = subprocess.run(
            [sys.executable, GCAL_FETCH_SCRIPT,
             "--date", start_date, "--days", str(days)],
            capture_output=True, text=True, timeout=45, cwd=WORKSPACE,
        )
    except subprocess.TimeoutExpired:
        return {"error": "gcal-fetch timed out after 45s"}
    except Exception as exc:
        return {"error": f"gcal-fetch subprocess failed: {exc}"}

    if proc.returncode != 0 and not proc.stdout:
        return {"error": proc.stderr.strip() or f"gcal-fetch exit {proc.returncode}"}

    parsed = subprocess_helpers.parse_script_stdout(proc.stdout or "")
    if isinstance(parsed, dict):
        return parsed
    return {"error": "gcal-fetch non-JSON output", "stdout": (proc.stdout or "")[:500]}


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


def _load_action_item_candidates() -> list[_FuzzyCandidate]:
    """Build an action-item candidate pool from all pending debrief
    files. Each candidate's subject is the action-item text, so
    substring matches like 'board deck' resolve correctly."""
    pool: list[_FuzzyCandidate] = []
    for it in (list_pending_action_items().get("items") or []):
        text = str(it.get("action_item") or "")
        pool.append(_FuzzyCandidate(
            id=it.get("item_id", ""),
            display=text[:80],
            last_seen=it.get("meeting_start", ""),
            subject=text,
            name=str(it.get("meeting") or ""),
            extras=dict(it),
        ))
    return pool


def _resolve_action_item_descriptor(descriptor: str) -> dict:
    """Map a fuzzy operator descriptor to an action-item item_id.
    Matches on action-item text (primary) or the meeting title
    (secondary). The item_id shape is '<event_id>:<idx>' which we
    accept as id_pattern passthrough."""
    pool = _load_action_item_candidates()
    result = _shared_resolve(
        descriptor,
        candidates=pool,
        id_pattern=r"^[A-Za-z0-9_-]+:\d+$",
    )

    def _fmt(c: _FuzzyCandidate) -> dict:
        return {
            "item_id": c.id,
            "action_item": c.subject,
            "meeting": c.name,
            "meeting_start": c.last_seen,
            "match_reason": c.match_reason,
        }

    return _result_to_dict(
        result, id_key="item_id",
        recent_key="recent_items",
        candidate_formatter=_fmt,
    )


def confirm_action_item(action_item: str) -> dict:
    """Mark a pending action item as accepted. ``action_item`` is a
    fuzzy descriptor — action-item text substring ('board deck'),
    meeting title, or explicit item_id ('<event_id>:<idx>'). On
    ambiguity returns candidates[] for LLM-driven disambiguation."""
    ref = (action_item or "").strip()
    if not ref:
        return {"status": "error", "error": "action_item is required"}
    resolution = _resolve_action_item_descriptor(ref)
    if resolution.get("status") != "ok":
        return {**resolution, "descriptor": ref}
    item_id = resolution["item_id"]
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


def dismiss_action_item(action_item: str) -> dict:
    """Mark a pending action item as dismissed. Accepts the same
    fuzzy-descriptor shapes as confirm_action_item."""
    ref = (action_item or "").strip()
    if not ref:
        return {"status": "error", "error": "action_item is required"}
    resolution = _resolve_action_item_descriptor(ref)
    if resolution.get("status") != "ok":
        return {**resolution, "descriptor": ref}
    item_id = resolution["item_id"]
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


# ---------------------------------------------------------------------------
# Meeting-descriptor resolver — P0 application of the shared
# fuzzy-resolver pattern. Operators describe meetings by attendee or
# title ("the Michelle call", "the board meeting"), never by GCal
# event_id. The resolver bridges that gap.
# ---------------------------------------------------------------------------


def _load_meeting_candidates(
    window_days_back: int = 1, window_days_forward: int = 7,
    include_non_real: bool = False,
) -> list[_FuzzyCandidate]:
    """Build a meeting-candidate pool from the cached gcal-fetch events
    files under the workspace. One Candidate per real meeting, with
    attendees concatenated into the searchable name/email fields so
    'the Michelle call' matches a meeting where Michelle is one of
    several attendees.

    include_non_real=True bypasses the gcal-fetch is_real_meeting
    classifier — needed for time-descriptor resolution, where the
    operator has named a specific clock time and the classifier's
    "no attendees, no video link → skip" heuristic is wrong for
    recruiter invites (Adobe, Greenhouse, etc. title themselves
    'Meeting Confirmation' with the interviewer only in the
    description body)."""
    cache = Path(WORKSPACE) / "cache"
    if not cache.exists():
        return []

    today = datetime.now(
        ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
    ).date()
    candidates: list[_FuzzyCandidate] = []
    seen_ids: set[str] = set()

    # Walk events-*.json files within the window; each may hold a
    # multi-day window of events (gcal-fetch --days N).
    for path in sorted(cache.glob("events-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for ev in (data.get("events") or []):
            eid = ev.get("id", "")
            if not eid or eid in seen_ids:
                continue
            if not include_non_real and not ev.get("is_real_meeting", True):
                # Respect the gcal-fetch classifier where present.
                continue
            start = (ev.get("start") or "")
            # Drop events too far outside our window.
            try:
                ev_date = datetime.fromisoformat(
                    start.replace("Z", "+00:00") if "T" in start else start
                ).date()
                delta = (ev_date - today).days
                if delta < -window_days_back or delta > window_days_forward:
                    continue
            except (ValueError, TypeError):
                pass

            attendees = ev.get("attendees") or []
            # Concatenate attendees so a substring like "michelle"
            # matches even on a multi-attendee meeting.
            names = [
                (a.get("name") or "").strip()
                for a in attendees if isinstance(a, dict)
            ]
            emails = [
                (a.get("email") or "").strip().lower()
                for a in attendees if isinstance(a, dict)
            ]
            seen_ids.add(eid)
            candidates.append(_FuzzyCandidate(
                id=eid,
                display=f"{ev.get('summary', '(untitled)')}",
                last_seen=start,
                name=" ; ".join(n for n in names if n),
                email=" ; ".join(e for e in emails if e),
                subject=ev.get("summary", ""),
                extras=dict(ev),
            ))
    return candidates


# Time-descriptor parser: "tomorrow 2:45pm", "3pm today", "14:45
# tomorrow", bare "3pm". Operators describe meetings by clock time at
# least as often as by name; the fuzzy resolver can't match on a time
# because its candidate fields are text. This parser returns the target
# datetime (in the user's TZ) and the _time_window_match branch below
# walks the candidate pool comparing start timestamps.

_WEEKDAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_TIME_RE = re.compile(
    r"""
    (?P<hour>\d{1,2})
    (?: [:.] (?P<minute>\d{2}) )?
    \s*
    (?P<ampm> am | pm | a\.m\.? | p\.m\.? )?
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _parse_time_descriptor(descriptor: str, tz: ZoneInfo) -> datetime | None:
    """Parse 'tomorrow 2:45pm' / '3pm today' / 'thu 10am' / '14:45' into
    a concrete datetime in ``tz``. Returns None if no time token found
    or if the time alone (no am/pm, hour > 23) is unparseable."""
    s = (descriptor or "").strip().lower()
    if not s:
        return None

    today = datetime.now(tz).date()
    day_offset: int | None = None
    if re.search(r"\btomorrow\b", s):
        day_offset = 1
    elif re.search(r"\btoday\b", s):
        day_offset = 0
    elif re.search(r"\byesterday\b", s):
        day_offset = -1
    else:
        # Weekday forms: "thu 10am", "monday 2pm". Pick the next
        # occurrence (today if today's weekday matches — the caller can
        # disambiguate via multiple matches).
        for token, wday in _WEEKDAY_NAMES.items():
            if re.search(rf"\b{token}\b", s):
                diff = (wday - today.weekday()) % 7
                day_offset = diff
                break

    target_date = today + timedelta(days=day_offset) if day_offset is not None else today

    # Strip the day phrase so the time regex doesn't chomp "2" from
    # "monday". Leave am/pm markers intact.
    time_text = s
    for token in ("tomorrow", "today", "yesterday", *_WEEKDAY_NAMES.keys()):
        time_text = re.sub(rf"\b{token}\b", " ", time_text)
    time_text = re.sub(r"\bat\b", " ", time_text)

    m = _TIME_RE.search(time_text)
    if not m:
        return None

    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").replace(".", "").lower()

    if ampm.startswith("p") and hour < 12:
        hour += 12
    elif ampm.startswith("a") and hour == 12:
        hour = 0
    elif not ampm:
        # Bare number with no am/pm. 24h only accepts 0-23; reject
        # values like "45" that _TIME_RE matched from "2:45" via the
        # fall-through path.
        if hour > 23 or minute > 59:
            return None
        # Ambiguous short hours ("3") without am/pm could be either,
        # but we treat them as 24h: if the user meant 3pm they would
        # write "3pm". Keep as-is.

    if hour > 23 or minute > 59:
        return None

    return datetime(target_date.year, target_date.month, target_date.day,
                    hour, minute, tzinfo=tz)


def _match_by_time(
    target: datetime,
    candidates: list[_FuzzyCandidate],
    window_minutes: int = 5,
) -> list[_FuzzyCandidate]:
    """Return candidates whose start timestamp falls within
    ±window_minutes of target. Candidates without a parseable start are
    skipped."""
    out: list[_FuzzyCandidate] = []
    delta = timedelta(minutes=window_minutes)
    for c in candidates:
        raw = (c.last_seen or "").strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=target.tzinfo)
        if abs(dt - target) <= delta:
            out.append(c)
    return out


_DESCRIPTOR_STOPWORDS = {
    "the", "a", "an", "my", "our", "your", "for", "with", "at",
    "on", "in", "to", "and", "please", "prep", "prepare",
    "meeting", "call", "chat", "interview", "sync", "standup",
    "catchup",
}


def _parse_day_only(descriptor: str, tz: ZoneInfo) -> tuple[date, str] | None:
    """Detect a day word in the descriptor without a clock time and
    return (target_date, cleaned_descriptor). Returns None when no day
    word is present. Cleaned descriptor drops day/stopword tokens so
    'Coinbase for tomorrow' → 'coinbase' for substring matching.

    Runs AFTER _parse_time_descriptor has returned None, so we know no
    time was parseable. A descriptor like 'tomorrow 3pm' hits the time
    branch first and never reaches here."""
    s = (descriptor or "").strip().lower()
    if not s:
        return None

    today = datetime.now(tz).date()
    day_offset: int | None = None
    day_tokens: list[str] = []
    if re.search(r"\btomorrow\b", s):
        day_offset = 1
        day_tokens = ["tomorrow"]
    elif re.search(r"\btoday\b", s):
        day_offset = 0
        day_tokens = ["today"]
    elif re.search(r"\byesterday\b", s):
        day_offset = -1
        day_tokens = ["yesterday"]
    else:
        for token, wday in _WEEKDAY_NAMES.items():
            if re.search(rf"\b{token}\b", s):
                day_offset = (wday - today.weekday()) % 7
                day_tokens = [token]
                break

    if day_offset is None:
        return None

    cleaned = s
    for token in day_tokens + list(_DESCRIPTOR_STOPWORDS):
        cleaned = re.sub(rf"\b{re.escape(token)}\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return today + timedelta(days=day_offset), cleaned


def _filter_candidates_by_date(
    candidates: list[_FuzzyCandidate], target: date, tz: ZoneInfo,
) -> list[_FuzzyCandidate]:
    out: list[_FuzzyCandidate] = []
    for c in candidates:
        raw = (c.last_seen or "").strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        if dt.astimezone(tz).date() == target:
            out.append(c)
    return out


def _resolve_meeting_descriptor(descriptor: str) -> dict:
    """Map a fuzzy operator descriptor to a GCal event_id.

    Thin wrapper over agents/shared/fuzzy_resolver. Accepts:
      - explicit event_id (broad regex; GCal ids are alphanumeric +
        underscore, length ~10+)
      - attendee name (first or full) — resolved via brain.get_person
        when available; falls back to substring on the concatenated
        attendee-names field
      - attendee email
      - meeting title substring
      - time reference ('tomorrow 2:45pm', '3pm today', '14:45
        tomorrow', bare '3pm' which defaults to today) — matched
        against candidate start within ±5 min
      - day-only reference ('Coinbase for tomorrow', 'my meeting
        tuesday') — narrows pool by date, substring-matches remaining
        content words after stripping conversational stopwords
    """
    pool = _load_meeting_candidates()

    # Time-descriptor branch: strong signal, runs before fuzzy string
    # matching. If the descriptor parses to a time AND we have a pool,
    # the match (or miss) is authoritative — don't fall through to
    # substring matching, which would match a 3pm-literal in a title.
    # Uses an expanded pool (include_non_real=True) so recruiter
    # invites with no attendees + phone-code location still resolve.
    tz = ZoneInfo(os.environ.get("TZ", DEFAULT_TZ))
    target_time = _parse_time_descriptor(descriptor, tz)
    time_pool = _load_meeting_candidates(include_non_real=True)
    if target_time is not None and time_pool:
        hits = _match_by_time(target_time, time_pool)
        if len(hits) == 1:
            c = hits[0]
            return {
                "status": "ok",
                "meeting_id": c.id,
                "match": {**(c.extras or {}),
                          "match_reason": f"time={target_time.isoformat()}"},
            }
        if len(hits) > 1:
            return {
                "status": "ambiguous",
                "candidates": [
                    {"meeting_id": c.id,
                     "title": (c.subject or "")[:80],
                     "start": c.last_seen,
                     "attendees": c.name,
                     "match_reason": f"time={target_time.isoformat()}"}
                    for c in hits
                ],
            }
        # Parsed a time, matched nothing — return not_found with a
        # time-specific reason rather than falling through to fuzzy.
        # A time miss isn't something substring can rescue.
        return {
            "status": "not_found",
            "reason": (
                f"no meeting at {target_time.strftime('%H:%M')} on "
                f"{target_time.date().isoformat()}"
            ),
            "recent_meetings": [],
        }

    # Day-only branch: descriptor contains 'tomorrow'/'today'/weekday
    # but no clock time. Narrow the pool to that date and substring-
    # match the remaining content words. Regression target:
    # 2026-04-22 'Coinbase for tomorrow' returned not_found because the
    # full descriptor went straight to title-substring matching.
    day_parse = _parse_day_only(descriptor, tz)
    if day_parse is not None:
        target_date, cleaned = day_parse
        day_pool = _filter_candidates_by_date(time_pool, target_date, tz)
        # On-demand cache refresh: if the target date is in the near
        # future (≤14 days) and the pool is empty for that day, extend
        # the events cache and reload once. Same 2026-04-22 incident
        # as above — between morning-brief runs the cache-warmer was
        # writing only today's events, so any "tomorrow" query missed.
        # Capped at 14 days to bound the API-quota blast radius of
        # operator typos. One fetch, one retry, never loops.
        if not day_pool:
            today_local = datetime.now(tz).date()
            offset = (target_date - today_local).days
            if 0 <= offset <= 14:
                fetch_result = _run_gcal_fetch(
                    today_local.isoformat(), offset + 1,
                )
                if not fetch_result.get("error"):
                    time_pool = _load_meeting_candidates(include_non_real=True)
                    pool = _load_meeting_candidates()
                    day_pool = _filter_candidates_by_date(
                        time_pool, target_date, tz,
                    )
        if day_pool:
            # No content tokens left → the day itself is the query.
            # One meeting that day = ok, many = ambiguous.
            if not cleaned:
                if len(day_pool) == 1:
                    c = day_pool[0]
                    return {
                        "status": "ok",
                        "meeting_id": c.id,
                        "match": {**(c.extras or {}),
                                  "match_reason": f"day={target_date.isoformat()}"},
                    }
                return {
                    "status": "ambiguous",
                    "candidates": [
                        {"meeting_id": c.id,
                         "title": (c.subject or "")[:80],
                         "start": c.last_seen,
                         "attendees": c.name,
                         "match_reason": f"day={target_date.isoformat()}"}
                        for c in day_pool
                    ],
                }
            # Content tokens remain — run fuzzy on the narrowed pool.
            day_result = _shared_resolve(
                cleaned,
                candidates=day_pool,
                id_pattern=r"^[A-Za-z0-9_]{10,}$",
            )
            if day_result.status == "ok":
                return _result_to_dict(
                    day_result, id_key="meeting_id",
                    recent_key="recent_meetings",
                    candidate_formatter=lambda c: {
                        **(c.extras or {}), "match_reason": c.match_reason,
                    },
                )
            if day_result.status == "ambiguous":
                return _result_to_dict(
                    day_result, id_key="meeting_id",
                    recent_key="recent_meetings",
                    candidate_formatter=lambda c: {
                        "meeting_id": c.id,
                        "title": (c.subject or "")[:80],
                        "start": c.last_seen,
                        "attendees": c.name,
                        "match_reason": c.match_reason,
                    },
                )
            # Narrowed-pool miss falls through to full-pool fuzzy so
            # content-token matching still runs in case the date was
            # guessed wrong (e.g. weekday cache not populated).

    person_resolver = None
    if _brain is not None:
        person_resolver = lambda n: _brain.get_person(n)  # noqa: E731

    result = _shared_resolve(
        descriptor,
        candidates=pool,
        id_pattern=r"^[A-Za-z0-9_]{10,}$",
        person_resolver=person_resolver,
    )

    def _meeting_fmt(c: _FuzzyCandidate) -> dict:
        return {
            "meeting_id": c.id,
            "title": (c.subject or "")[:80],
            "start": c.last_seen,
            "attendees": c.name,
            "match_reason": c.match_reason,
        }

    def _match_fmt(c: _FuzzyCandidate) -> dict:
        # For status=ok, pass original event dict through so callers
        # that need full event metadata can reach it via extras.
        return {**(c.extras or {}), "match_reason": c.match_reason}

    if result.status == "ok":
        return _result_to_dict(
            result, id_key="meeting_id",
            recent_key="recent_meetings",
            candidate_formatter=_match_fmt,
        )
    return _result_to_dict(
        result, id_key="meeting_id",
        recent_key="recent_meetings",
        candidate_formatter=_meeting_fmt,
    )


def force_prep(meeting: str) -> dict:
    """Run meeting-prep.py for a specific event on demand, identified
    by fuzzy reference. Returns the prep bundle: attendees, facts,
    open commitments, and — for professional meetings — the 8-field
    llm_prep block.

    `meeting` accepts any of:
      - attendee name ('Michelle', 'Jamie Fitzgerald')
      - attendee email
      - meeting title substring ('board meeting', 'Reddit')
      - time reference ('tomorrow 2:45pm', '3pm today', '14:45
        tomorrow', 'thu 10am') — resolver matches candidate start
        within ±5 min, so this is a primary path, not a fallback
      - explicit GCal event_id

    Use for `/prep [meeting]` — the LLM formats the returned data for
    the operator. On ambiguity, returns candidates[] for the LLM to offer
    choices on Telegram."""
    ref = (meeting or "").strip()
    if not ref:
        return {"status": "error", "error": "meeting is required"}
    resolution = _resolve_meeting_descriptor(ref)
    if resolution.get("status") != "ok":
        return {**resolution, "descriptor": ref}

    meeting_id = resolution["meeting_id"]
    result = subprocess_helpers.run_json_script(
        _MEETING_PREP, "--meeting-id", meeting_id, timeout=60,
    )
    if subprocess_helpers.is_subprocess_error(result):
        return {"status": "error",
                "error": result.get("__error__", "script error"),
                "resolved_meeting_id": meeting_id}
    return result


_WORKFLOWY_SYNC = str(_SCRIPTS_DIR / "workflowy-sync.py")


def prep_meeting(meeting: str) -> dict:
    """Push the professional-prep block for a specific meeting to
    Workflowy, identified by fuzzy reference (attendee name, email,
    title substring, or explicit event_id).

    Creates/finds the meeting node under the operator's Notes
    workspace and adds labeled sections (Framing, Recipient model,
    Objective, Why this role, Pitch, Evidence, Red flags, Questions
    to ask) to its Agenda, skipping any that already exist.

    Returns the Workflowy meeting node id + which labels were newly
    created vs skipped. For general (non-professional) meetings this
    is a no-op that returns status=skipped — operator prep belongs
    only on recruiter/hiring meetings. On ambiguity, returns
    candidates[] for the LLM to offer choices."""
    ref = (meeting or "").strip()
    if not ref:
        return {"status": "error", "error": "meeting is required"}
    resolution = _resolve_meeting_descriptor(ref)
    if resolution.get("status") != "ok":
        return {**resolution, "descriptor": ref}

    meeting_id = resolution["meeting_id"]
    result = subprocess_helpers.run_json_script(
        _WORKFLOWY_SYNC, "--push-prep-meeting", meeting_id, timeout=120,
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
            "Mark a pending action item as accepted, identified by "
            "fuzzy descriptor. `action_item` accepts:\n"
            "  - text substring of the action item ('board deck', "
            "'Q2 roadmap')\n"
            "  - the meeting title the item came from\n"
            "  - explicit item_id ('<event_id>:<idx>')\n"
            "\n"
            "On ambiguity returns candidates[] for LLM-driven "
            "disambiguation. Call when the operator says 'accept the board "
            "deck item' or 'I'll do the roadmap one'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_item": {
                    "type": "string",
                    "description": (
                        "Fuzzy reference — action-item text, meeting "
                        "title, or explicit item_id"
                    ),
                },
            },
            "required": ["action_item"],
        },
    },
    {
        "type": "function",
        "name": "dismiss_action_item",
        "description": (
            "Dismiss a pending action item — mark as not relevant or "
            "already done. Same fuzzy descriptor shapes as "
            "confirm_action_item."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_item": {
                    "type": "string",
                    "description": (
                        "Fuzzy reference — action-item text, meeting "
                        "title, or explicit item_id"
                    ),
                },
            },
            "required": ["action_item"],
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
            "Run meeting-prep for a specific calendar event on demand, "
            "identified by fuzzy reference. Returns attendees, facts, "
            "open commitments, and (for recruiter / hiring meetings) "
            "the 8-field llm_prep block.\n"
            "\n"
            "`meeting` accepts ANY of:\n"
            "  - attendee name: 'Michelle', 'Jamie Fitzgerald'\n"
            "  - attendee email\n"
            "  - meeting title substring: 'board meeting', 'Reddit'\n"
            "  - time reference: 'tomorrow 2:45pm', '3pm today', "
            "'14:45 tomorrow', 'thu 10am'. Pass these straight "
            "through — do NOT ask the operator for an email, title, or "
            "event id when he's already named a time.\n"
            "  - explicit GCal event_id\n"
            "\n"
            "Use for `/prep [meeting]` or 'what do I need to know "
            "before my 3pm with Alice'. On ambiguity, returns "
            "candidates[] for LLM-driven disambiguation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "meeting": {
                    "type": "string",
                    "description": (
                        "Fuzzy reference to the meeting — attendee name "
                        "or email, title substring, or event_id"
                    ),
                },
            },
            "required": ["meeting"],
        },
    },
    {
        "type": "function",
        "name": "prep_meeting",
        "description": (
            "Push the 8-field professional-meeting prep to Workflowy, "
            "identified by fuzzy reference. Creates the meeting node "
            "if it doesn't exist yet (Notes > <year> > <month> > "
            "<date>) and populates its Agenda with labeled sections "
            "(Framing, Recipient model, Objective, Why this role, "
            "Pitch, Evidence, Red flags, Questions to ask). "
            "Idempotent — re-running skips existing sections.\n"
            "\n"
            "`meeting` accepts ANY of:\n"
            "  - attendee name, attendee email, title substring, or "
            "explicit event_id\n"
            "  - time reference: 'tomorrow 2:45pm', '3pm today', "
            "'thu 10am'. Pass straight through; don't ask the operator for "
            "an event id when he's already given a time.\n"
            "\n"
            "Use when the operator says '/prep-meeting Michelle', 'prep the "
            "Anthropic call', or similar. For general (non-"
            "professional) meetings returns status=skipped — prep "
            "belongs only on recruiter/hiring meetings. On ambiguity, "
            "returns candidates[] for LLM-driven disambiguation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "meeting": {
                    "type": "string",
                    "description": (
                        "Fuzzy reference to the meeting — attendee "
                        "name or email, title substring, or event_id"
                    ),
                },
            },
            "required": ["meeting"],
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
    "prep_meeting": prep_meeting,
    "force_debrief": force_debrief,
    "propose_coaching_toggle": propose_coaching_toggle,
    "confirm_coaching_toggle": confirm_coaching_toggle,
    "propose_coaching_area_add": propose_coaching_area_add,
    "confirm_coaching_area_add": confirm_coaching_area_add,
    "propose_coaching_area_remove": propose_coaching_area_remove,
    "confirm_coaching_area_remove": confirm_coaching_area_remove,
    "get_recent_runs": get_recent_runs,
}
