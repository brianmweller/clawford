#!/usr/bin/env python3
"""notes-triage-alert.py — Connector (Huckle Cat) notes triage alerter.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`connector:notes-triage`. Runs the existing I/O script notes-triage.py
to fetch untriaged inbox notes, skips IDs already staged in
pending-triage.json, LLM-classifies each new note, and sends a single
Telegram message presenting up to `max_batch_size` items with /confirm
and /dismiss N commands.

LLM use here is legit under the logic-gate rule — note contents are
free-form natural language where keyword matching is brittle.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
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


WORKSPACE = Path(os.path.expanduser("~/.clawford/connector-workspace"))
SCRIPTS_DIR = WORKSPACE / "scripts"
CACHE_DIR = WORKSPACE / "cache"
TRIAGE_FILE = WORKSPACE / "pending-triage.json"
LAST_RUN_FILE = CACHE_DIR / "last-notes-triage.json"

BOT_TOKEN_ENV = "CONNECTOR_BOT_TOKEN"
SUBPROCESS_TIMEOUT_S = 60
LLM_TIMEOUT_S = 30
MAX_BATCH_SIZE = 10

VALID_CATEGORIES = {"fact", "commitment", "task", "shopping", "unclear"}

_PROMPT_TEMPLATE = """You categorize inbox notes for the operator's personal
knowledge system. Pick exactly one category based on what the note is
about.

Categories:
- fact: information about a person or situation (e.g., "Priya is
  traveling next week"; "Maya prefers text over email")
- commitment: someone promised something, or the operator promised
  something to someone (e.g., "told Ravi I'd look into the rebalance
  by Friday")
- task: an action the operator needs to take for themself (e.g.,
  "book dentist for Avery")
- shopping: a physical item to buy (e.g., "more paper towels")
- unclear: ambiguous, too little context, or doesn't fit above

Return JSON ONLY, no markdown fences:
{{"category": "fact|commitment|task|shopping|unclear", "reason": "..."}}

Note to categorize:
{content}
"""


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


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


def classify_note(note: dict) -> tuple[dict, bool]:
    """Classify a single note via llm.infer. Returns
    (classification_dict, llm_failed_bool). On any failure — LLM down,
    malformed JSON, unknown category — falls through to category
    'unclear' so the orchestrator keeps moving.
    """
    content = (note.get("content") or "").strip()
    prompt = _PROMPT_TEMPLATE.format(content=content[:1200])
    result = llm_infer(prompt, json_mode=True, timeout=LLM_TIMEOUT_S)
    if not getattr(result, "ok", False):
        return {"category": "unclear", "reason": ""}, True

    try:
        data = json.loads(_strip_markdown_fence(result.text or ""))
    except (json.JSONDecodeError, TypeError):
        return {"category": "unclear", "reason": ""}, True

    category = (data.get("category") or "unclear").strip().lower()
    if category not in VALID_CATEGORIES:
        category = "unclear"
    reason = (data.get("reason") or "").strip()
    return {"category": category, "reason": reason}, False


def _load_pending_ids() -> set:
    if not TRIAGE_FILE.exists():
        return set()
    try:
        with TRIAGE_FILE.open(encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(entries, list):
        return set()
    return {str(e.get("id")) for e in entries if isinstance(e, dict) and e.get("id")}


def _append_pending(note_ids: list[str]) -> None:
    existing: list = []
    if TRIAGE_FILE.exists():
        try:
            with TRIAGE_FILE.open(encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = []
    if not isinstance(existing, list):
        existing = []

    stamp = datetime.now(timezone.utc).isoformat()
    known = {
        str(e.get("id"))
        for e in existing
        if isinstance(e, dict) and e.get("id")
    }
    for nid in note_ids:
        if nid in known:
            continue
        existing.append({"id": nid, "created_at": stamp})

    tmp = TRIAGE_FILE.with_suffix(".json.tmp")
    TRIAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    tmp.replace(TRIAGE_FILE)


def format_message(classified: list, hidden: int = 0) -> str:
    """Render the batch as a single Telegram message.

    `classified` is a list of (note_dict, classification_dict) tuples.
    """
    count = len(classified)
    lines: list[str] = [
        f"\U0001f431\U0001f91d Notes to triage — {count} new",
        "",
    ]
    for idx, (note, classification) in enumerate(classified, start=1):
        content = (note.get("content") or "").strip()
        category = classification.get("category", "unclear")
        reason = (classification.get("reason") or "").strip()
        lines.append(f"\U0001f4dd {idx}. \"{content}\"")
        if reason:
            lines.append(f"   \u2192 {category} ({reason})")
        else:
            lines.append(f"   \u2192 {category}")
        lines.append("")

    if hidden > 0:
        lines.append(f"\u2026 {hidden} more queued. Handle these first.")
        lines.append("")

    lines.append("/confirm to save all \u00b7 /dismiss N to skip item N")
    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)

    payload = _run_script("notes-triage.py")

    # notes-triage.py is the only data source. Propagate subprocess
    # failures instead of masking them as "no new notes".
    if is_subprocess_error(payload):
        error_msg = payload["__error__"]
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "error",
                    "error": error_msg,
                    "summary": f"notes-triage failed: {error_msg[:120]}",
                },
                indent=2,
            ),
        )
        return {
            "status": "error",
            "error": error_msg,
            "alert": f"🐱 notes-triage-alert failed: {error_msg[:200]}",
        }

    untriaged: list = []
    if isinstance(payload, dict):
        untriaged = payload.get("untriaged") or []

    pending_ids = _load_pending_ids()
    new_notes = [
        n for n in untriaged
        if isinstance(n, dict) and n.get("id") and str(n.get("id")) not in pending_ids
    ]

    hidden = 0
    if len(new_notes) > MAX_BATCH_SIZE:
        hidden = len(new_notes) - MAX_BATCH_SIZE
        new_notes = new_notes[:MAX_BATCH_SIZE]

    classified: list[tuple[dict, dict]] = []
    llm_failures = 0
    for note in new_notes:
        classification, failed = classify_note(note)
        if failed:
            llm_failures += 1
        classified.append((note, classification))

    sent_count = 0
    if classified:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        msg = format_message(classified, hidden=hidden)
        if send_message(token, chat_id, msg, silent=False):
            sent_count = 1
            _append_pending([str(n.get("id")) for n in new_notes])

    summary = {
        "timestamp": now_utc.isoformat(),
        "status": "ok",
        "untriaged_seen": len(untriaged),
        "new_notes": len(new_notes),
        "hidden": hidden,
        "sent": sent_count,
        "llm_failures": llm_failures,
    }
    _write_atomic(LAST_RUN_FILE, json.dumps(summary, indent=2))

    return {
        "status": "ok",
        "untriaged_seen": len(untriaged),
        "new_notes": len(new_notes),
        "hidden": hidden,
        "sent": sent_count,
        "llm_failures": llm_failures,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f431\U0001f91d notes-triage-alert failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
