"""Query-time confidence discount — what we remember gets weaker over
time unless we see it again.

Stored `confidence` on every fact is the epistemic value the miner (or
operator) wrote. Effective confidence is what retrieval consumes:

  effective = stored × 0.5 ** (age_days / half_life)

Per-category half-lives live in HALF_LIFE_DAYS below. Some categories
— identity, relationship, birthdate — don't decay (they name durable
truths; knowing someone's birthday doesn't get less true over time).
Those categories map to ``None`` as a sentinel, and the formula
short-circuits to the raw confidence.

`last_reinforced_at` takes precedence over `recorded_at` when computing
age: reinforcement IS a re-observation and should reset the decay
clock. Missing / malformed timestamps short-circuit to raw confidence
(never penalize a fact for a bad date).

Degrade-open: any parse failure returns the raw confidence rather than
raising. Retrieval paths call this in a hot loop; it has to be safe.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# Per-category half-lives in days. None = "never decays."
# the operator's fact taxonomy is narrow; these cover every category the
# miner + scope-augment + Flux importer currently emit.
HALF_LIFE_DAYS: dict[str, int | None] = {
    # Durable truths — no decay.
    "identity": None,           # name, slug, canonical contact info
    "relationship": None,       # "Jamie's son Eliott"
    "birthdate": None,          # dates of birth don't shift
    "established": None,        # legacy Flux-import marker for operator-confirmed

    # Slow-changing traits — 1-year half-life.
    "role": 365,
    "employer": 365,
    "preference": 180,          # preferences do shift; half-life 6 months

    # Evergreen but updatable.
    "health": 90,
    "event": 90,                # life events — kid's kindergarten, new hire

    # Fast-moving state.
    "logistics": 30,            # travel plans, meeting scheduling
    "rumor": 30,                # unverified hearsay
    "guess": 30,                # low-conf inference

    # Fallback for anything unrecognized.
    "default": 180,
}


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp; return None on failure. Accepts
    both 'Z' and explicit-offset forms."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _age_days(fact: dict, now: datetime) -> float | None:
    """Return age in days since the fact was last observed, or None
    when no usable timestamp is available. Prefers last_reinforced_at
    over recorded_at so reinforcement resets the decay clock."""
    for key in ("last_reinforced_at", "recorded_at"):
        ts = fact.get(key)
        parsed = _parse_iso(ts) if isinstance(ts, str) else None
        if parsed is not None:
            # Normalize to UTC so naive/aware mixing doesn't explode.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delta = (now - parsed).total_seconds() / 86400.0
            return delta
    return None


def effective_confidence(fact: dict, now: datetime | None = None) -> float:
    """Return the decay-adjusted confidence, clamped to [0.0, 1.0].

    Degrades open: invalid confidence → 0.0; missing/bad dates → raw
    confidence (no decay applied); "never-decay" categories → raw
    confidence. Future-dated facts (clock skew) also return raw — we
    never INCREASE confidence through this path.
    """
    now = now or datetime.now(timezone.utc)

    try:
        raw = float(fact.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0
    raw = max(0.0, min(1.0, raw))

    category = str(fact.get("category") or "").lower().strip()
    half_life = HALF_LIFE_DAYS.get(category, HALF_LIFE_DAYS["default"])
    if half_life is None:
        return raw

    age = _age_days(fact, now)
    if age is None or age <= 0:
        return raw

    decay = 0.5 ** (age / half_life)
    return max(0.0, min(1.0, raw * decay))


def sort_by_effective_confidence(
    facts: list[dict],
    now: datetime | None = None,
) -> list[dict]:
    """Return a new list sorted by effective_confidence descending.
    Each fact in the output carries an ``effective_confidence`` key
    surfaced for debug + downstream display."""
    now = now or datetime.now(timezone.utc)
    annotated: list[dict] = []
    for f in facts:
        eff = effective_confidence(f, now=now)
        out = dict(f)
        out["effective_confidence"] = eff
        annotated.append(out)
    annotated.sort(key=lambda x: x["effective_confidence"], reverse=True)
    return annotated
