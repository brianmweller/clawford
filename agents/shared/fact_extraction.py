"""Shared fact-extraction helper for Huckle's miners.

Miners (`gmail-facts-mine.py`, `krisp-facts-mine.py`,
`workflowy-facts-mine.py`) pass raw source text through this module to
get back `upsert_fact()`-ready dicts tagged with `audience_scope` and
flagged for pending review when appropriate.

Contract pinned by `agents/shared/tests/test_fact_extraction.py`:
- Low-confidence (< MIN_CONFIDENCE) drop silently.
- Medium-confidence (MIN_CONFIDENCE <= conf < REVIEW_CONFIDENCE) return
  with `needs_review=True` so the caller also appends to
  `facts/_pending_review.md` after `upsert_fact()` reports `created`.
- Subjects outside the provided `candidate_slugs` drop (anti-hallucination).
- Self-subjects (the operator) drop — the brain tracks others only.
- `audience_scope` filtered against `VALID_SCOPE_TAGS`; empty scope drops.
- LLM failure / malformed JSON → empty list, never raises.

The prompt is source-agnostic with `source_context` metadata injected so
the same LLM pipeline serves Gmail bodies, Krisp transcripts, and
Workflowy notes without prompt forking.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Constants — single source of truth for miners AND the scope-augment
# retroactive tagger. facts_scope_augment_lib re-imports VALID_SCOPE_TAGS
# from here so both pipelines stay in lockstep.
# ---------------------------------------------------------------------------

VALID_SCOPE_TAGS: set[str] = {
    "professional", "personal", "family", "friends",
    "academic", "financial", "legal", "genealogy", "internal", "public",
}

MIN_CONFIDENCE: float = 0.3
REVIEW_CONFIDENCE: float = 0.6

from agents.shared.operator import load_operator as _load_operator


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_PROMPT_TEMPLATE = """Extract durable facts about PEOPLE from the source material below.

A durable fact is information that would remain useful for weeks or months
of future correspondence — identity, role, relationship, preference, life
event. Skip transient noise (scheduling, pleasantries, one-off requests).
Most messages have ZERO durable facts — return an empty list rather than
fabricating.

Source: {source}
{source_hints}
{statement_date_line}
Candidate subjects (you may ONLY pick from these slugs — never invent a
new slug):
{candidate_block}

For each fact, assign an audience_scope — the contexts in which the fact
is appropriate to surface in future draft correspondence. Valid tags:
{valid_tags}

Rules of thumb for audience_scope:
- Health, family composition, home, relationships → ["personal", "family"]
- Work role, employer, job search, project scope → ["professional"]
- School info about kids → ["personal", "family"]
- Financial positions, estate planning → ["personal", "financial"]
- Peer-to-peer work context → ["professional"]
- Identity facts that are public (title, employer) → ["professional"]
- Identity facts that are intimate (health, beliefs, marital status) → ["personal"]
- If truly generic (harmless everywhere) → ["public"]

AGE NORMALIZATION (IMPORTANT — ages rot, birthdates don't):
When the source material states a person's age (e.g. "Eliott is almost 5,"
"the twins just turned 3," "baby is 10 months"), DO NOT store the raw age
string. Instead, use the STATEMENT DATE above to compute an approximate
birth month/year and store THAT as the fact. Reader LLMs will compute
current age from today's date.

Format the content as:
  "<Name> is <relationship>, born approx <YYYY-MM> (age ~<N> as of <STATEMENT_DATE>)"

Example — if the statement date is 2026-04-21 and the source says
"Eliott is approaching 5":
  content: "Eliott is Jamie's son, born approx 2021-06 (age ~5 as of 2026-04-21)"
  category: "identity"
  confidence: 0.6  (approximate — widen the month to +/- 2 months mentally)

Do the same for age ranges ("2.5" → month offset back 2.5 years from the
statement date). For "baby"/"newborn", use +/- 6 months. Never store just
"age 5" — that's worthless in 12 months.

Confidence scale:
- 0.8+ = fact stated explicitly (e.g. "I'm starting a new job at Acme")
- 0.5-0.7 = strongly implied but not stated (e.g. signature block mentions a new company)
- 0.3-0.4 = plausible inference from context (treat as provisional; human review welcomed)
- below 0.3 = guess; DO NOT emit these

