"""Append-only JSONL queue for brain-maintenance items surfaced to
the operator's Telegram.

Producers (miner low-conf pass, structured-type migration, embedding
dedupe) append entries. The morning-brief digest reads active entries
and renders them as tap-to-resolve messages. Callback handlers call
``remove`` (approve / reject) or ``mute`` (skip) to resolve items.

Every entry is a single JSON object on one line. Required fields:
- id: string, globally unique (used for callback_data routing)
- source: "miner" | "migration" | "dedupe"

Optional fields the producers add:
- fact: dict with subject, content, confidence, ...
- question: one-line human-readable prompt
- options: ["approve", "reject", "skip"] or richer choices
- muted_until: ISO timestamp; entries whose muted_until > now are
  filtered out of load_active()
- created_at: ISO timestamp

Atomicity: append uses O_APPEND on POSIX (atomic for lines < PIPE_BUF).
Rewrites (mute / remove) go through a tmp file + os.replace, same
pattern as facts.upsert_fact.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


def _iter_valid_lines(path: Path) -> Iterable[dict]:
    """Yield parsed JSON objects from the file, skipping malformed lines."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for ln, raw in enumerate(f, start=1):
            raw = raw.rstrip("\n")
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("pending_queue: skipping malformed line %d in %s", ln, path)
                continue
            if isinstance(obj, dict):
                yield obj


def load(path: Path) -> list[dict]:
    """Return every entry in the queue, in append order. Missing file or
    malformed lines degrade to [] — never raises."""
    return list(_iter_valid_lines(path))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_active(entry: dict, now_iso: str) -> bool:
    muted = entry.get("muted_until")
    if not muted:
        return True
    # ISO-sortable comparison is safe because both strings are UTC Z-normal.
    return str(muted) < now_iso


def load_active(path: Path, now: str | None = None) -> list[dict]:
    """Return only entries whose muted_until is absent or < now.
    `now` defaults to the current UTC timestamp. Same degrade-to-empty
    behavior as load()."""
    now_iso = now or _now_iso()
    return [e for e in _iter_valid_lines(path) if _is_active(e, now_iso)]


def find_by_id(path: Path, entry_id: str) -> dict | None:
    """Return the (first) entry with matching id, or None."""
    for e in _iter_valid_lines(path):
        if str(e.get("id") or "") == entry_id:
            return e
    return None


def append(path: Path, entry: dict) -> None:
    """Append one entry to the queue. Idempotent on `id` — if an entry
    with the same id already exists, the call is a no-op.

    Uses O_APPEND (single-write atomicity for lines under PIPE_BUF, which
    any reasonable queue entry is). For stronger durability guarantees a
    caller would need fsync; this queue's loss window on unclean shutdown
    is bounded to "the most recent append or two," which is acceptable
    because producers re-surface items on the next run.
    """
    entry_id = str(entry.get("id") or "")
    if not entry_id:
        raise ValueError("pending_queue.append: entry must carry a non-empty 'id'")

    # Idempotency: scan existing file for the id.
    if path.exists():
        for e in _iter_valid_lines(path):
            if str(e.get("id") or "") == entry_id:
                return  # already present

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def _atomic_rewrite(path: Path, entries: list[dict]) -> None:
    """Write `entries` to `path` via tmp + replace. Empty list still
    writes an empty file (removes stale content)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def mute(path: Path, entry_id: str, until_iso: str) -> bool:
    """Set muted_until on the matching entry. Returns True when the
    entry was found and updated, False when not present."""
    entries = list(_iter_valid_lines(path))
    touched = False
    for e in entries:
        if str(e.get("id") or "") == entry_id:
            e["muted_until"] = until_iso
            touched = True
            break
    if touched:
        _atomic_rewrite(path, entries)
    return touched


def remove(path: Path, entry_id: str) -> bool:
    """Delete the matching entry. Returns True when the entry was found
    and removed, False when not present (no-op)."""
    entries = list(_iter_valid_lines(path))
    kept = [e for e in entries if str(e.get("id") or "") != entry_id]
    if len(kept) == len(entries):
        return False
    _atomic_rewrite(path, kept)
    return True
