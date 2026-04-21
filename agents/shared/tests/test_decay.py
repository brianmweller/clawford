"""Tests for agents/shared/decay.py — query-time confidence discount
that models "what we remember gets weaker over time unless we see it
again."

Stored confidence is the epistemic value the miner wrote. Effective
confidence is what the retrieval path uses: stored × 0.5^(age_days /
half_life). Per-category half-lives in HALF_LIFE_DAYS; fallback via
the "default" key.

Contract pinned here:
- identity / relationship / birthdate don't decay (half_life=None → return raw)
- default half_life applies to unknown categories
- last_reinforced_at resets the clock when present (reinforcement IS
  a re-observation and should count)
- Missing / malformed recorded_at → return raw (never penalize a fact
  for not carrying a date)
- Returned value is bounded to [0.0, 1.0]
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import decay  # type: ignore


NOW = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)


def _fact(**overrides) -> dict:
    base = {
        "id": "x",
        "subject": "sarah-chen",
        "content": "y",
        "confidence": 0.8,
        "category": "event",
        "recorded_at": "2026-04-21T12:00:00Z",
    }
    base.update(overrides)
    return base


# ─── effective_confidence ────────────────────────────────────────────


def test_fresh_fact_returns_raw_confidence():
    f = _fact(recorded_at="2026-04-21T12:00:00Z")
    assert decay.effective_confidence(f, now=NOW) == 0.8


def test_one_halflife_halves_confidence():
    # event has a 90-day half-life (per HALF_LIFE_DAYS default below)
    f = _fact(category="event", recorded_at="2026-01-21T12:00:00Z")  # 90 days ago
    out = decay.effective_confidence(f, now=NOW)
    assert abs(out - 0.4) < 0.01  # 0.8 * 0.5 = 0.4


def test_two_halflives_quarters_confidence():
    # event, 180 days ago → two half-lives → 0.8 * 0.25 = 0.2
    f = _fact(category="event", recorded_at="2025-10-23T12:00:00Z")
    out = decay.effective_confidence(f, now=NOW)
    assert abs(out - 0.2) < 0.02


def test_identity_fact_never_decays():
    f = _fact(category="identity", recorded_at="2020-01-01T12:00:00Z")
    assert decay.effective_confidence(f, now=NOW) == 0.8


def test_relationship_fact_never_decays():
    f = _fact(category="relationship", recorded_at="2020-01-01T12:00:00Z")
    assert decay.effective_confidence(f, now=NOW) == 0.8


def test_birthdate_fact_never_decays():
    f = _fact(category="birthdate", recorded_at="2020-01-01T12:00:00Z")
    assert decay.effective_confidence(f, now=NOW) == 0.8


def test_unknown_category_uses_default_halflife():
    # "mystery" isn't in the table — must still decay, not crash
    f = _fact(category="mystery", recorded_at="2025-10-23T12:00:00Z")
    out = decay.effective_confidence(f, now=NOW)
    assert 0.0 <= out < 0.8


def test_last_reinforced_at_resets_clock():
    # Fact was recorded a year ago but reinforced yesterday — should
    # look fresh, not ancient.
    f = _fact(
        category="event",
        recorded_at="2025-04-21T12:00:00Z",  # 365 days ago
        last_reinforced_at="2026-04-20T12:00:00Z",  # 1 day ago
    )
    out = decay.effective_confidence(f, now=NOW)
    assert out > 0.79  # essentially fresh (0.8 × 0.5^(1/90) ≈ 0.794)


def test_missing_recorded_at_returns_raw():
    f = _fact()
    f.pop("recorded_at")
    assert decay.effective_confidence(f, now=NOW) == 0.8


def test_malformed_recorded_at_returns_raw():
    f = _fact(recorded_at="not-a-date")
    assert decay.effective_confidence(f, now=NOW) == 0.8


def test_non_numeric_confidence_returns_zero():
    f = _fact(confidence="wat")
    assert decay.effective_confidence(f, now=NOW) == 0.0


def test_output_clamped_to_zero_minimum():
    # Very old + aggressive half-life shouldn't go negative even on
    # rounding; monotonic: close to 0.0 is OK.
    f = _fact(category="logistics", recorded_at="2020-01-01T12:00:00Z")
    out = decay.effective_confidence(f, now=NOW)
    assert out >= 0.0


def test_future_recorded_at_returns_raw():
    # Clock skew shouldn't INCREASE confidence
    f = _fact(recorded_at="2027-01-01T12:00:00Z")
    out = decay.effective_confidence(f, now=NOW)
    assert out == 0.8


# ─── sorted_by_effective ────────────────────────────────────────────


def test_sort_reorders_by_effective_confidence():
    f_old_high = _fact(id="a", category="event", confidence=0.9,
                       recorded_at="2025-01-21T12:00:00Z")  # 90d ago-ish
    f_fresh_mid = _fact(id="b", category="event", confidence=0.7,
                        recorded_at="2026-04-20T12:00:00Z")
    f_identity_old = _fact(id="c", category="identity", confidence=0.85,
                           recorded_at="2020-01-01T12:00:00Z")
    out = decay.sort_by_effective_confidence(
        [f_old_high, f_fresh_mid, f_identity_old], now=NOW,
    )
    ids = [f["id"] for f in out]
    # identity (no decay, 0.85) > fresh-mid (≈0.7) > old-high (decayed below 0.5)
    assert ids[0] == "c"
    assert ids[1] == "b"
    assert ids[2] == "a"


def test_sort_surfaces_effective_confidence_on_each_fact():
    f = _fact(category="event", recorded_at="2026-01-21T12:00:00Z")
    out = decay.sort_by_effective_confidence([f], now=NOW)
    assert "effective_confidence" in out[0]
    assert abs(out[0]["effective_confidence"] - 0.4) < 0.01


# ─── HALF_LIFE_DAYS surface ─────────────────────────────────────────


def test_half_life_table_names_expected_categories():
    # These are the categories the miner + scope-augment currently
    # emit; the table should cover them with a sensible choice.
    required = {"identity", "relationship", "event", "preference",
                "role", "birthdate", "employer", "default"}
    assert required.issubset(decay.HALF_LIFE_DAYS.keys())


def test_identity_and_birthdate_are_sentinel_none():
    # Using None as the sentinel for "never decays" — do NOT encode
    # this as 0 (which would be a divide-by-zero trap).
    assert decay.HALF_LIFE_DAYS["identity"] is None
    assert decay.HALF_LIFE_DAYS["birthdate"] is None
    assert decay.HALF_LIFE_DAYS["relationship"] is None
