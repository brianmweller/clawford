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

        recorded_at = fields.get("recorded_at", "")
        facts.append({
            "id": fields["id"],
            "content": fields.get("content", ""),
            "subject": fields.get("subject", ""),
            "confidence": confidence,
            "category": fields.get("category", ""),
            "recorded_at": recorded_at,
            # Missing last_reinforced_at defaults to recorded_at so
            # readers that sort by "how recently was this seen" get
            # a sensible value on pre-reinforcement facts. The
            # reinforcement path (upsert_fact) stamps the field
            # explicitly when it bumps.
            "last_reinforced_at": fields.get("last_reinforced_at", recorded_at),
            "source_agent": fields.get("source_agent", ""),
            "audience_scope": _parse_audience_scope(fields.get("audience_scope")),
            "mention_slugs": _parse_slug_list(fields.get("mention_slugs")),
            "fact_type": fields.get("fact_type", ""),
            "value": _parse_value_json(fields.get("value")),
            "raw": raw_block,
        })
    return facts


def _parse_value_json(raw: str | None) -> dict | None:
    """Parse the optional `value` field as a JSON object.
    Returns None for missing, empty, or malformed content — the field
    is best-effort metadata; a parse failure must not take down the reader."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw or raw.lower() == "null":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_slug_list(raw: str | None) -> list[str]:
    """Parse a JSON-list field (mention_slugs) into list[str].
    Returns [] when field is missing or malformed."""
    if not raw:
        return []
    raw = raw.strip()
    if not raw or raw == "[]":
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return []


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


DEFAULT_COMPOSER_MIN_CONFIDENCE = 0.6


def load_facts_for_subject(
    subject_slug: str,
    facts_dir: Path,
    *,
    min_confidence: float = DEFAULT_COMPOSER_MIN_CONFIDENCE,
) -> list[dict]:
    """Return every fact in facts_dir whose subject matches the slug
    (case-insensitive) AND whose confidence is >= min_confidence.

    The default min_confidence mirrors ``fact_extraction.REVIEW_CONFIDENCE``:
    facts below 0.6 are flagged in ``_pending_review.md`` for operator
    triage and must NOT leak into draft composition until promoted.
    Centralizing the default here closes the gap that every caller used
    to have to filter post-load.

    Callers who need the full set (audit tools, triage UIs, the pending-
    review loop itself) can pass ``min_confidence=0.0`` to disable the
    filter.

    Fast path: when ``brain/facts/_index.json`` is present AND fresh (no
    monthly file has been modified since the index was built), only the
    months that actually contain facts for the subject are parsed. Falls
    back to a full scan when the index is absent, stale, or corrupt.
    """
    if not facts_dir.exists():
        return []
    target = subject_slug.lower()

    # Fast path — via index.
    try:
        from agents.shared import brain_index as _bi
    except ImportError:
        _bi = None  # type: ignore
    if _bi is not None:
        index = _bi.load_index(facts_dir)
        if _bi.is_fresh(facts_dir, index):
            return _load_via_index(
                target=target,
                facts_dir=facts_dir,
                index=index,
                min_confidence=min_confidence,
            )

    # Slow path — full scan. Skip operator-internal files
    # (_pending_review.md, _index.json-like scratch) so low-confidence
    # entries quarantined in _pending_review.md don't leak back into
    # the composer via this loader.
    #
    # Match both subject and mention_slugs — the latter surfaces facts
    # that name the target without being about them (e.g. a fact on
    # Jamie's slug that mentions Eliott surfaces for Eliott too).
    out: list[dict] = []
    seen: set[str] = set()
    for path in sorted(facts_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        for fact in parse_facts_file(path):
            if fact["confidence"] < min_confidence:
                continue
            if fact["id"] in seen:
                continue
            matches_subject = fact["subject"].lower() == target
            matches_mention = target in {
                m.lower() for m in fact.get("mention_slugs") or []
            }
            if matches_subject or matches_mention:
                out.append(fact)
                seen.add(fact["id"])
    return out


def _load_via_index(
    *,
    target: str,
    facts_dir: Path,
    index: dict,
    min_confidence: float,
) -> list[dict]:
    """Fast-path helper: parse only the monthly files listed in the
    index for this subject; apply the confidence filter; return."""
    entries = (index.get("by_subject") or {}).get(target, [])
    if not entries:
        return []
    months_needed: set[str] = {month for month, _fid in entries}
    want_ids: set[str] = {fid for _month, fid in entries}
    out: list[dict] = []
    for month in sorted(months_needed):
        path = facts_dir / f"{month}.md"
        if not path.exists():
            continue
        for fact in parse_facts_file(path):
            if fact["id"] not in want_ids:
                continue
            if fact["confidence"] < min_confidence:
                continue
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


CONFIDENCE_REINFORCEMENT_BUMP = 0.05
CONFIDENCE_REINFORCEMENT_CAP = 0.95


def _reinforce_fact_in_file(
    *,
    path: Path,
    fact_id: str,
    current_confidence: float,
    reinforced_at: str,
) -> float:
    """Rewrite the fact block with matching id in-place: bump confidence
    (capped) and set last_reinforced_at to reinforced_at. Returns the
    new confidence. Atomic via tmp+replace."""
    new_conf = round(
        min(current_confidence + CONFIDENCE_REINFORCEMENT_BUMP,
            CONFIDENCE_REINFORCEMENT_CAP),
        4,
    )
    # Render enough precision to survive parse/format roundtrip; trim
    # a trailing .0 so "0.75" reads cleanly (avoid "0.7500").
    new_conf_str = f"{new_conf:.4f}".rstrip("0").rstrip(".")
    if not new_conf_str:
        new_conf_str = "0"

    text = path.read_text(encoding="utf-8")
    blocks = text.split("\n---\n")
    rewritten: list[str] = []
    touched = False
    for block in blocks:
        if f"- **id:** {fact_id}" in block and not touched:
            rewritten.append(_rewrite_block_reinforcement(
                block, new_conf_str, reinforced_at,
            ))
            touched = True
        else:
            rewritten.append(block)
    new_text = "\n---\n".join(rewritten)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    return new_conf


_CONFIDENCE_LINE_RE = re.compile(
    r"^(\s*-\s*\*\*confidence:\*\*\s*).*$", re.MULTILINE,
)
_LAST_REINFORCED_LINE_RE = re.compile(
    r"^\s*-\s*\*\*last_reinforced_at:\*\*\s*.*$", re.MULTILINE,
)


def _rewrite_block_reinforcement(
    block: str, new_confidence_str: str, reinforced_at: str,
) -> str:
    # Update confidence line.
    block = _CONFIDENCE_LINE_RE.sub(
        rf"\g<1>{new_confidence_str}", block, count=1,
    )
    # Replace an existing last_reinforced_at line, or insert one after
    # the recorded_at line.
    if _LAST_REINFORCED_LINE_RE.search(block):
        block = _LAST_REINFORCED_LINE_RE.sub(
            f"- **last_reinforced_at:** {reinforced_at}", block, count=1,
        )
    else:
        # Insert after recorded_at. Fall back to end-of-block if that
        # line is somehow missing.
        inserted = False
        lines = block.splitlines()
        out: list[str] = []
        for ln in lines:
            out.append(ln)
            if not inserted and ln.lstrip().startswith("- **recorded_at:"):
                out.append(f"- **last_reinforced_at:** {reinforced_at}")
                inserted = True
        if not inserted:
            out.append(f"- **last_reinforced_at:** {reinforced_at}")
        block = "\n".join(out)
    return block


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
    mention_slugs: list[str] | None = None,
    fact_type: str = "",
    value: dict | None = None,
) -> dict:
    """Append a fact to ``facts/YYYY-MM.md`` (derived from ``recorded_at``),
    idempotent on ``(source_agent, subject, idempotency_key)``.

    On idempotency collision — a fact with the same id already on disk —
    the existing fact is REINFORCED: its confidence bumps by
    ``CONFIDENCE_REINFORCEMENT_BUMP`` (capped at ``CONFIDENCE_REINFORCEMENT_CAP``)
    and ``last_reinforced_at`` is set to the incoming ``recorded_at``. This
    Flux-style pattern preserves the "seen multiple times" signal that was
    thrown away pre-2026-04-20 when collisions just returned ``skipped``.

    If ``idempotency_key`` is None, the caller must supply a collision-safe
    id scheme — the function falls back to ``{source_agent}-{subject}-{recorded_at}``.

    Returns ``{"id", "status", "path"}`` where ``status`` is one of
    ``"created"`` (new fact), ``"reinforced"`` (confidence bumped), or
    ``"skipped"`` (rare — only when idempotency_key is None and the timestamp
    collides). When ``status == "reinforced"`` the dict also carries
    ``new_confidence``.
    """
    if idempotency_key is not None:
        fact_id = f"{source_agent}-{subject}-{idempotency_key}"
        if facts_dir.exists():
            for existing_path in facts_dir.glob("*.md"):
                for fact in parse_facts_file(existing_path):
                    if fact["id"] == fact_id:
                        new_conf = _reinforce_fact_in_file(
                            path=existing_path,
                            fact_id=fact_id,
                            current_confidence=fact["confidence"],
                            reinforced_at=recorded_at,
                        )
                        return {
                            "id": fact_id,
                            "status": "reinforced",
                            "path": str(existing_path),
                            "new_confidence": new_conf,
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
    if mention_slugs:
        entry += f"- **mention_slugs:** {json.dumps(mention_slugs)}\n"
    if fact_type:
        entry += f"- **fact_type:** {fact_type}\n"
    if value is not None:
        entry += f"- **value:** {json.dumps(value, ensure_ascii=False)}\n"

    with open(month_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# Facts — {month}\n")
        f.write(entry)

    return {"id": fact_id, "status": "created", "path": str(month_path)}
