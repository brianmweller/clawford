"""Pure helpers for the Flux → Huckle fact import.

Flux stores facts in SQLite (flux/data/flux.db, knowledge_facts table).
Schema columns relevant to this import:
  id, fact_type, subject, subject_address, content, source_type,
  source_id, audience_scope (JSON), confidence, status, established_at.

Huckle's upsert_fact() consumes:
  subject (slug), category, content, source_agent, source_type,
  source_detail, confidence, idempotency_key, recorded_at,
  audience_scope.

build_email_to_slug_map() builds the email → Huckle-slug lookup so we
can translate Flux's subject_address into Huckle's subject. Facts about
unknown subjects are dropped (caller decides whether to log).

flux_row_to_huckle_fact() does the row-level mapping. Returns None when
the fact should be skipped (unknown subject, empty content, missing
subject_address).
"""
from __future__ import annotations

import json
import re
from pathlib import Path


_EMAIL_FIELD_RE = re.compile(r"^\s*-\s*\*\*email(?::\*\*|\*\*:)\s*(.+?)\s*$")
_SLUG_FIELD_RE = re.compile(r"^\s*-\s*\*\*slug(?::\*\*|\*\*:)\s*(.+?)\s*$")


def build_email_to_slug_map(people_dir: Path) -> dict[str, str]:
    """Scan every people/*.md (skipping _template.md etc.) and return a
    lowercase email → slug map. People files without an email or with
    placeholder values (em-dash, empty) are skipped."""
    if not people_dir.exists():
        return {}
    out: dict[str, str] = {}
    for path in people_dir.glob("*.md"):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        email = None
        slug = None
        for line in text.splitlines():
            if email is None:
                m = _EMAIL_FIELD_RE.match(line)
                if m:
                    email = m.group(1).strip()
            if slug is None:
                m = _SLUG_FIELD_RE.match(line)
                if m:
                    slug = m.group(1).strip()
            if email is not None and slug is not None:
                break
        if not email or email in {"—", "-", ""} or "@" not in email:
            continue
        if not slug:
            slug = path.stem
        out[email.lower()] = slug
    return out


def parse_audience_scope(raw) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(x) for x in raw]
    s = str(raw).strip()
    if not s or s.lower() == "null":
        return None
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (json.JSONDecodeError, TypeError):
        return None
    return None


def flux_row_to_huckle_fact(row: dict, email_to_slug: dict[str, str]) -> dict | None:
    """Map a Flux knowledge_facts row (dict-like) to the kwargs that
    upsert_fact() accepts. Returns None if the fact should be skipped."""
    subject_address = row.get("subject_address")
    if not subject_address:
        return None
    slug = email_to_slug.get(subject_address.lower())
    if not slug:
        return None
    content = (row.get("content") or "").strip()
    if not content:
        return None

    recorded_at = row.get("established_at") or row.get("created_at") or ""
    if recorded_at and not str(recorded_at).endswith("Z") and "T" in str(recorded_at):
        # established_at sometimes stored without trailing Z; normalize for consistency
        recorded_at = str(recorded_at)

    return {
        "subject": slug,
        "category": row.get("fact_type") or "fact",
        "content": content,
        "source_agent": "flux",
        "source_type": row.get("source_type") or "flux-import",
        "source_detail": row.get("source_id") or "",
        "confidence": float(row.get("confidence") or 0.8),
        "idempotency_key": str(row.get("id")),
        "recorded_at": str(recorded_at),
        "audience_scope": parse_audience_scope(row.get("audience_scope")),
    }