Output a JSON object with a single "facts" array. Each fact has:
- subject_slug: one of the candidate slugs exactly
- category: short type word (identity, role, preference, event, relationship, ...)
- content: a single sentence stating the fact
- confidence: float 0.3-1.0
- audience_scope: list of 1-3 valid tags
- reason: short phrase naming the evidence
- mention_slugs: (optional) list of slugs from "MENTION CANDIDATES" below
  that the fact names but isn't principally about. Omit or empty when none.
{mention_candidates_block}
SOURCE MATERIAL
{text}
"""


def _statement_date_iso(source_context: dict) -> str:
    """Derive the ISO date the source was authored. Accepts:
      - internal_date: Gmail epoch-ms string
      - statement_date: pre-computed "YYYY-MM-DD" string (Krisp/Workflowy)
    Returns "" when neither is present or parseable."""
    raw = source_context.get("statement_date")
    if isinstance(raw, str) and len(raw) == 10:
        return raw
    internal = source_context.get("internal_date")
    if internal:
        try:
            ms = int(internal)
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return ""
    return ""


def _render_mention_candidates_block(
    mention_candidate_slugs: dict[str, dict] | None,
) -> str:
    """Render the MENTION CANDIDATES section, or empty string when none.

    Mention candidates are people who may be named in the body but cannot
    themselves be subjects (typically minor children linked via parent_slug
    who have no email address). The LLM may cite them in `mention_slugs`
    but must not use them as `subject_slug`.
    """
    if not mention_candidate_slugs:
        return ""
    lines = []
    for slug in sorted(mention_candidate_slugs.keys()):
        info = mention_candidate_slugs[slug] or {}
        full = info.get("full_name", "").strip()
        first = info.get("first_name", "").strip()
        if full and first:
            lines.append(f"- {slug} ({full} — first name: {first})")
        elif full:
            lines.append(f"- {slug} ({full})")
        else:
            lines.append(f"- {slug}")
    body = "\n".join(lines)
    return (
        "\nMENTION CANDIDATES — these people may be mentioned in the body.\n"
        "If a fact names one, add their slug to `mention_slugs`.\n"
        "You MAY NOT use these as `subject_slug` — only the primary\n"
        "Candidate subjects list above is valid for subject_slug.\n"
        f"{body}\n"
    )


def build_extraction_prompt(
    *,
    text: str,
    source_context: dict,
    candidate_slugs: set[str],
    mention_candidate_slugs: dict[str, dict] | None = None,
) -> str:
    """Assemble the LLM prompt for a single extraction call.

    Source-agnostic framing. source_context hints (from_email, attendees,
    etc.) help the LLM anchor references like "she said" back to a slug.

    `mention_candidate_slugs` maps slug → {full_name, first_name} for
    people who may be named in the body but cannot be subjects (typically
    children of primary candidates, linked via parent_slug on their person
    record). When provided, they surface in a MENTION CANDIDATES block and
    the LLM may cite them in each fact's `mention_slugs` field.
    """
    source = source_context.get("source", "unknown")
    hints = []
    for k in ("message_id", "event_id", "node_id", "from_email", "direction", "attendees"):
        v = source_context.get(k)
        if v:
            hints.append(f"- {k}: {v}")
    hints_block = "\n".join(hints) if hints else "(no metadata)"

    statement_date = _statement_date_iso(source_context)
    statement_date_line = (
        f"\nSTATEMENT DATE (use for AGE NORMALIZATION below): {statement_date}\n"
        if statement_date else ""
    )

    cand_lines = "\n".join(f"- {s}" for s in sorted(candidate_slugs))
    mention_block = _render_mention_candidates_block(mention_candidate_slugs)

    return _PROMPT_TEMPLATE.format(
        source=source,
        source_hints=hints_block,
        statement_date_line=statement_date_line,
        candidate_block=cand_lines or "(none)",
        mention_candidates_block=mention_block,
        valid_tags=", ".join(sorted(VALID_SCOPE_TAGS)),
        text=text,
    )


# ---------------------------------------------------------------------------
# LLM call + response parsing
# ---------------------------------------------------------------------------


def _default_infer(prompt: str, **kwargs) -> Any:
    """Lazy import of shared.llm.infer so tests can monkeypatch without
    loading the HTTP stack."""
    from agents.shared.llm import infer
    return infer(prompt=prompt, **kwargs)


def _strip_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return cleaned


def _parse_llm_response(raw_text: str) -> list[dict]:
    """Best-effort JSON parse. Returns the raw facts list or []."""
    cleaned = _strip_fence(raw_text)
    if not cleaned:
        return []
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []
    facts = parsed.get("facts")
    if not isinstance(facts, list):
        return []
    return [f for f in facts if isinstance(f, dict)]


# ---------------------------------------------------------------------------
# Filters and normalization
# ---------------------------------------------------------------------------


def _source_id_from_context(source_context: dict) -> str:
    """Pull the stable identifier for this source unit — the Gmail
    message id, Krisp event id, or Workflowy node id. Used in
    source_detail and as the idempotency seed."""
    for key in ("message_id", "event_id", "node_id"):
        v = source_context.get(key)
        if v:
            return str(v)
    return ""


def _build_idempotency_key(source_context: dict, subject_slug: str, content: str) -> str:
    """Deterministic key: source-type + source-id + subject + short
    content hash. The content hash guards against two distinct facts
    about the same subject from the same message — one message could
    legitimately reveal multiple facts about one person."""
    source = source_context.get("source", "unknown")
    src_id = _source_id_from_context(source_context) or "nosrc"
    content_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:8]
    return f"{source}-{src_id}-{subject_slug}-{content_hash}"


def _normalize_fact(
    raw: dict,
    *,
    source_context: dict,
    candidate_slugs: set[str],
    mention_candidate_slugs: set[str] | None = None,
) -> dict | None:
    """Apply all filters; return the upsert-ready dict (with
    needs_review flag) or None if the fact should be dropped.

    `subject_slug` must be in `candidate_slugs` (primary). Mention-candidate
    slugs MAY NOT be used as subjects — a fact that tries to is dropped.
    Valid `mention_slugs` values are those in `candidate_slugs ∪
    mention_candidate_slugs`; invalid entries are silently filtered out
    without dropping the fact.
    """
    mention_set = mention_candidate_slugs or set()

    subject_slug = str(raw.get("subject_slug") or "").strip()
    if not subject_slug or subject_slug not in candidate_slugs:
        return None
    if subject_slug in _load_operator().slugs:
        return None

    content = str(raw.get("content") or "").strip()
    if not content:
        return None

    try:
        confidence = float(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    if confidence < MIN_CONFIDENCE:
        return None

    scope_raw = raw.get("audience_scope")
    if not isinstance(scope_raw, list):
        return None
    scope = [t for t in scope_raw if isinstance(t, str) and t in VALID_SCOPE_TAGS]
    if not scope:
        return None

    category = str(raw.get("category") or "fact").strip() or "fact"

    # mention_slugs: filter against the closed set (primary ∪ mention). Drop
    # hallucinations but keep the fact. Preserve order, dedupe.
    allowed_mentions = candidate_slugs | mention_set
    seen: set[str] = set()
    mention_slugs: list[str] = []
    raw_mentions = raw.get("mention_slugs")
    if isinstance(raw_mentions, list):
        for m in raw_mentions:
            if not isinstance(m, str):
                continue
            m = m.strip()
            if m and m in allowed_mentions and m != subject_slug and m not in seen:
                mention_slugs.append(m)
                seen.add(m)

    source = source_context.get("source", "unknown")
    src_id = _source_id_from_context(source_context)
    source_detail = f"{source}:{src_id}" if src_id else source

    needs_review = confidence < REVIEW_CONFIDENCE

    return {
        "subject": subject_slug,
        "category": category,
        "content": content,
        "confidence": confidence,
        "audience_scope": scope,
        "mention_slugs": mention_slugs,
        "source_detail": source_detail,
        "idempotency_key": _build_idempotency_key(source_context, subject_slug, content),
        "needs_review": needs_review,
        "reason": str(raw.get("reason") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_DEDUPE_JACCARD_THRESHOLD = 0.6

_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to",
    "is", "are", "was", "were", "be", "been", "has", "have", "had",
    "for", "from", "with", "as", "that", "this", "these", "those",
    "s",  # possessive leftovers after tokenize strip
})


def _tokenize_for_dedupe(text: str) -> frozenset[str]:
    """Return a stopword-stripped lowercased token set. Numerics retained
    (birth years, ages, phone fragments often carry the signal)."""
    raw = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return frozenset(t for t in raw if t and t not in _STOPWORDS)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _dedupe_within_batch(facts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Collapse near-duplicate facts within a single extraction batch.

    Grouped by subject_slug. Within each group, facts are processed
    ordered by (-confidence, idempotency_key) so the higher-confidence
    fact is seen first; on tie the lexicographically-smaller
    idempotency_key wins (determinism). A candidate fact is dropped
    when its Jaccard similarity against any already-kept fact in the
    same group meets the threshold.

    Returns (kept, dropped). Dropped facts carry an extra key
    'dropped_reason' pointing to the keeper id.
    """
    if not facts:
        return [], []

    # Preserve original order for the kept-list return.
    by_subject: dict[str, list[dict]] = {}
    for f in facts:
        by_subject.setdefault(f.get("subject", ""), []).append(f)

    keep_ids: set[str] = set()
    dropped: list[dict] = []

    for group in by_subject.values():
        if len(group) < 2:
            keep_ids.update(f["idempotency_key"] for f in group)
            continue
        ordered = sorted(
            group,
            key=lambda f: (
                -float(f.get("confidence") or 0),
                str(f.get("idempotency_key") or ""),
            ),
        )
        kept_tokens: list[tuple[dict, frozenset[str]]] = []
        for cand in ordered:
            cand_tokens = _tokenize_for_dedupe(cand.get("content") or "")
            match: dict | None = None
            for kept_fact, kept_toks in kept_tokens:
                if _jaccard(cand_tokens, kept_toks) >= _DEDUPE_JACCARD_THRESHOLD:
                    match = kept_fact
                    break
            if match is None:
                kept_tokens.append((cand, cand_tokens))
                keep_ids.add(cand["idempotency_key"])
            else:
                cand_copy = dict(cand)
                cand_copy["dropped_reason"] = (
                    f"near-duplicate of {match.get('idempotency_key')}"
                )
                dropped.append(cand_copy)

    kept = [f for f in facts if f["idempotency_key"] in keep_ids]
    return kept, dropped


