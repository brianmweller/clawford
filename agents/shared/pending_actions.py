"""agents/shared/pending_actions.py — shared pending action store.

Every producer tool (propose_reorder, propose_event_add, etc.) stages
an action here instead of executing it directly. The dispatcher
auto-attaches [Confirm] [Cancel] inline buttons. When the user taps
Confirm, the dispatcher reads the action back, calls the per-kind
confirm executor with the stored payload, and removes it on success.

Storage: one JSON file per agent at
  ~/.clawford/<agent>-workspace/pending-actions.json

Thread safety: per-agent threading.Lock protects the read-modify-write
cycle. The daemon is single-process so Python locks suffice.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TTL_HOURS = 4

_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_lock(agent_id: str) -> threading.Lock:
    with _locks_lock:
        if agent_id not in _locks:
            _locks[agent_id] = threading.Lock()
        return _locks[agent_id]


def _pending_path(agent_id: str) -> Path:
    root = os.environ.get("CLAWFORD_WORKSPACE_ROOT") or os.path.expanduser("~/.clawford")
    return Path(root) / f"{agent_id}-workspace" / "pending-actions.json"


def _read(agent_id: str) -> dict:
    path = _pending_path(agent_id)
    if not path.exists():
        return {"updated_at": None, "actions": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "actions" not in data:
            return {"updated_at": None, "actions": []}
        return data
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "actions": []}


def _write(agent_id: str, data: dict) -> None:
    path = _pending_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _is_expired(action: dict) -> bool:
    expires_at = action.get("expires_at")
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
        return datetime.now(timezone.utc) >= exp
    except (ValueError, TypeError):
        return False


def stage(
    agent_id: str,
    kind: str,
    payload: dict,
    summary: str,
    *,
    confirm_label: str = "\u2705 Confirm",
    cancel_label: str = "\u274c Cancel",
    ttl_hours: float = DEFAULT_TTL_HOURS,
) -> dict:
    action_id = "act_" + secrets.token_hex(6)
    now = datetime.now(timezone.utc)
    action = {
        "id": action_id,
        "kind": kind,
        "agent_id": agent_id,
        "batch_id": None,
        "staged_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
        "summary": summary,
        "confirm_label": confirm_label,
        "cancel_label": cancel_label,
        "payload": payload,
    }

    lock = _get_lock(agent_id)
    with lock:
        data = _read(agent_id)
        data["actions"].append(action)
        _write(agent_id, data)

    return {
        "__pending_action__": {"id": action_id},
        "action_id": action_id,
        "summary": summary,
        "expires_in": f"{ttl_hours} hours",
    }


def load(agent_id: str) -> list[dict]:
    lock = _get_lock(agent_id)
    with lock:
        data = _read(agent_id)
    return [a for a in data.get("actions", []) if not _is_expired(a)]


def load_by_id(agent_id: str, action_id: str) -> dict | None:
    for action in load(agent_id):
        if action["id"] == action_id:
            return action
    return None


def load_by_batch(agent_id: str, batch_id: str) -> list[dict]:
    return [a for a in load(agent_id) if a.get("batch_id") == batch_id]


def remove(agent_id: str, action_id: str) -> dict | None:
    lock = _get_lock(agent_id)
    with lock:
        data = _read(agent_id)
        actions = data.get("actions", [])
        found = None
        remaining = []
        for a in actions:
            if a["id"] == action_id and found is None:
                found = a
            else:
                remaining.append(a)
        if found is None:
            return None
        data["actions"] = remaining
        _write(agent_id, data)
    return found


def assign_batch(agent_id: str, action_ids: list[str], batch_id: str) -> int:
    lock = _get_lock(agent_id)
    with lock:
        data = _read(agent_id)
        count = 0
        for a in data.get("actions", []):
            if a["id"] in action_ids:
                a["batch_id"] = batch_id
                count += 1
        if count > 0:
            _write(agent_id, data)
    return count


def prune_expired(agent_id: str) -> int:
    lock = _get_lock(agent_id)
    with lock:
        data = _read(agent_id)
        before = len(data.get("actions", []))
        data["actions"] = [a for a in data.get("actions", []) if not _is_expired(a)]
        after = len(data["actions"])
        pruned = before - after
        if pruned > 0:
            _write(agent_id, data)
    return pruned
