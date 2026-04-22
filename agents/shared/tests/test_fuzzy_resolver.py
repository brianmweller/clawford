"""Tests for agents/shared/fuzzy_resolver.py — generic fuzzy-descriptor
matching for operator tools.

Extracted from the connector-specific Gmail-thread resolver. Every
operator tool that accepts an opaque identifier (thread_id, event_id,
item_id, slug) plus a user-friendly name should be convertable into
this abstraction: caller provides candidates + per-flow config, the
resolver handles the matching, ambiguity, and not-found paths
identically.

Contract:
  - id_pattern: regex that matches explicit-id passthroughs
  - person_resolver: optional Callable[str, {slug, email, ...}] for
    name-based resolution (typically brain.get_person)
  - candidates: iterable of Candidate dicts with searchable fields
  - returns ResolutionResult (status, id, matched, candidates, recent)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SHARED))

from fuzzy_resolver import (  # type: ignore
    Candidate,
    resolve_fuzzy_descriptor,
    result_to_dict,
)


# ---------------------------------------------------------------------------
# Minimal fixture — two distinct domains to prove generality
# ---------------------------------------------------------------------------


def _email_pool():
    """Thread candidates from an email domain."""
    return [
        Candidate(
            id="19db1111111111aa",
            display="Jamie Fitzgerald — Re: hello!",
            last_seen="2026-04-21T20:00:00Z",
            name="",
            email="jamie.fitzgerald@gmail.com",
            subject="Re: hello!",
            slug="jamie-fitzgerald",
        ),
        Candidate(
            id="19db2222222222bb",
            display="Michelle Leist — Reddit",
            last_seen="2026-04-21T21:00:00Z",
            email="michelle@rivierapartners.com",
            subject="Senior Director role at Reddit",
            slug="michelle-leist",
        ),
    ]


def _meeting_pool():
    """Meeting candidates from a calendar domain — proves the same
    abstraction works for a different opaque-id type."""
    return [
        Candidate(
            id="evt_abc123xyz",
            display="1:1 with Michelle — Thu 10am",
            last_seen="2026-04-24",
            email="michelle@rivierapartners.com",
            subject="1:1 with Michelle",
        ),
        Candidate(
            id="evt_board456def",
            display="Q2 board meeting — Fri 2pm",
            last_seen="2026-04-25",
            subject="Q2 board meeting",
        ),
    ]


# ---------------------------------------------------------------------------
# Explicit-id passthrough
# ---------------------------------------------------------------------------


def test_explicit_id_passthrough_hex():
    """Gmail-style thread id (hex 14-22) passes through directly."""
    r = resolve_fuzzy_descriptor(
        "19db2222222222bb",
        candidates=_email_pool(),
        id_pattern=r"^[0-9a-f]{14,22}$",
    )
    assert r.status == "ok"
    assert r.id == "19db2222222222bb"


def test_explicit_id_passthrough_respects_pattern():
    """A hex-like string that matches the id_pattern is treated as an
    id even if not in the candidate pool — lets programmatic callers
    pass ids the pool doesn't know about yet."""
    r = resolve_fuzzy_descriptor(
        "19dbffffffffffff",
        candidates=_email_pool(),
        id_pattern=r"^[0-9a-f]{14,22}$",
    )
    assert r.status == "ok"
    assert r.id == "19dbffffffffffff"


def test_id_pattern_works_for_non_hex_ids():
    """The resolver must be domain-agnostic. Prove it with GCal-shape
    ids (evt_<alphanum>) that don't look like Gmail thread_ids."""
    r = resolve_fuzzy_descriptor(
        "evt_abc123xyz",
        candidates=_meeting_pool(),
        id_pattern=r"^evt_[A-Za-z0-9]+$",
    )
    assert r.status == "ok"
    assert r.id == "evt_abc123xyz"


# ---------------------------------------------------------------------------
# Email / subject matching
# ---------------------------------------------------------------------------


