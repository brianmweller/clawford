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

STRUCTURED FACT TYPES (ages rot, structure doesn't):
Every fact gets narrative `content` as a complete sentence. When the
claim also fits one of the four types below, ALSO emit `fact_type` +
`value` so structured readers don't have to re-parse prose. Emit
fact_type ONLY when confident the value's required keys are satisfied.

1. birthdate — use for ANY age or birth claim. Ages rot; birthdates
   don't.
   value: {{"year": <int>, "month": <int 1-12|null>, "day": <int 1-31|null>,
           "precision": "year"|"month"|"day"}}
   Example — if the STATEMENT DATE above is 2026-04-21 and the source
   says "Eliott is approaching 5":
     content: "Eliott is Jamie's son, born approx 2021-06 (age ~5 as of 2026-04-21)"
     fact_type: "birthdate"
     value: {{"year": 2021, "month": 6, "precision": "month"}}
   For ranges like "2.5" offset back 2.5 years from the statement date.
   "baby"/"newborn" → +/- 6 months. Never store a raw "age 5" narrative
   without the structured value — that's worthless in 12 months.

2. employer — use when the claim names BOTH a title AND an org. Role
   nested inside. If title-only (no org known), use fact_type: "role"
   instead.
   value: {{"company": <str required>, "role": <str>, "level":
           "IC"|"Manager"|"Director"|"VP"|"CxO"|"Founder"|null,
           "functional_area": <str>, "start_date": <ISO>,
           "end_date": <ISO or null = current>,
           "status": "current"|"former"}}
   Example:
     content: "Jane is Director of Data at Example Corp."
     fact_type: "employer"
     value: {{"company": "Example Corp", "role": "Director of Data",
             "level": "Director", "functional_area": "Data",
             "status": "current"}}

3. role — title-only (common in career-exploration emails). Set status
   to "exploring" when the person is actively looking for this role;
   "current" or "former" otherwise.
   value: {{"title": <str required>, "level": <str>,
           "functional_area": <str>,
           "status": "current"|"exploring"|"former"}}

4. preference — a liked/disliked/preferred thing.
   value: {{"domain": <str required — "food"|"travel"|"communication"|...>,
           "item": <str required>,
           "polarity": "likes"|"dislikes"|"prefers"|"avoids",
           "strength": "strong"|"moderate"|"mild"|null,
           "context": <str>}}

OVERLAP RULE: if a claim includes BOTH a title AND an org, emit
fact_type: "employer" (with role nested). Title-only → fact_type:
"role". Never both for the same employment claim.

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
- fact_type: (optional) "birthdate" | "employer" | "role" | "preference"
  — only when the value's required keys are satisfied
- value: (optional) structured value object matching fact_type's schema
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


# ---------------------------------------------------------------------------
# Structured-value validators (Phase 2)
# ---------------------------------------------------------------------------

_EMPLOYER_LEVELS = {"IC", "Manager", "Director", "VP", "CxO", "Founder"}
_EMPLOYER_STATUS = {"current", "former"}
_ROLE_STATUS = {"current", "exploring", "former"}
_PREFERENCE_POLARITY = {"likes", "dislikes", "prefers", "avoids"}
_PREFERENCE_STRENGTH = {"strong", "moderate", "mild"}


def _validate_birthdate(value: dict) -> dict | None:
    """Return a normalized birthdate value, or None when invalid."""
    if not isinstance(value, dict):
        return None
    year = value.get("year")
    if not isinstance(year, int) or not (1900 <= year <= 2100):
        return None
    precision = value.get("precision")
    if precision not in ("year", "month", "day"):
        return None
    out: dict = {"year": year, "precision": precision}
    month = value.get("month")
    if month is not None:
        if not isinstance(month, int) or not (1 <= month <= 12):
            return None
        out["month"] = month
    day = value.get("day")
    if day is not None:
        if not isinstance(day, int) or not (1 <= day <= 31):
            return None
        out["day"] = day
    return out


def _validate_employer(value: dict) -> dict | None:
    if not isinstance(value, dict):
        return None
    company = value.get("company")
    if not isinstance(company, str) or not company.strip():
        return None
    status = value.get("status")
    if status not in _EMPLOYER_STATUS:
        return None
    out: dict = {"company": company.strip(), "status": status}
    for key in ("role", "functional_area", "start_date", "end_date"):
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    level = value.get("level")
    if level is not None:
        if level not in _EMPLOYER_LEVELS:
            return None
        out["level"] = level
    return out


def _validate_role(value: dict) -> dict | None:
    if not isinstance(value, dict):
        return None
    title = value.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    status = value.get("status")
    if status not in _ROLE_STATUS:
        return None
    out: dict = {"title": title.strip(), "status": status}
    for key in ("functional_area",):
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    level = value.get("level")
    if level is not None:
        if level not in _EMPLOYER_LEVELS:
            return None
        out["level"] = level
    return out


def _validate_preference(value: dict) -> dict | None:
    if not isinstance(value, dict):
        return None
    domain = value.get("domain")
    item = value.get("item")
    polarity = value.get("polarity")
    if not isinstance(domain, str) or not domain.strip():
        return None
    if not isinstance(item, str) or not item.strip():
        return None
    if polarity not in _PREFERENCE_POLARITY:
        return None
    out: dict = {
        "domain": domain.strip(),
        "item": item.strip(),
        "polarity": polarity,
    }
    strength = value.get("strength")
    if strength is not None:
        if strength not in _PREFERENCE_STRENGTH:
            return None
        out["strength"] = strength
    context = value.get("context")
    if isinstance(context, str) and context.strip():
        out["context"] = context.strip()
    return out


_VALIDATORS: dict = {
    "birthdate": _validate_birthdate,
    "employer": _validate_employer,
    "role": _validate_role,
    "preference": _validate_preference,
}


def _normalize_structured_fields(fact_type: str, value) -> tuple[str, dict | None]:
    """Apply per-type validation to (fact_type, value). Returns the
    pair that should land on the normalized fact — either the
    validated shape, or ("", None) for graceful degrade when the
    input is unrecognized / malformed."""
    if not isinstance(fact_type, str) or not fact_type.strip():
        return "", None
    ft = fact_type.strip()
    validator = _VALIDATORS.get(ft)
    if validator is None:
        return "", None
    validated = validator(value) if isinstance(value, dict) else None
    if validated is None:
        return "", None
    return ft, validated


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

    fact_type, value = _normalize_structured_fields(
        raw.get("fact_type", ""), raw.get("value"),
    )

    return {
        "subject": subject_slug,
        "category": category,
        "content": content,
        "confidence": confidence,
        "audience_scope": scope,
        "mention_slugs": mention_slugs,
        "fact_type": fact_type,
        "value": value,
        "source_detail": source_detail,
        "idempotency_key": _build_idempotency_key(source_context, subject_slug, content),
        "needs_review": needs_review,
        "reason": str(raw.get("reason") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Cross-run semantic dedupe (Phase 3)
# ---------------------------------------------------------------------------

DEDUPE_REINFORCE_THRESHOLD = 0.88
DEDUPE_REVIEW_THRESHOLD = 0.75


def _structured_match(a_type: str, a_val, b_type: str, b_val) -> bool | None:
    """Type-aware structured compare. Returns:
       True  — confident structured match → treat as reinforcement
       False — different fact_type OR same type but value conflict →
               definitely NEW, skip embedding
       None  — same type but value is inconclusive → fall through to
               embedding for final call

    Only fires when BOTH sides have fact_type set. When either side is
    untyped, returns None so the embedding path handles the comparison.
    """
    if not a_type or not b_type:
        return None
    if a_type != b_type:
        return False
    if not isinstance(a_val, dict) or not isinstance(b_val, dict):
        return None
    if a_type == "birthdate":
        return a_val.get("year") == b_val.get("year")
    if a_type == "employer":
        a_co = (a_val.get("company") or "").strip().lower()
        b_co = (b_val.get("company") or "").strip().lower()
        if not a_co or not b_co:
            return None
        return a_co == b_co
    if a_type == "role":
        a_t = (a_val.get("title") or "").strip().lower()
        b_t = (b_val.get("title") or "").strip().lower()
        if not a_t or not b_t:
            return None
        return a_t == b_t
    if a_type == "preference":
        pair_a = (
            (a_val.get("domain") or "").strip().lower(),
            (a_val.get("item") or "").strip().lower(),
        )
        pair_b = (
            (b_val.get("domain") or "").strip().lower(),
            (b_val.get("item") or "").strip().lower(),
        )
        if "" in pair_a or "" in pair_b:
            return None
        return pair_a == pair_b
    return None


def _dedupe_against_existing(
    new_facts: list[dict],
    existing_facts: list[dict],
    *,
    embed_fn: Callable[[str], list[float] | None] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Route each new fact against the existing brain for the same
    subject.

    Returns ``(kept, reinforce_targets, queue_entries)``:
      - kept: new facts with no prior match; the caller upserts these.
      - reinforce_targets: list of dicts ``{"new_fact": <dict>,
        "existing_id": <str>, "similarity": <float>}`` — caller
        ``reinforce_fact_by_id``s each.
      - queue_entries: list of dicts ``{"new_fact", "suspected_existing_id",
        "similarity"}`` — caller enqueues these for Phase 4 operator review.

    Routing:
      - Type-aware match on `fact_type` + `value` structure first.
        Conclusive match → reinforce (skip embedding).
        Conclusive mismatch → NEW (skip embedding).
        Inconclusive → embed.
      - Cosine ≥ 0.88 → reinforce.
      - 0.75 ≤ cosine < 0.88 → queue.
      - cosine < 0.75 → NEW.

    ``embed_fn`` is injectable so tests can stub it; production should
    pass ``agents.shared.embed.embed``. When it returns None (fastembed
    unavailable or runtime error) the comparison short-circuits to NEW —
    never take down the miner over an embedding failure.
    """
    if not existing_facts:
        return list(new_facts), [], []

    kept: list[dict] = []
    reinforce: list[dict] = []
    queued: list[dict] = []

    # Scope existing to the subject of each new fact. This guards
    # against mixed-subject existing_facts accidentally deduping
    # across people.
    existing_by_subject: dict[str, list[dict]] = {}
    for e in existing_facts:
        existing_by_subject.setdefault(str(e.get("subject", "")), []).append(e)

    for new in new_facts:
        subject = str(new.get("subject", ""))
        candidates = existing_by_subject.get(subject, [])
        if not candidates:
            kept.append(new)
            continue

        n_type = str(new.get("fact_type", "") or "")
        n_val = new.get("value")

        verdict: tuple[str, dict | None, float] = ("new", None, 0.0)
        for ex in candidates:
            e_type = str(ex.get("fact_type", "") or "")
            e_val = ex.get("value")
            structured = _structured_match(n_type, n_val, e_type, e_val)
            if structured is True:
                verdict = ("reinforce", ex, 1.0)
                break
            if structured is False:
                # Definitive mismatch on structured compare — no need
                # to embed. Don't set verdict; move to next candidate.
                continue
            # structured is None → embedding decides.
            if embed_fn is None:
                continue
            n_vec = embed_fn(str(new.get("content", "")))
            e_vec = embed_fn(str(ex.get("content", "")))
            if not n_vec or not e_vec:
                continue
            try:
                from agents.shared.embed import cosine as _cosine  # type: ignore
            except ImportError:
                from embed import cosine as _cosine  # type: ignore
            sim = _cosine(n_vec, e_vec)
            if sim >= DEDUPE_REINFORCE_THRESHOLD:
                verdict = ("reinforce", ex, sim)
                break
            if sim >= DEDUPE_REVIEW_THRESHOLD:
                # Remember the best candidate for the queue route.
                _, best, best_sim = verdict
                if best is None or sim > best_sim:
                    verdict = ("queue", ex, sim)

        label, match, sim = verdict
        if label == "reinforce":
            reinforce.append({
                "new_fact": new,
                "existing_id": match["id"],
                "similarity": sim,
            })
        elif label == "queue":
            queued.append({
                "new_fact": new,
                "suspected_existing_id": match["id"],
                "similarity": sim,
            })
        else:
            kept.append(new)

    return kept, reinforce, queued


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


def append_pending_review(
    facts_dir: Path,
    fact: dict,
    *,
    queue_path: Path | None = None,
) -> None:
    """Append a low-confidence fact marker to
    ``<facts_dir>/_pending_review.md`` (write-only audit trail) AND,
    when ``queue_path`` is given, enqueue a tap-to-resolve item onto the
    pending-review queue the morning-brief digest reads from.

    Idempotent on fact["id"] — re-running the miner on the same window
    doesn't duplicate either the markdown entry or the queue line. The
    markdown file uses atomic tmp+replace; the queue uses append-only
    JSONL with a pre-append id scan (pending_queue.append).
    """
    facts_dir.mkdir(parents=True, exist_ok=True)
    path = facts_dir / "_pending_review.md"
    fact_id = str(fact.get("id") or "")

    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if fact_id and fact_id in {m.strip() for m in _REVIEW_ID_LINE.findall(existing)}:
            already_in_md = True
        else:
            already_in_md = False
    else:
        existing = _REVIEW_HEADER
        already_in_md = False

    if not already_in_md:
        entry = _format_review_entry(fact)
        new_text = existing.rstrip() + "\n\n" + entry + "\n"
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)

    if queue_path is not None and fact_id:
        # Lazy import so test modules that don't use the queue path
        # aren't forced to resolve pending_queue (keeps the existing
        # audit-only callers free of a new hard dependency).
        from pathlib import Path as _P
        try:
            from agents.shared import pending_queue  # type: ignore
        except ImportError:
            import pending_queue  # type: ignore
        pending_queue.append(
            _P(queue_path),
            {
                "id": fact_id,
                "source": "miner",
                "fact": {
                    "id": fact_id,
                    "subject": fact.get("subject", ""),
                    "category": fact.get("category", ""),
                    "content": fact.get("content", ""),
                    "confidence": fact.get("confidence"),
                    "audience_scope": fact.get("audience_scope") or [],
                    "source_detail": fact.get("source_detail", ""),
                    "reason": fact.get("reason", ""),
                },
                "question": "Is this a durable fact worth remembering?",
                "options": ["approve", "reject", "skip"],
                "created_at": _now_iso_for_queue(),
            },
        )


def _now_iso_for_queue() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