def extract_facts_from_text(
    *,
    text: str,
    source_context: dict,
    candidate_slugs: set[str],
    mention_candidate_slugs: dict[str, dict] | None = None,
    timeout: int = 120,
    infer_fn: Callable | None = None,
) -> list[dict]:
    """Extract durable facts from text. See module docstring for contract.

    `mention_candidate_slugs` maps slug → {full_name, first_name} for
    people who may be named in the body but cannot be subjects. They
    surface to the LLM via build_extraction_prompt's MENTION CANDIDATES
    block; valid cites land in each fact's `mention_slugs` list.
    """
    if not candidate_slugs:
        return []
    if not text or not text.strip():
        return []

    prompt = build_extraction_prompt(
        text=text,
        source_context=source_context,
        candidate_slugs=candidate_slugs,
        mention_candidate_slugs=mention_candidate_slugs,
    )
    caller = infer_fn or _default_infer
    result = caller(prompt, json_mode=True, timeout=timeout)
    if not getattr(result, "ok", False):
        return []

    raw_facts = _parse_llm_response(getattr(result, "text", "") or "")
    mention_set = set((mention_candidate_slugs or {}).keys())
    normalized: list[dict] = []
    for raw in raw_facts:
        norm = _normalize_fact(
            raw,
            source_context=source_context,
            candidate_slugs=candidate_slugs,
            mention_candidate_slugs=mention_set,
        )
        if norm is not None:
            normalized.append(norm)

    kept, _dropped = _dedupe_within_batch(normalized)
    return kept


