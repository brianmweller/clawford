"""Approve/reject helpers for pending-review facts.

The miner writes low-confidence extractions to
``<facts_dir>/_pending_review.md`` (markdown audit trail) and, when a
queue path is given, enqueues an item for the operator to resolve in the
morning brief. The Telegram callback router calls into this module
when he taps a button:

  ✅ Yes    → approve(...)   — move to canonical month file, bump
                               confidence to 0.95, stamp
                               operator_confirmed_at.
  ❌ No     → reject(...)    — append to _rejected.md with
                               rejected_at + rejected_reason; miner
                               consults this file to skip re-proposing
                               the same (subject, content) pair.
  ⏭  Skip   → handled by pending_queue.mute() in the dispatcher — no
               work here.

Atomic rewrites throughout: tmp + replace, matching the pattern in
facts.upsert_fact.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


_PENDING_FILE = "_pending_review.md"
_REJECTED_FILE = "_rejected.md"

_FIELD_RE = re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")


def _parse_blocks(text: str) -> list[tuple[str, dict]]:
    """Split markdown-per-block text into (raw_block, fields_dict) pairs."""
    out: list[tuple[str, dict]] = []
    for raw in re.split(r"\n---\n", text):
        block = raw.strip()
        if not block:
            continue
        fields: dict = {}
        for line in block.splitlines():
            m = _FIELD_RE.match(line)
            if m:
                fields[m.group(1).strip()] = m.group(2).strip()
        if fields.get("id"):
            out.append((raw, fields))
    return out


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ─── Pending-review lookup ───────────────────────────────────────────


def load_pending_fact(facts_dir: Path, fact_id: str) -> dict | None:
    """Return the parsed fact fields from _pending_review.md, or None."""
    path = facts_dir / _PENDING_FILE
    if not path.exists():
        return None
    for _raw, fields in _parse_blocks(path.read_text(encoding="utf-8")):
        if fields.get("id") == fact_id:
            out = dict(fields)
            try:
                out["confidence"] = float(out.get("confidence", "0") or 0)
            except ValueError:
                out["confidence"] = 0.0
            scope_raw = out.get("audience_scope", "")
            if scope_raw.startswith("["):
                try:
                    parsed = json.loads(scope_raw)
                    if isinstance(parsed, list):
                        out["audience_scope"] = [str(x) for x in parsed]
                except json.JSONDecodeError:
                    out["audience_scope"] = []
            return out
    return None


def _remove_block_from_pending(facts_dir: Path, fact_id: str) -> str | None:
    """Strip the fact block with matching id from _pending_review.md.
    Returns the removed raw block, or None when not found."""
    path = facts_dir / _PENDING_FILE
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    header, *tail = re.split(r"\n---\n", text, maxsplit=1)
    if not tail:
        return None  # No blocks; only header.
    rest = "\n---\n".join(tail) if tail else ""

    # Re-split preserving all blocks so we can reassemble losslessly.
    blocks = re.split(r"\n---\n", text)
    removed = None
    kept: list[str] = []
    for b in blocks:
        fields = {}
        for line in b.splitlines():
            m = _FIELD_RE.match(line)
            if m:
                fields[m.group(1).strip()] = m.group(2).strip()
        if fields.get("id") == fact_id and removed is None:
            removed = b
            continue
        kept.append(b)
    if removed is None:
        return None
    _atomic_write(path, "\n---\n".join(kept))
    return removed


# ─── approve ─────────────────────────────────────────────────────────


def approve(facts_dir: Path, fact_id: str, *, recorded_at: str) -> dict:
    """Promote a pending fact into the canonical month file. Returns
    {status: approved|not_found, path, new_confidence}."""
    fact = load_pending_fact(facts_dir, fact_id)
    if fact is None:
        return {"status": "not_found"}

    month = recorded_at[:7]  # YYYY-MM
    month_path = facts_dir / f"{month}.md"
    is_new = not month_path.exists()

    entry_lines = [
        "",
        "---",
        "",
        f"- **id:** {fact.get('id', '')}",
        f"- **content:** {fact.get('content', '')}",
        f"- **subject:** {fact.get('subject', '')}",
        f"- **source_type:** operator_promoted",
        f"- **source_detail:** {fact.get('source') or fact.get('source_detail', '')}",
        f"- **source_agent:** connector",
        f"- **confidence:** 0.95",
        f"- **category:** {fact.get('category', '')}",
        f"- **recorded_at:** {recorded_at}",
        f"- **operator_confirmed_at:** {recorded_at}",
    ]
    scope = fact.get("audience_scope")
    if isinstance(scope, list) and scope:
        entry_lines.append(f"- **audience_scope:** {json.dumps(scope)}")
    elif isinstance(scope, str) and scope.strip():
        entry_lines.append(f"- **audience_scope:** {scope}")

    entry = "\n".join(entry_lines) + "\n"
    facts_dir.mkdir(parents=True, exist_ok=True)
    with open(month_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# Facts — {month}\n")
        f.write(entry)

    _remove_block_from_pending(facts_dir, fact_id)
    return {
        "status": "approved",
        "path": str(month_path),
        "new_confidence": 0.95,
    }


# ─── reject ──────────────────────────────────────────────────────────


_REJECTED_HEADER = "# Rejected facts — training signal for miner dedupe\n"


def reject(facts_dir: Path, fact_id: str, *, rejected_at: str,
           reason: str = "operator") -> dict:
    """Move a pending fact to _rejected.md. Returns
    {status: rejected|not_found, path}."""
    fact = load_pending_fact(facts_dir, fact_id)
    if fact is None:
        return {"status": "not_found"}

    rejected_path = facts_dir / _REJECTED_FILE
    is_new = not rejected_path.exists()
    entry_lines = [
        "",
        "---",
        "",
        f"- **id:** {fact.get('id', '')}",
        f"- **subject:** {fact.get('subject', '')}",
        f"- **content:** {fact.get('content', '')}",
        f"- **category:** {fact.get('category', '')}",
        f"- **source:** {fact.get('source') or fact.get('source_detail', '')}",
        f"- **rejected_at:** {rejected_at}",
        f"- **rejected_reason:** {reason}",
    ]
    entry = "\n".join(entry_lines) + "\n"
    facts_dir.mkdir(parents=True, exist_ok=True)
    with open(rejected_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(_REJECTED_HEADER)
        f.write(entry)

    _remove_block_from_pending(facts_dir, fact_id)
    return {"status": "rejected", "path": str(rejected_path)}


# ─── Skip signatures for miner dedupe ────────────────────────────────


def signature_for(*, subject: str, content: str) -> tuple[str, str]:
    """Return (subject_lower, content_sha1_short) — the stable
    signature the miner uses to skip re-proposing a rejected claim.
    Whitespace is normalized so cosmetic variations don't bust the
    match; case is preserved (miner content is emitted by the LLM
    with natural casing)."""
    subj = subject.strip().lower()
    normalized = " ".join((content or "").split())
    content_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return (subj, content_hash)


def load_rejected_signatures(facts_dir: Path) -> set[tuple[str, str]]:
    """Return the set of (subject, content_hash) signatures for every
    fact in _rejected.md. Used by the miner to skip re-extraction."""
    path = facts_dir / _REJECTED_FILE
    if not path.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for _raw, fields in _parse_blocks(path.read_text(encoding="utf-8")):
        subj = fields.get("subject", "")
        content = fields.get("content", "")
        if not subj or not content:
            continue
        out.add(signature_for(subject=subj, content=content))
    return out
