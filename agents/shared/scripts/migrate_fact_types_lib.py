"""Pure helpers for agents/shared/scripts/migrate-fact-types.py.

The migration: every fact currently in the brain carries free-form
`content`. Phase 2 adds optional `fact_type` + `value` on top. This
module provides the building blocks for a one-time pass that
classifies legacy narrative content into one of four types
(birthdate / employer / role / preference) and splices the structured
fields in place, atomically, idempotently.

Design (from the approved plan):
  - Re-use fact_extraction's per-type validators so the migration and
    the miner agree on what shape a value must have.
  - Classify per-fact via the Codex broker; confidence ≥ 0.8 applies
    directly, 0.5–0.8 routes to the Phase 4 pending-review queue for
    operator decision, < 0.5 leaves the fact unchanged.
  - In-place rewrite only blocks missing fact_type — already-typed
    facts are untouched (second-run idempotence).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

# Reuse the same validators the miner uses so the migration and the
# extractor agree on what a well-formed `value` looks like per type.
# fact_extraction imports `agents.shared.operator`, so repo root needs
# to be on sys.path too — not just the agents/shared dir.
_shared = Path(__file__).resolve().parent.parent
_repo = _shared.parent.parent
for _p in (_shared, _repo):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fact_extraction import (  # type: ignore[import-not-found]
    _VALIDATORS,
    _normalize_structured_fields,
)


_FIELD_RE = re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")

HIGH_CONFIDENCE_THRESHOLD = 0.8
MEDIUM_CONFIDENCE_THRESHOLD = 0.5


# ─── Block parsing / iteration ───────────────────────────────────────


def _fields_from_block(block: str) -> dict:
    fields: dict = {}
    for line in block.splitlines():
        m = _FIELD_RE.match(line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def iter_facts_to_classify(path: Path) -> Iterable[tuple[str, dict]]:
    """Yield (raw_block, fields_dict) for every fact in `path` that
    does NOT already carry a fact_type. Blocks already typed are
    skipped — migration must be idempotent.
    """
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for raw in re.split(r"\n---\n", text):
        block = raw.strip()
        if not block:
            continue
        fields = _fields_from_block(block)
        if not fields.get("id"):
            continue
        if fields.get("fact_type", ""):
            continue
        yield raw, fields


# ─── Block rewriting ─────────────────────────────────────────────────


def rewrite_block_with_fact_type(
    block: str,
    *,
    fact_type: str,
    value: dict,
) -> str:
    """Splice `- **fact_type:** ...` and `- **value:** ...` lines into
    a fact block, preserving every other line. Inserts after the
    audience_scope line when present, else at the end.

    Idempotent: if the block already carries a fact_type, returns it
    unchanged (the plan requires that re-running migration be a no-op
    on previously-typed facts).
    """
    existing = _fields_from_block(block)
    if existing.get("fact_type", ""):
        return block

    new_lines = [
        f"- **fact_type:** {fact_type}",
        f"- **value:** {json.dumps(value, ensure_ascii=False)}",
    ]
    lines = block.splitlines()

    # Prefer inserting after audience_scope so the typed fields hug the
    # narrative-era fields rather than getting lost at the bottom.
    insert_idx = len(lines)
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("- **audience_scope:"):
            insert_idx = i + 1
            break

    out = lines[:insert_idx] + new_lines + lines[insert_idx:]
    return "\n".join(out)


def rewrite_month_file(path: Path, applications: dict[str, tuple[str, dict]]) -> int:
    """Rewrite every fact block in `path` whose id is a key in
    `applications`, splicing in the matching (fact_type, value).
    Returns the count of facts actually modified.

    Atomic via tmp + replace. `applications` is pre-validated by the
    caller; any fact already carrying fact_type is left alone.
    """
    if not applications or not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    blocks = text.split("\n---\n")
    applied = 0
    out: list[str] = []
    for b in blocks:
        fields = _fields_from_block(b)
        fid = fields.get("id", "")
        if fid and fid in applications and not fields.get("fact_type", ""):
            ft, val = applications[fid]
            b = rewrite_block_with_fact_type(b, fact_type=ft, value=val)
            applied += 1
        out.append(b)

    new_text = "\n---\n".join(out)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return applied


# ─── Classifier prompt ───────────────────────────────────────────────


_CLASSIFIER_PROMPT_TEMPLATE = """Classify the following durable fact into one of four structured types, or `none`.

