#!/usr/bin/env python3
"""post-meeting-scan.py — Meetings Coach (Sergeant Murphy) debrief cron.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`meetings-coach:post-meeting-scan`. This cron is delicate — the
2026-04-14 Alexis Lloyd incident re-sent the same debrief 5x across
2.5 hours because the LLM was told to iterate pending-debrief-*.json
files as a delivery queue. This implementation preserves the operator's
74c726c fix invariants:

1. DELIVERY INVARIANT: the ONLY items eligible for Telegram delivery
   are those in `transcript-scan.py`'s `processed` list for the
   current run. Pending files are the /confirm staging area, not a
   delivery queue.
2. COACHING DEDUP: before running metrics + LLM composition, check
   coaching-history.json for an existing entry with the same event_id.
3. CLEANUP vs DELIVERY: the cleanup pass walks pending-debrief-*.json
   files and deletes any whose event_id already landed in active.md.
   It never sends anything.
4. KRISP 401 4-STEP: stamp krisp-last-401.json on every 401, send a
   rate-limited Telegram alert via krisp-last-alert.json (90 min
   suppression window).

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.llm import infer as llm_infer  # noqa: E402
from agents.shared.subprocess_helpers import (  # noqa: E402
    is_subprocess_error,
    run_json_script,
)
from agents.shared.telegram_api import resolve_credentials, send_message  # noqa: E402


WORKSPACE = Path(os.path.expanduser("~/.clawford/meetings-coach-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
MEETING_CONFIG_FILE = WORKSPACE / "meeting-config.json"
COACHING_HISTORY_FILE = CACHE_DIR / "coaching-history.json"
KRISP_LAST_401_FILE = CACHE_DIR / "krisp-last-401.json"
KRISP_LAST_ALERT_FILE = CACHE_DIR / "krisp-last-alert.json"
LAST_RUN_FILE = CACHE_DIR / "last-post-meeting.json"
ACTIVE_COMMITMENTS_FILE = Path(
    os.path.expanduser("~/Dropbox/openclaw-backup/commitments/active.md")
)

BOT_TOKEN_ENV = "MEETINGS_BOT_TOKEN"
SUBPROCESS_TIMEOUT_S = 120
LLM_TIMEOUT_S = 60
LLM_RETRY_BACKOFF_S = 3
KRISP_ALERT_RATE_LIMIT_S = 90 * 60  # 90 min

_COACHING_PROMPT_TEMPLATE = """You coach the operator on meeting communication
skills. Analyse the transcript excerpt below and assess each of the
growth areas the operator is working on.

Growth areas:
{growth_areas}

For each area, provide:
- assessment: 1-2 short sentences describing what you observed.
- quote: a short (~80 char) direct quote of the operator's words that
  illustrates the area. Empty string if nothing to flag.
- timestamp: the HH:MM:SS timestamp from the transcript if the quote
  has one. Empty string if not available.
- try: a tighter/better alternative the operator could have said.
  Empty string if nothing to flag.

Return JSON ONLY, no markdown fences. Keys must be the area IDs
listed above. Example shape:
{{"concision": {{"assessment": "...", "quote": "...", "timestamp": "...", "try": "..."}}}}

Meeting: {meeting_title}
Talk ratio (the operator): {talk_ratio}
Avg the operator turn: {avg_turn} words
the operator questions asked: {questions}
the operator filler count: {fillers}