def test_exact_email_match_returns_ok():
    r = resolve_fuzzy_descriptor(
        "michelle@rivierapartners.com",
        candidates=_email_pool(),
    )
    assert r.status == "ok"
    assert r.id == "19db2222222222bb"


def test_substring_on_subject_returns_ok():
    r = resolve_fuzzy_descriptor(
        "Reddit",
        candidates=_email_pool(),
    )
    assert r.status == "ok"
    assert r.id == "19db2222222222bb"


def test_case_insensitive_matching():
    r = resolve_fuzzy_descriptor("MICHELLE", candidates=_email_pool())
    assert r.status == "ok"
    assert r.id == "19db2222222222bb"


# ---------------------------------------------------------------------------
# Person-resolver integration
# ---------------------------------------------------------------------------


def test_person_resolver_bridges_name_to_candidate_slug():
    """Given a person_resolver that returns {slug, email}, the resolver
    bridges 'Jamie' → jamie-fitzgerald's slug/email → matches candidate
    with that slug or email."""
    def fake_person(name):
        if name.lower().startswith("jamie"):
            return {"slug": "jamie-fitzgerald",
                    "email": "jamie.fitzgerald@gmail.com",
                    "raw": "# Jamie Fitzgerald\n- email: jamie.fitzgerald@gmail.com"}
        return None

    r = resolve_fuzzy_descriptor(
        "Jamie",
        candidates=_email_pool(),
        person_resolver=fake_person,
    )
    assert r.status == "ok"
    assert r.id == "19db1111111111aa"


def test_person_resolver_returns_none_falls_back_to_substring():
    """When person_resolver doesn't know the name, the resolver must
    still try substring matching — not fail fast."""
    r = resolve_fuzzy_descriptor(
        "jamie",  # no person_resolver, but substring on email matches
        candidates=_email_pool(),
        person_resolver=lambda n: None,  # always returns None
    )
    assert r.status == "ok"
    assert r.id == "19db1111111111aa"


# ---------------------------------------------------------------------------
# Ambiguous + not_found
# ---------------------------------------------------------------------------


def test_ambiguous_descriptor_returns_candidates():
    """Add two candidates sharing a substring to force ambiguity."""
    pool = _email_pool() + [
        Candidate(id="19db3333333333cc", display="Michelle S — Reddit",
                  email="michelle@other.com", subject="Reddit onsite prep"),
    ]
    r = resolve_fuzzy_descriptor("Reddit", candidates=pool)
    assert r.status == "ambiguous"
    ids = {c.id for c in r.candidates}
    assert "19db2222222222bb" in ids
    assert "19db3333333333cc" in ids


def test_not_found_returns_recent_candidates():
    r = resolve_fuzzy_descriptor(
        "zzznothinglikethat",
        candidates=_email_pool(),
        recent_count=5,
    )
    assert r.status == "not_found"
    assert r.recent is not None
    assert len(r.recent) <= 5


def test_empty_descriptor_returns_not_found():
    r = resolve_fuzzy_descriptor("", candidates=_email_pool())
    assert r.status == "not_found"


def test_empty_candidates_returns_not_found():
    r = resolve_fuzzy_descriptor("anything", candidates=[])
    assert r.status == "not_found"


# ---------------------------------------------------------------------------
# Match-reason enumeration (useful for debugging ambiguity)
# ---------------------------------------------------------------------------


def test_match_reason_names_all_firing_signals():
    """When multiple signals fire (slug + email + substring), the
    resolved candidate's match_reason should enumerate all of them."""
    def fake_person(name):
        return {"slug": "michelle-leist",
                "email": "michelle@rivierapartners.com",
                "raw": ""}

    r = resolve_fuzzy_descriptor(
        "Michelle",
        candidates=_email_pool(),
        person_resolver=fake_person,
    )
    assert r.status == "ok"
    reason = r.matched.match_reason
    assert "slug=michelle-leist" in reason
    assert "person_email=" in reason or "email" in reason