Valid types:
  - birthdate   (any age or birth claim; value holds year/month/precision)
  - employer    (person works at a named org; value holds company/role/status)
  - role        (title-only, org unknown; value holds title/status)
  - preference  (person likes/dislikes a thing; value holds domain/item/polarity)
  - none        (narrative doesn't map cleanly to any of the four)

Emit the JSON schema the miner would have produced had this fact been
typed at extraction time. Respond with a JSON object with exactly these
fields:

  fact_type:              string — one of birthdate, employer, role, preference, none
  value:                  object | null — shape depends on fact_type (see below)
  migration_confidence:   float 0.0-1.0 — how confident you are that fact_type is correct
                          AND that the value's required keys are correctly populated
                          from the narrative content

Per-type value shapes:
  birthdate:   {{"year": <int>, "month": <int 1-12|null>, "day": <int 1-31|null>,
                "precision": "year"|"month"|"day"}}
  employer:    {{"company": <str required>, "role": <str>, "level": "IC"|"Manager"|"Director"|"VP"|"CxO"|"Founder"|null,
                "functional_area": <str>, "start_date": <ISO>, "end_date": <ISO or null>,
                "status": "current"|"former"}}
  role:        {{"title": <str required>, "level": <str>, "functional_area": <str>,
                "status": "current"|"exploring"|"former"}}
  preference:  {{"domain": <str required>, "item": <str required>,
                "polarity": "likes"|"dislikes"|"prefers"|"avoids",
                "strength": "strong"|"moderate"|"mild"|null, "context": <str>}}

Overlap rule: if the claim includes BOTH a title AND an org, use
"employer" (with role nested). Title-only → "role".

When the narrative doesn't fit any type confidently, emit
`fact_type: "none"`, `value: null`, and a low migration_confidence.

FACT TO CLASSIFY
  subject:  {subject}
  category: {category}
  content:  {content}
"""


def build_classifier_prompt(*, content: str, subject: str, category: str) -> str:
    return _CLASSIFIER_PROMPT_TEMPLATE.format(
        content=content,
        subject=subject,
        category=category,
    )


# ─── Classifier response parsing ─────────────────────────────────────


def _strip_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return cleaned


def parse_classifier_response(raw_text: str) -> dict:
    """Parse + validate the classifier's JSON response. Returns a dict
    with {fact_type, value, migration_confidence}. Failures (malformed
    JSON, invalid per-type value) collapse to {"", None, 0.0}."""
    cleaned = _strip_fences(raw_text)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return {"fact_type": "", "value": None, "migration_confidence": 0.0}
    if not isinstance(parsed, dict):
        return {"fact_type": "", "value": None, "migration_confidence": 0.0}

    ft_raw = str(parsed.get("fact_type") or "").strip()
    if ft_raw == "none":
        ft_raw = ""
    value_raw = parsed.get("value")

    try:
        conf = float(parsed.get("migration_confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0

    # Re-validate via the shared per-type validators. An invalid value
    # collapses the whole row; we'd rather leave the fact narrative-only
    # than inject malformed structure.
    ft, val = _normalize_structured_fields(ft_raw, value_raw)
    return {"fact_type": ft, "value": val, "migration_confidence": conf}


# ─── Route decision ──────────────────────────────────────────────────


def classify_route(migration_confidence: float) -> str:
    """Return 'apply' (≥ 0.8), 'queue' (0.5 ≤ x < 0.8), or 'skip' (< 0.5)."""
    if migration_confidence >= HIGH_CONFIDENCE_THRESHOLD:
        return "apply"
    if migration_confidence >= MEDIUM_CONFIDENCE_THRESHOLD:
        return "queue"
    return "skip"