Transcript excerpt (may be truncated):
{transcript}
"""


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _strip_markdown_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


# ─── format_debrief ──────────────────────────────────────────────────


_SPEAKER_PLACEHOLDER_RE = re.compile(r"\{\{\s*Speaker_(\d+)\s*\}\}")
_FIRST_TURN_RE = re.compile(
    r"\*\*[A-Z][a-zA-Z \-']+\s*\|\s*\d{1,2}:\d{2}\*\*"
)
_OWNER_PROSE_RE = re.compile(
    r"^(?P<who>[A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+)?)"
    r"\s+(?P<verb>to|will|should|shall|must|needs to|has to|is going to)"
    r"\b\s+(?P<what>.+)$",
    re.DOTALL,
)


def _first_name(full: str) -> str:
    return (full or "").strip().split(" ", 1)[0]


def _resolve_speaker_placeholders(text: str, speakers: list) -> str:
    """Replace {{Speaker_N}} tokens with speakers[N-1] (first name).
    Strip unresolvable tokens. Krisp emits placeholders in this shape
    because it stores transcripts before the speaker→name mapping is
    confirmed; the mapping rides on the meeting's ordered ``speakers``
    array, which we persist as ``krisp_speakers`` on the pending file."""
    def repl(match):
        n = int(match.group(1))
        if 1 <= n <= len(speakers):
            return _first_name(speakers[n - 1])
        return ""
    text = _SPEAKER_PLACEHOLDER_RE.sub(repl, text or "")
    return re.sub(r"\s+", " ", text).strip()


def _extract_action_item(item, speakers=None) -> tuple[str, str, str]:
    """Resolve a Krisp-shaped or canonical action item into
    ``(who, what, by_when)``. Every item is guaranteed to carry a
    ``who`` if any content is available: try assignee → prose-leading
    name → first speaker (meeting host) → 'Unassigned'."""
    speakers = speakers or []
    if not isinstance(item, dict):
        raw = str(item or "").strip()
        if not raw:
            return "", "", ""
        who = _first_name(speakers[0]) if speakers else "Unassigned"
        return who, raw, ""

    raw = (
        item.get("what")
        or item.get("title")
        or item.get("text")
        or item.get("task")
        or ""
    )
    raw = _resolve_speaker_placeholders(str(raw), speakers)
    if not raw:
        return "", "", ""

    assignee = (
        item.get("who") or item.get("assignee") or item.get("owner") or ""
    ) or ""
    assignee = str(assignee).strip()

    by_when = (
        item.get("by_when") or item.get("due") or item.get("due_date") or ""
    ) or ""
    by_when = str(by_when).strip()

    if assignee:
        who = _first_name(assignee)
        what = raw
    else:
        m = _OWNER_PROSE_RE.match(raw)
        if m:
            who = _first_name(m.group("who"))
            what = f"{m.group('verb')} {m.group('what')}".strip()
        else:
            who = _first_name(speakers[0]) if speakers else "Unassigned"
            what = raw

    if what:
        what = what[0].upper() + what[1:] if len(what) > 1 else what.upper()
    return who, what, by_when


def format_debrief(pending: dict) -> str:
    """Render the debrief Telegram message from a pending-debrief-*.json
    staged file."""
    title = (pending.get("meeting_title") or "(untitled)").strip()
    action_items = pending.get("krisp_action_items") or []
    key_points = pending.get("krisp_key_points") or []
    speakers = pending.get("krisp_speakers") or []

    lines: list[str] = [
        f"\U0001f437\U0001f50d Debrief ready — {title}",
        "",
    ]

    rendered_items: list[str] = []
    for item in action_items:
        who, what, by_when = _extract_action_item(item, speakers=speakers)
        if not what:
            continue
        segment = f"{who}: {what}" if who else what
        if by_when:
            segment += f" (by {by_when})"
        rendered_items.append(segment)

    if rendered_items:
        lines.append("\U0001f3af ACTION ITEMS:")
        for idx, segment in enumerate(rendered_items, start=1):
            lines.append(f"{idx}. {segment}")
        lines.append("")

    if key_points:
        lines.append("\U0001f4cc KEY POINTS:")
        for point in key_points:
            lines.append(f"\u2022 {point}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ─── inline keyboard ─────────────────────────────────────────────────


def build_debrief_keyboard(pending: dict) -> dict | None:
    """Build the inline keyboard attached to a debrief message.
    One button set per debrief: Save / Dismiss / Modify. Returns None
    when the pending has no event_id — the callbacks would be unable
    to resolve the source file."""
    event_id = (pending.get("event_id") or "").strip()
    if not event_id:
        return None
    return {
        "inline_keyboard": [
            [
                {"text": "\u2705 Save",
                 "callback_data": f"debrief_save:{event_id}"},
                {"text": "\u274c Dismiss",
                 "callback_data": f"debrief_dismiss:{event_id}"},
                {"text": "\u270f\ufe0f Modify",
                 "callback_data": f"debrief_modify:{event_id}"},
            ]
        ]
    }


# ─── save/dismiss executors (button handlers) ────────────────────────


_COMMITMENT_ID_RE = re.compile(r"meetings-coach-(\d{4}-\d{2}-\d{2})-(\d{3})\b")


def _next_commitment_sequence(active_md: Path, date_str: str) -> int:
    """Return the next sequence number for today's commitments, scanning
    existing ids in ``active.md``. Starts at 1."""
    if not active_md.exists():
        return 1
    try:
        content = active_md.read_text(encoding="utf-8")
    except OSError:
        return 1
    max_seq = 0
    for d, seq in _COMMITMENT_ID_RE.findall(content):
        if d == date_str:
            try:
                max_seq = max(max_seq, int(seq))
            except ValueError:
                continue
    return max_seq + 1


def _event_already_in_active(active_md: Path, event_id: str) -> bool:
    if not event_id or not active_md.exists():
        return False
    try:
        content = active_md.read_text(encoding="utf-8")
    except OSError:
        return False
    return f"event_id: {event_id}" in content


def _delete_pending(event_id: str) -> None:
    path = CACHE_DIR / f"pending-debrief-{event_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def save_debrief_to_brain(event_id: str) -> dict:
    """Append non-dismissed action items from a staged debrief to
    ``commitments/active.md`` using the canonical schema, then delete
    the pending file. Idempotent — if the event_id is already present
    in active.md, returns ``already_saved`` without rewriting."""
    pending = _load_pending(event_id)
    if pending is None:
        return {
            "status": "error",
            "error": f"pending-debrief not found for event_id={event_id}",
        }

    active_md = ACTIVE_COMMITMENTS_FILE
    if _event_already_in_active(active_md, event_id):
        _delete_pending(event_id)
        return {
            "status": "ok",
            "already_saved": True,
            "written": 0,
            "event_id": event_id,
        }

    speakers = pending.get("krisp_speakers") or []
    items = pending.get("krisp_action_items") or []
    dismissed = set(pending.get("dismissed_items") or [])
    meeting_title = (pending.get("meeting_title") or "").strip()
    meeting_start = (pending.get("meeting_start") or "").strip()

    today_str = date.today().isoformat()
    seq = _next_commitment_sequence(active_md, today_str)

    blocks: list[str] = []
    for idx, item in enumerate(items):
        if idx in dismissed:
            continue
        who, what, by_when = _extract_action_item(item, speakers=speakers)
        if not what:
            continue
        entry_id = f"meetings-coach-{today_str}-{seq:03d}"
        seq += 1
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"## {entry_id}",
            f"- who: {who or 'Unassigned'}",
            "- to_whom: the operator",
            f"- what: {what}",
        ]
        if by_when:
            lines.append(f"- by_when: {by_when}")
        lines.extend([
            "- status: open",
            f"- source_detail: {meeting_title} debrief "
            f"(event_id: {event_id}, meeting_start: {meeting_start})",
            "- source_agent: meetings-coach",
            f"- created_at: {created_at}",
        ])
        blocks.append("\n".join(lines) + "\n")

    if blocks:
        active_md.parent.mkdir(parents=True, exist_ok=True)
        if not active_md.exists():
            active_md.write_text(
                "# Commitments — Active\n\n", encoding="utf-8"
            )
        with active_md.open("a", encoding="utf-8") as f:
            for block in blocks:
                f.write("\n" + block)

    _delete_pending(event_id)
    return {
        "status": "ok",
        "written": len(blocks),
        "event_id": event_id,
    }


def dismiss_debrief(event_id: str) -> dict:
    """Skip this debrief entirely. Never writes to active.md — just
    deletes the pending file so the cleanup pass has nothing to do."""
    pending_path = CACHE_DIR / f"pending-debrief-{event_id}.json"
    existed = pending_path.exists()
    _delete_pending(event_id)
    return {
        "status": "ok",
        "dismissed": True,
        "existed": existed,
        "event_id": event_id,
    }


def _parse_replacement_item(raw: str) -> tuple[str, str]:
    """Best-effort parse of a free-form the operator-typed line like
    'Steve: Talk to recruiting about X' or 'Steve to talk to...'.
    Returns (who, what). Never raises — falls back to ("", raw)."""
    line = (raw or "").strip()
    if not line:
        return "", ""
    if ":" in line:
        head, _, tail = line.partition(":")
        head = head.strip()
        tail = tail.strip()
        if head and tail and " " not in head.strip().rstrip("."):
            return _first_name(head), tail
    m = _OWNER_PROSE_RE.match(line)
    if m:
        return _first_name(m.group("who")), f"{m.group('verb')} {m.group('what')}".strip()
    return "", line


def replace_action_items(event_id: str, items: list) -> dict:
    """Wholesale replace the action items on a staged debrief with the
    list the operator typed after pressing Modify. Each entry is parsed for
    ``who``/``what`` and written to ``commitments/active.md`` using the
    same canonical schema as ``save_debrief_to_brain``. The pending file
    is deleted whether or not any items land."""
    pending = _load_pending(event_id)
    if pending is None:
        return {
            "status": "error",
            "error": f"pending-debrief not found for event_id={event_id}",
        }

    speakers = pending.get("krisp_speakers") or []
    meeting_title = (pending.get("meeting_title") or "").strip()
    meeting_start = (pending.get("meeting_start") or "").strip()
    active_md = ACTIVE_COMMITMENTS_FILE

    today_str = date.today().isoformat()
    seq = _next_commitment_sequence(active_md, today_str)

    fallback_owner = _first_name(speakers[0]) if speakers else "the operator"

    blocks: list[str] = []
    for raw in items or []:
        if isinstance(raw, dict):
            who, what, by_when = _extract_action_item(raw, speakers=speakers)
        else:
            who, what = _parse_replacement_item(str(raw))
            by_when = ""
            if what:
                what = what[0].upper() + what[1:] if len(what) > 1 else what.upper()
        if not what:
            continue
        who = who or fallback_owner
        entry_id = f"meetings-coach-{today_str}-{seq:03d}"
        seq += 1
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"## {entry_id}",
            f"- who: {who}",
            "- to_whom: the operator",
            f"- what: {what}",
        ]
        if by_when:
            lines.append(f"- by_when: {by_when}")
        lines.extend([
            "- status: open",
            f"- source_detail: {meeting_title} debrief "
            f"(event_id: {event_id}, meeting_start: {meeting_start})",
            "- source_agent: meetings-coach",
            f"- created_at: {created_at}",
        ])
        blocks.append("\n".join(lines) + "\n")

    if blocks:
        active_md.parent.mkdir(parents=True, exist_ok=True)
        if not active_md.exists():
            active_md.write_text(
                "# Commitments — Active\n\n", encoding="utf-8"
            )
        with active_md.open("a", encoding="utf-8") as f:
            for block in blocks:
                f.write("\n" + block)

    _delete_pending(event_id)
    return {"status": "ok", "written": len(blocks), "event_id": event_id}


# ─── coaching history ────────────────────────────────────────────────


def _load_coaching_history() -> list:
    """Return the coaching history as a flat list of entry dicts.

    Accepts two on-disk shapes:
      - ``[entry, entry, ...]`` — current Python format.
      - ``{"history": [entry, ...]}`` — legacy LLM-cron format.

    Until 2026-04-16 the loader silently returned [] for the dict
    shape, and _append then wrote a bare list that wiped the prior
    entries — which broke _already_coached dedup. Be tolerant here."""
    if not COACHING_HISTORY_FILE.exists():
        return []
    try:
        with COACHING_HISTORY_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("history")
        if isinstance(inner, list):
            return inner
    return []


def _already_coached(event_id: str) -> bool:
    if not event_id:
        return False
    for entry in _load_coaching_history():
        if isinstance(entry, dict) and str(entry.get("event_id")) == event_id:
            return True
    return False


def _append_coaching_history(entry: dict) -> None:
    history = _load_coaching_history()
    event_id = str(entry.get("event_id", ""))
    if event_id and any(
        isinstance(e, dict) and str(e.get("event_id")) == event_id for e in history
    ):
        return  # idempotent
    history.append(entry)
    COACHING_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COACHING_HISTORY_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    tmp.replace(COACHING_HISTORY_FILE)


# ─── cleanup_confirmed_pending ───────────────────────────────────────


def _active_event_ids() -> set:
    if not ACTIVE_COMMITMENTS_FILE.exists():
        return set()
    try:
        with ACTIVE_COMMITMENTS_FILE.open(encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return set()
    ids: set = set()
    for m in re.finditer(r"event_id\s*[:\*]+\s*([A-Za-z0-9\-_]+)", content):
        ids.add(m.group(1))
    return ids


def _cleanup_confirmed_pending() -> int:
    """Walk pending-debrief-*.json files; delete any whose event_id is
    already recorded in commitments/active.md. Returns count deleted.
    This function NEVER sends anything — it's orthogonal to delivery."""
    if not CACHE_DIR.exists():
        return 0
    active_ids = _active_event_ids()
    if not active_ids:
        return 0
    deleted = 0
    for pending_file in CACHE_DIR.glob("pending-debrief-*.json"):
        try:
            with pending_file.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        event_id = str((data or {}).get("event_id", ""))
        if event_id and event_id in active_ids:
            try:
                pending_file.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