# ---------------------------------------------------------------------------
# Pending-review log (Flux-style)
# ---------------------------------------------------------------------------


_REVIEW_HEADER = "# Pending review — low-confidence mined facts\n\n"
_REVIEW_ID_LINE = re.compile(r"^\s*-\s*\*\*id:\*\*\s*(.+?)\s*$", re.MULTILINE)


def append_pending_review(facts_dir: Path, fact: dict) -> None:
    """Append a low-confidence fact marker to
    ``<facts_dir>/_pending_review.md``.

    Idempotent on fact["id"] — re-running the miner on the same window
    doesn't duplicate entries. Atomic tmp+replace when rewriting the
    file; simple append when the file doesn't exist yet.
    """
    facts_dir.mkdir(parents=True, exist_ok=True)
    path = facts_dir / "_pending_review.md"
    fact_id = str(fact.get("id") or "")

    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if fact_id and fact_id in {m.strip() for m in _REVIEW_ID_LINE.findall(existing)}:
            return
    else:
        existing = _REVIEW_HEADER

    entry = _format_review_entry(fact)
    new_text = existing.rstrip() + "\n\n" + entry + "\n"

    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)


def _format_review_entry(fact: dict) -> str:
    scope = fact.get("audience_scope") or []
    try:
        conf = f"{float(fact.get('confidence', 0)):.2f}"
    except (TypeError, ValueError):
        conf = str(fact.get("confidence", ""))
    return (
        "---\n"
        f"- **id:** {fact.get('id', '')}\n"
        f"- **subject:** {fact.get('subject', '')}\n"
        f"- **category:** {fact.get('category', '')}\n"
        f"- **confidence:** {conf}\n"
        f"- **audience_scope:** {json.dumps(scope)}\n"
        f"- **source:** {fact.get('source_detail', '')}\n"
        f"- **reason:** {fact.get('reason', '')}\n"
        f"- **content:** {fact.get('content', '')}"
    )
