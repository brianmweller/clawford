"""agents/shared/conversation.py — per-agent conversation window.

A sliding window of the last N input-items per agent, persisted to
~/.clawford/inbox/<agent_id>.jsonl so daemon restarts don't wipe memory.
Every LLM call from the dispatcher loads the window, trims for age +
size, prepends to the current user turn, and sends. After the reply
(whether text or tool-use), the new items are appended.

Items are OpenAI Responses input-items:
- {role: "user"|"assistant", content: "..."}
- {type: "function_call", call_id, name, arguments}
- {type: "function_call_output", call_id, output}

Invariants:
- WINDOW_SIZE items max per load (older items exist on disk but are
  not returned). ~20 items = ~10 turns for typical chat.
- INACTIVITY_TIMEOUT_S = 3600: if the last entry is older, load()
  returns an empty list — the model sees a fresh session. This
  prevents week-old context from leaking into a new conversation.
- JSONL append is line-atomic for single-writer. The dispatcher is
  the only writer per agent_id in practice.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

WINDOW_SIZE = 20
INACTIVITY_TIMEOUT_S = 3600

DEFAULT_INBOX_DIR = os.path.expanduser("~/.clawford/inbox")
INBOX_DIR = os.environ.get("CLAWFORD_INBOX_DIR", DEFAULT_INBOX_DIR)


def _log_path(agent_id: str) -> Path:
    return Path(INBOX_DIR) / f"{agent_id}.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def append(agent_id: str, item: dict) -> None:
    """Append one input-item to the agent's conversation log."""
    path = _log_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": _now_iso(), "agent_id": agent_id, "item": item}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load(agent_id: str) -> list[dict]:
    """Return the current conversation window for the agent.

    Returns up to WINDOW_SIZE most-recent items. If the most recent
    entry is older than INACTIVITY_TIMEOUT_S, returns []. Malformed
    JSONL lines are silently skipped.
    """
    path = _log_path(agent_id)
    if not path.exists():
        return []

    entries: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        return []

    last_ts = _parse_ts(entries[-1].get("ts", ""))
    if last_ts is not None:
        age = (datetime.now(timezone.utc) - last_ts).total_seconds()
        if age > INACTIVITY_TIMEOUT_S:
            return []

    window = entries[-WINDOW_SIZE:]
    items = [e["item"] for e in window if "item" in e]
    return _drop_orphan_tool_outputs(items)


def _drop_orphan_tool_outputs(items: list[dict]) -> list[dict]:
    """Filter out any function_call_output whose paired function_call
    isn't in the window.

    The codex Responses API rejects input with a `function_call_output`
    that has no preceding `function_call` for the same call_id
    ("No tool call found for function call output"). This happens when
    the 20-item sliding window trims mid-pair — the function_call falls
    off the front while its output becomes the first item in the
    window. Walk once, tracking which call_ids have been seen as
    function_call, and drop any function_call_output that references
    an unseen id.
    """
    seen_call_ids: set[str] = set()
    result: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "function_call":
            cid = item.get("call_id")
            if cid:
                seen_call_ids.add(cid)
            result.append(item)
        elif itype == "function_call_output":
            cid = item.get("call_id")
            if cid and cid in seen_call_ids:
                result.append(item)
            # else drop — orphan (paired function_call is outside window)
        else:
            result.append(item)
    return result