# ─── Krisp 401 state machine ─────────────────────────────────────────


def _is_401_error(mcp_error: str | None) -> bool:
    if not mcp_error:
        return False
    text = str(mcp_error).lower()
    return "401" in text or "unauth" in text


def _stamp_krisp_401(now_iso: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(KRISP_LAST_401_FILE, json.dumps({"ts": now_iso}))


def _krisp_alert_rate_limited() -> bool:
    """True if we've sent a Krisp 401 alert within the last 90 min."""
    if not KRISP_LAST_ALERT_FILE.exists():
        return False
    try:
        mtime = KRISP_LAST_ALERT_FILE.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) < KRISP_ALERT_RATE_LIMIT_S


def _stamp_krisp_alert(now_iso: str) -> None:
    _write_atomic(KRISP_LAST_ALERT_FILE, json.dumps({"ts": now_iso}))


# ─── Coaching compose ────────────────────────────────────────────────


def _load_meeting_config() -> dict:
    if not MEETING_CONFIG_FILE.exists():
        return {}
    try:
        with MEETING_CONFIG_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _compose_coaching_message(
    pending: dict, metrics: dict, growth_areas: list
) -> str | None:
    """Use the LLM to assess each growth area, then format a single
    Telegram message. Returns None on LLM failure."""
    metrics_block = (metrics or {}).get("metrics", {}) if isinstance(metrics, dict) else {}
    # Feed the transcript body (speaker turns only) to the LLM. New
    # pending files are stored body-anchored by build_transcript_data,
    # but defend against stale files that still have the notes prefix.
    # Regression: 2026-04-16 Meet & Greet coaching said "too little
    # material" because the first 3000 chars were all notes.
    raw_transcript = pending.get("transcript_text") or ""
    m = _FIRST_TURN_RE.search(raw_transcript)
    transcript = raw_transcript[m.start():] if m else raw_transcript

    areas_text = "\n".join(
        f"- {a.get('id')}: {a.get('label')} — {a.get('description', '')}"
        for a in growth_areas
        if isinstance(a, dict) and a.get("id")
    )

    prompt = _COACHING_PROMPT_TEMPLATE.format(
        growth_areas=areas_text,
        meeting_title=pending.get("meeting_title") or "(untitled)",
        talk_ratio=metrics_block.get("talk_ratio", "n/a"),
        avg_turn=metrics_block.get("avg_brian_turn_words", "n/a"),
        questions=metrics_block.get("brian_questions", "n/a"),
        fillers=metrics_block.get("brian_filler_count", "n/a"),
        transcript=transcript,
    )

    # Retry once on transient broker/auth/parse failures. A single
    # retry catches the vast majority of flakes without inflating cron
    # cost. Regression: 2026-04-16 19:45 UTC Meet & Greet the operator/Steve
    # coaching silently dropped on a transient LLM ok=False.
    data = None
    for attempt in range(2):
        if attempt > 0:
            time.sleep(LLM_RETRY_BACKOFF_S)
        result = llm_infer(prompt, json_mode=True, timeout=LLM_TIMEOUT_S)
        if not getattr(result, "ok", False):
            continue
        try:
            data = json.loads(_strip_markdown_fence(result.text or ""))
            break
        except (json.JSONDecodeError, TypeError):
            continue
    if data is None:
        return None

    lines: list[str] = [
        f"\U0001f4ca Meeting Coach — {pending.get('meeting_title') or '(untitled)'}",
        "",
    ]

    emoji = {
        "concision": "\U0001f4cf",
        "structured_thinking": "\U0001f9e9",
        "empathy": "\U0001f91d",
    }

    for area in growth_areas:
        aid = (area.get("id") or "").lower()
        label = area.get("label") or aid
        section = data.get(aid) or {}
        if not isinstance(section, dict):
            continue
        assessment = (section.get("assessment") or "").strip()
        quote = (section.get("quote") or "").strip()
        ts = (section.get("timestamp") or "").strip()
        try_text = (section.get("try") or "").strip()

        marker = emoji.get(aid, "\U0001f4cc")
        lines.append(f"{marker} {label.upper()}")
        if not assessment and not quote and not try_text:
            lines.append("Nothing to flag.")
        else:
            if assessment:
                lines.append(assessment)
            if quote:
                ts_part = f" ({ts})" if ts else ""
                lines.append(f"\U0001f4ac \"{quote}\"{ts_part}")
            if try_text:
                lines.append(f"\u270f\ufe0f Try: \"{try_text}\"")
        lines.append("")

    metrics_footer = []
    if "talk_ratio" in metrics_block:
        metrics_footer.append(f"{int(metrics_block['talk_ratio'] * 100)}% talk")
    if "brian_turns" in metrics_block:
        metrics_footer.append(f"{metrics_block['brian_turns']} turns")
    if "brian_questions" in metrics_block:
        metrics_footer.append(f"{metrics_block['brian_questions']} questions")
    if "brian_filler_count" in metrics_block:
        metrics_footer.append(f"{metrics_block['brian_filler_count']} fillers")
    if metrics_footer:
        lines.append("\U0001f4ca " + " \u00b7 ".join(metrics_footer))
    lines.append("\U0001f437\U0001f50d")
    return "\n".join(lines).rstrip() + "\n"