# ---------------------------------------------------------------------------
# Ranking: strong signals (slug match) rank above substring-only
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# result_to_dict — standardized per-domain shape
# ---------------------------------------------------------------------------


def test_result_to_dict_ok_uses_domain_id_key():
    """Each domain names the id differently — thread_id, meeting_id,
    event_id — but the shape is identical otherwise."""
    result = resolve_fuzzy_descriptor(
        "Michelle", candidates=_email_pool(),
    )
    out = result_to_dict(
        result,
        id_key="thread_id",
        recent_key="recent_threads",
    )
    assert out["status"] == "ok"
    assert out["thread_id"] == "19db2222222222bb"
    assert "matched" in out


def test_result_to_dict_ambiguous_uses_candidate_formatter():
    """Callers pass a formatter to extract domain-specific fields from
    each Candidate's extras dict."""
    pool = [
        Candidate(id="evt_1", display="A", subject="Board Meeting",
                  extras={"start": "2026-04-22T10:00:00-07:00"}),
        Candidate(id="evt_2", display="B", subject="Board Call",
                  extras={"start": "2026-04-23T14:00:00-07:00"}),
    ]
    result = resolve_fuzzy_descriptor("board", candidates=pool)

    def _fmt(c):
        return {
            "event_id": c.id,
            "title": c.subject,
            "start": (c.extras or {}).get("start", ""),
            "match_reason": c.match_reason,
        }

    out = result_to_dict(
        result,
        id_key="event_id",
        recent_key="recent_events",
        candidate_formatter=_fmt,
    )
    assert out["status"] == "ambiguous"
    assert {c["event_id"] for c in out["candidates"]} == {"evt_1", "evt_2"}
    assert all("title" in c for c in out["candidates"])


def test_result_to_dict_not_found_uses_domain_recent_key():
    result = resolve_fuzzy_descriptor(
        "nothingmatches", candidates=_email_pool(),
    )
    out = result_to_dict(
        result,
        id_key="thread_id",
        recent_key="recent_threads",
    )
    assert out["status"] == "not_found"
    assert "recent_threads" in out
    assert "reason" in out


def test_result_to_dict_default_formatter_works_without_caller_fmt():
    """Omitting candidate_formatter should produce a sensible default
    shape for quick wiring."""
    result = resolve_fuzzy_descriptor(
        "Reddit", candidates=_email_pool(),
    )
    out = result_to_dict(
        result, id_key="thread_id", recent_key="recent_threads",
    )
    assert out["status"] == "ok"
    matched = out["matched"]
    assert matched["thread_id"] == "19db2222222222bb"
    assert "subject" in matched
    assert "match_reason" in matched


def test_ambiguous_matches_rank_strong_signals_first():
    """If 'Jamie' matches both via person-resolver (strong) AND via a
    substring hit on someone else's header, the resolver orders the
    candidates list with the strong-signal match first."""
    pool = _email_pool() + [
        Candidate(id="19db4444444444dd",
                  display="Jamie Park — Texting",
                  email="jamiepark@example.com",
                  subject="Re: Jamie is trying something new"),
    ]

    def fake_person(name):
        if name.lower().startswith("jamie"):
            return {"slug": "jamie-fitzgerald",
                    "email": "jamie.fitzgerald@gmail.com",
                    "raw": ""}
        return None

    r = resolve_fuzzy_descriptor(
        "jamie",
        candidates=pool,
        person_resolver=fake_person,
    )
    if r.status == "ambiguous":
        # Strong signal (slug match) should be first.
        assert r.candidates[0].id == "19db1111111111aa"
    else:
        # If the strong signal was decisive enough to narrow to one,
        # that's also acceptable — person_resolver resolution is
        # stronger than substring.
        assert r.id == "19db1111111111aa"
