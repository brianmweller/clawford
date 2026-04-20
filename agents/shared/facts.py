"""Shared fact-file reader with audience_scope support.

Extends the schema in agents/fix-it/scripts/monthly-archival.py by extracting
an audience_scope field so Huckle Cat's draft composer can filter facts
through agents/shared/audience.fact_visible_to_audience() before handing
them to an LLM.

Accepted audience_scope formats in a facts/YYYY-MM.md block:
  - JSON array:    - **audience_scope:** ["family", "personal"]
  - bare string:   - **audience_scope:** professional
  - CSV:           - **audience_scope:** family, personal
  - missing:       field absent — parser returns None, which
                   fact_visible_to_audience() treats as visible-to-all.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


_FIELD_RE = re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")


def parse_facts_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    facts: list[dict] = []
    for raw_block in re.split(r"\n---\n", text):
        block = raw_block.strip()
        if not block:
            continue
        fields: dict = {}
        for line in block.splitlines():
            m = _FIELD_RE.match(line)
            if m:
                fields[m.group(1).strip()] = m.group(2).strip()
        if not fields.get("id"):
            continue

        try:
            confidence = float(fields.get("confidence", "0") or 0)
        except ValueError:
            confidence = 0.0

        facts.append({
            "id": fields["id"],
            "content": fields.get("content", ""),
            "subject": fields.get("subject", ""),
            "confidence": confidence,
            "category": fields.get("category", ""),
            "recorded_at": fields.get("recorded_at", ""),
            "source_agent": fields.get("source_agent", ""),
            "audience_scope": _parse_audience_scope(fields.get("audience_scope")),
            "raw": raw_block,
        })
    return facts


def _parse_audience_scope(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed]
        except json.JSONDecodeError:
            pass
    if "," in raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [raw]


def load_facts_for_subject(subject_slug: str, facts_dir: Path) -> list[dict]:
    """Return every fact in facts_dir whose subject matches the slug (case-insensitive)."""
    if not facts_dir.exists():
        return []
    target = subject_slug.lower()
    out: list[dict] = []
    for path in sorted(facts_dir.glob("*.md")):
        for fact in parse_facts_file(path):
            if fact["subject"].lower() == target:
                out.append(fact)
    return out


_FACT_TEMPLATE = (
    "\n---\n\n"
    "- **id:** {fact_id}\n"
    "- **content:** {content}\n"
    "- **subject:** {subject}\n"
    "- **source_type:** {source_type}\n"
    "- **source_detail:** {source_detail}\n"
    "- **source_agent:** {source_agent}\n"
    "- **confidence:** {confidence}\n"
    "- **category:** {category}\n"
    "- **recorded_at:** {recorded_at}\n"
)


def upsert_fact(
    *,
    facts_dir: Path,
    subject: str,
    category: str,
    content: str,
    source_agent: str,
    source_type: str = "derived",
    source_detail: str = "",
    confidence: float = 0.9,
    idempotency_key: str | None = None,
    recorded_at: str,
    audience_scope: list[str] | None = None,
) -> dict:
    """Append a fact to ``facts/YYYY-MM.md`` (derived from ``recorded_at``),
    idempotent on ``(source_agent, subject, idempotency_key)``.

    If ``idempotency_key`` is provided, scans every ``facts/*.md`` for a
    fact whose id already matches ``{source_agent}-{subject}-{idempotency_key}``
    and skips the write if found. Callers that don't want dedup can pass
    ``idempotency_key=None`` to always append — but in that case they must
    supply their own collision-safe id scheme.

    Returns ``{"id", "status", "path"}`` where ``status`` is ``"created"`` or
    ``"skipped"``.
    """
    if idempotency_key is not None:
        fact_id = f"{source_agent}-{subject}-{idempotency_key}"
        if facts_dir.exists():
            for existing_path in facts_dir.glob("*.md"):
                for fact in parse_facts_file(existing_path):
                    if fact["id"] == fact_id:
                        return {
                            "id": fact_id,
                            "status": "skipped",
                            "path": str(existing_path),
                        }
    else:
        fact_id = f"{source_agent}-{subject}-{recorded_at}"

    month = recorded_at[:7]  # YYYY-MM from ISO timestamp
    month_path = facts_dir / f"{month}.md"
    facts_dir.mkdir(parents=True, exist_ok=True)
    is_new = not month_path.exists()

    entry = _FACT_TEMPLATE.format(
        fact_id=fact_id,
        content=content,
        subject=subject,
        source_type=source_type,
        source_detail=source_detail,
        source_agent=source_agent,
        confidence=confidence,
        category=category,
        recorded_at=recorded_at,
    )
    if audience_scope:
        entry += f"- **audience_scope:** {json.dumps(audience_scope)}\n"

    with open(month_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# Facts — {month}\n")
        f.write(entry)

    return {"id": fact_id, "status": "created", "path": str(month_path)}
