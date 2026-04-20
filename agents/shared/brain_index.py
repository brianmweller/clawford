"""brain_index — per-subject fact index over brain/facts/*.md.

Context: load_facts_for_subject() used to re-parse every monthly
`facts/YYYY-MM.md` on every call (~8K lines at 610 facts today, growing
with the 6h miner cadence). That's fine at current scale but won't
scale past 1K+ facts. This module builds a subject → [[month, fact_id]…]
index so readers can open only the files that actually contain facts
about the subject being drafted.

The index is a HINT, not a source of truth. A nightly cron rebuilds
it at 2:45 AM PT (after miners, before brief-gen); consumers check
`is_fresh()` against monthly-file mtimes and fall back to the slow
full scan whenever the index is absent, stale, or corrupt.

Index layout (`brain/facts/_index.json`):
    {
      "by_subject": {
        "priya-rivera": [["2026-03", "f-old"], ["2026-04", "f-new"]],
        "aaron-nuti":    [["2026-04", "f-002"]]
      },
      "built_at": "2026-04-20T09:45:12Z",
      "built_at_epoch": 1776678312.0
    }

Subject keys are lowercase (lookup normalization done at index-build
time, not at every read).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


INDEX_FILENAME = "_index.json"


_ID_RE = re.compile(r"^\s*-\s*\*\*id:\*\*\s*(.+)$")
_SUBJECT_RE = re.compile(r"^\s*-\s*\*\*subject:\*\*\s*(.+)$")


def _iter_fact_files(facts_dir: Path) -> list[Path]:
    """Yield monthly fact files. Excludes the index and any
    underscore-prefixed file (reserved for internal state like
    `_pending_review.md`)."""
    if not facts_dir.exists():
        return []
    return sorted(
        p for p in facts_dir.glob("*.md")
        if not p.name.startswith("_")
    )


def rebuild_index(facts_dir: Path) -> dict:
    """Scan every monthly fact file in facts_dir and build a fresh
    subject→entries index. Returns the index dict; does NOT persist
    it (save_index does that)."""
    by_subject: dict[str, list[list[str]]] = {}
    for path in _iter_fact_files(facts_dir):
        month = path.stem  # YYYY-MM
        current_id: str | None = None
        current_subject: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped == "---":
                if current_id and current_subject:
                    subj = current_subject.lower()
                    by_subject.setdefault(subj, []).append([month, current_id])
                current_id = None
                current_subject = None
                continue
            m = _ID_RE.match(line)
            if m:
                current_id = m.group(1).strip()
                continue
            m = _SUBJECT_RE.match(line)
            if m:
                current_subject = m.group(1).strip()
        # Flush trailing block (no final `---`).
        if current_id and current_subject:
            subj = current_subject.lower()
            by_subject.setdefault(subj, []).append([month, current_id])

    now = datetime.now(timezone.utc)
    return {
        "by_subject": by_subject,
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "built_at_epoch": now.timestamp(),
    }


def save_index(facts_dir: Path, index: dict) -> Path:
    """Write the index atomically (tmp + replace) to
    facts_dir/_index.json. Returns the index path."""
    facts_dir.mkdir(parents=True, exist_ok=True)
    path = facts_dir / INDEX_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def load_index(facts_dir: Path) -> dict | None:
    """Read the index, or return None if absent / corrupt. The caller
    is expected to fall back to a full scan when None is returned."""
    path = facts_dir / INDEX_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "by_subject" not in data:
        return None
    return data


def is_fresh(facts_dir: Path, index: dict | None) -> bool:
    """Return True when the index is safe to trust: no monthly fact
    file has been modified since index["built_at_epoch"]. If any has,
    callers must fall back to the slow path (rebuild or full scan).

    The index's own file is NOT considered — saving it after a rebuild
    shouldn't invalidate itself.
    """
    if index is None:
        return False
    built_epoch = index.get("built_at_epoch")
    if not isinstance(built_epoch, (int, float)):
        return False
    for path in _iter_fact_files(facts_dir):
        try:
            if path.stat().st_mtime > built_epoch:
                return False
        except OSError:
            return False
    return True


def facts_for_subject(index: dict, subject_slug: str) -> list[list[str]]:
    """Return the list of [month, fact_id] entries for the subject.
    Empty list when the subject has no indexed facts. Lookup is
    case-insensitive."""
    by_subject = index.get("by_subject") or {}
    return list(by_subject.get(subject_slug.lower(), []))