# ─── run() ───────────────────────────────────────────────────────────


def _load_pending(event_id: str) -> dict | None:
    path = CACHE_DIR / f"pending-debrief-{event_id}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def run() -> dict:
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    # Populate gcal cache for transcript-scan. Return value intentionally
    # ignored — this is a cache warmer, not a data source. transcript-scan
    # tolerates a stale/missing gcal cache.
    _run_script("gcal-fetch.py")

    # Run transcript-scan — SINGLE SOURCE OF TRUTH for delivery. Without
    # it we have no way to know which transcripts are newly ready, so
    # propagate subprocess failures as a top-level error instead of the
    # old 'degraded' masking (2026-04-15 silent-outage class).
    scan = _run_script("transcript-scan.py")
    if is_subprocess_error(scan):
        error_msg = scan["__error__"]
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_iso,
                    "status": "error",
                    "error": error_msg,
                    "summary": f"transcript-scan failed: {error_msg[:120]}",
                },
                indent=2,
            ),
        )
        return {
            "status": "error",
            "error": error_msg,
            "alert": f"\U0001f437\U0001f50d post-meeting-scan failed: {error_msg[:200]}",
            "processed": 0,
            "debriefs_sent": 0,
            "coaching_sent": 0,
            "cleanup_count": 0,
            "krisp_401": False,
        }
    if not isinstance(scan, dict):
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps({"timestamp": now_iso, "status": "degraded",
                        "alert": "transcript-scan.py failed"}, indent=2),
        )
        return {
            "status": "degraded",
            "alert": "transcript-scan.py failed",
            "processed": 0,
            "debriefs_sent": 0,
            "coaching_sent": 0,
            "cleanup_count": 0,
            "krisp_401": False,
        }

    # Krisp 401 handling (runs BEFORE any send so we always know the
    # heartbeat state even if delivery short-circuits)
    mcp_error = scan.get("mcp_error") or ""
    krisp_401 = _is_401_error(mcp_error)
    if krisp_401:
        _stamp_krisp_401(now_iso)

    # Cleanup pass: delete stale pending files. Never sends.
    cleanup_count = _cleanup_confirmed_pending()

    # Delivery: ONLY iterate scan["processed"] — never the pending-debrief
    # files themselves. This is the 2026-04-14 Alexis incident invariant.
    processed = scan.get("processed") or []

    config = _load_meeting_config()
    coaching_cfg = (config.get("coaching") or {}) if isinstance(config, dict) else {}
    coaching_enabled = bool(coaching_cfg.get("enabled", False))
    growth_areas = coaching_cfg.get("growth_areas") or []

    debriefs_sent = 0
    coaching_sent = 0
    coaching_llm_failures = 0

    need_telegram = bool(processed) or krisp_401
    token = chat_id = None
    if need_telegram:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)

    # Krisp 401 alert (rate-limited)
    if krisp_401 and not _krisp_alert_rate_limited():
        alert_msg = (
            "\U0001f437\U0001f50d Krisp auth expired — I can't pull transcripts "
            "until you re-login. Run `krisp-auth-manual.py` when you have a "
            "moment."
        )
        if send_message(token, chat_id, alert_msg, silent=False):
            _stamp_krisp_alert(now_iso)

    # Process each item in scan.processed
    for item in processed:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("matched_event_id") or "")
        if not event_id:
            continue
        pending = _load_pending(event_id)
        if pending is None:
            continue

        # Suppress delivery of debriefs with zero extracted content.
        # Krisp sometimes returns a meeting doc with empty action_items
        # AND empty key_points (e.g. Google Meet with Steven Oliver,
        # 2026-04-16). Rendering a title-only debrief with Save/Dismiss
        # buttons is noise — the operator sees "junk" and has to dismiss.
        action_items = pending.get("krisp_action_items") or []
        key_points = pending.get("krisp_key_points") or []
        has_actionable_item = any(
            _extract_action_item(it, speakers=pending.get("krisp_speakers") or [])[1]
            for it in action_items
        )
        if not has_actionable_item and not key_points:
            continue

        debrief_msg = format_debrief(pending)
        keyboard = build_debrief_keyboard(pending)
        if send_message(
            token, chat_id, debrief_msg,
            silent=False, reply_markup=keyboard,
        ):
            debriefs_sent += 1

        # Coaching (dedup via coaching-history.json)
        if (
            coaching_enabled
            and growth_areas
            and item.get("has_raw_text")
            and not _already_coached(event_id)
        ):
            # Coaching is best-effort: a per-event metrics failure
            # should not abort the debrief loop for other events.
            metrics = _run_script("transcript-metrics.py", "--event-id", event_id)
            if is_subprocess_error(metrics) or metrics is None:
                coaching_llm_failures += 1
                continue
            # transcript-metrics emits {status: 'too_short'} for transcripts
            # below min_transcript_length (short meet-and-greets, etc.).
            # That's a legitimate skip, not a failure — don't call the LLM
            # with an empty metrics block, and don't count it as an error.
            # Regression: 2026-04-16 the operator/Steve 5-min Meet & Greet, where
            # this gate silently swallowed coaching while the debrief rendered.
            if isinstance(metrics, dict) and metrics.get("status") != "ok":
                continue
            coaching_msg = _compose_coaching_message(pending, metrics, growth_areas)
            if coaching_msg is None:
                coaching_llm_failures += 1
                continue
            if send_message(token, chat_id, coaching_msg, silent=False):
                coaching_sent += 1
                metrics_block = (metrics or {}).get("metrics", {}) if isinstance(metrics, dict) else {}
                _append_coaching_history(
                    {
                        "event_id": event_id,
                        "meeting_title": pending.get("meeting_title") or "",
                        "date": now_utc.strftime("%Y-%m-%d"),
                        "metrics": {
                            "talk_ratio": metrics_block.get("talk_ratio"),
                            "avg_turn_words": metrics_block.get("avg_brian_turn_words"),
                            "question_count": metrics_block.get("brian_questions"),
                            "filler_count": metrics_block.get("brian_filler_count"),
                        },
                    }
                )

    summary = {
        "timestamp": now_iso,
        "status": "ok",
        "processed": len(processed),
        "debriefs_sent": debriefs_sent,
        "coaching_sent": coaching_sent,
        "coaching_llm_failures": coaching_llm_failures,
        "cleanup_count": cleanup_count,
        "krisp_401": krisp_401,
    }
    _write_atomic(LAST_RUN_FILE, json.dumps(summary, indent=2))

    return {
        "status": "ok",
        "processed": len(processed),
        "debriefs_sent": debriefs_sent,
        "coaching_sent": coaching_sent,
        "coaching_llm_failures": coaching_llm_failures,
        "cleanup_count": cleanup_count,
        "krisp_401": krisp_401,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f437\U0001f50d post-meeting-scan failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
