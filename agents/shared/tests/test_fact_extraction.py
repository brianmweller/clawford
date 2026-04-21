"""Unit tests for agents/shared/fact_extraction.py — shared miner helper.

The module extracts durable facts from arbitrary source text (Gmail body,
Krisp transcript, Workflowy note) by invoking the Codex LLM and normalizing
the response into upsert_fact()-ready dicts with audience_scope tagging.

Contract the tests pin down:

1. extract_facts_from_text() returns a list of dicts, each carrying the
   kwargs upsert_fact() wants PLUS a `needs_review` flag.
2. Facts with confidence < MIN_CONFIDENCE (0.3) are dropped silently.
3. Facts with MIN_CONFIDENCE <= confidence < REVIEW_CONFIDENCE (0.6) are
   returned with needs_review=True so the caller can append them to the
   facts/_pending_review.md tracker after upsert.
4. Subjects must be in the provided candidate_slugs; facts about unknown
   slugs are dropped (prevents brain bloat from LLM hallucinations).
5. Self-facts (about Sam Smith) are dropped.
6. audience_scope tags outside VALID_SCOPE_TAGS are filtered; facts whose
   entire scope is invalid are dropped.
7. Malformed LLM output (non-JSON, missing fields, LLM failure) yields []
   without raising.
8. source_detail and idempotency_key are derived from source_context.
9. append_pending_review() writes a stable markdown block atomically.

TDD: tests land before the implementation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload():
    for mod in list(sys.modules):
        if mod == "fact_extraction" or mod.startswith("fact_extraction."):
            del sys.modules[mod]
    import fact_extraction  # type: ignore
    return fact_extraction


@pytest.fixture
def mod():
    return _reload()


def _ok_infer(payload: dict) -> Any:
    """Build a fake InferResult-shaped object with JSON body payload."""
    return SimpleNamespace(
        ok=True,
        text=json.dumps(payload),
        error=None,
        input_tokens=0,
        output_tokens=0,
        model="fake",
    )


def _err_infer(err: str = "down") -> Any:
    return SimpleNamespace(
        ok=False, text=None, error=err,
        input_tokens=0, output_tokens=0, model=None,
    )


# ─── VALID_SCOPE_TAGS exposure ───────────────────────────────────────


def test_valid_scope_tags_match_scope_augment_lib(mod):
    """The scope-augment lib is the existing source of truth. When we
    lift the set here, the values must match exactly so miners and the
    retroactive tagger agree on what's valid."""
    expected = {
        "professional", "personal", "family", "friends",
        "academic", "financial", "legal", "genealogy", "internal", "public",
    }
    assert mod.VALID_SCOPE_TAGS == expected


def test_confidence_thresholds_exposed(mod):
    assert mod.MIN_CONFIDENCE == 0.3
    assert mod.REVIEW_CONFIDENCE == 0.6


# ─── extract_facts_from_text — happy path ────────────────────────────


def _source_ctx(**overrides) -> dict:
    base = {
        "source": "gmail",
        "message_id": "msg-abc123",
        "from_email": "sarah@example.com",
        "direction": "inbound",
    }
    base.update(overrides)
    return base


def test_extract_returns_upsert_kwargs_on_happy_path(mod):
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "identity",
                "content": "She's starting a fintech startup",
                "confidence": 0.85,
                "audience_scope": ["professional"],
                "reason": "stated explicitly in email",
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="some email body",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen", "mike-jones"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert len(results) == 1
    f = results[0]
    assert f["subject"] == "sarah-chen"
    assert f["category"] == "identity"
    assert f["content"] == "She's starting a fintech startup"
    assert f["confidence"] == 0.85
    assert f["audience_scope"] == ["professional"]
    assert f["needs_review"] is False
    # idempotency_key must be deterministic and tied to source message id + slug
    assert "msg-abc123" in f["idempotency_key"]
    assert "sarah-chen" in f["idempotency_key"]
    # source_detail follows the <source>:<id> convention
    assert f["source_detail"] == "gmail:msg-abc123"


def test_extract_flags_medium_confidence_for_review(mod):
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "preference",
                "content": "Possibly moving to Austin in June",
                "confidence": 0.45,
                "audience_scope": ["personal"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert len(results) == 1
    assert results[0]["needs_review"] is True
    assert results[0]["confidence"] == 0.45


def test_extract_drops_low_confidence_below_min(mod):
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "preference",
                "content": "Might like ramen",
                "confidence": 0.15,
                "audience_scope": ["personal"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert results == []


# ─── Filters ─────────────────────────────────────────────────────────


def test_extract_drops_self_subject(mod):
    """Facts about the operator himself are noise — the brain only tracks other
    people. Use _SELF_NAME_VARIANTS from flux_import_lib plus the slug
    'sam-smith' as the filter."""
    payload = {
        "facts": [
            {
                "subject_slug": "sam-smith",
                "category": "identity",
                "content": "He's fundraising",
                "confidence": 0.9,
                "audience_scope": ["professional"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sam-smith", "sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert results == []


def test_extract_drops_unknown_slug(mod):
    """LLM hallucinated a subject not in the candidate set → drop.
    Prevents bloating the brain with unknown people."""
    payload = {
        "facts": [
            {
                "subject_slug": "ghost-person",
                "category": "identity",
                "content": "Works at Acme",
                "confidence": 0.8,
                "audience_scope": ["professional"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert results == []


def test_extract_drops_fact_with_empty_content(mod):
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "identity",
                "content": "",
                "confidence": 0.9,
                "audience_scope": ["professional"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert results == []


def test_extract_drops_fact_with_all_invalid_scope(mod):
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "identity",
                "content": "Likes sushi",
                "confidence": 0.9,
                "audience_scope": ["bogus", "alsobogus"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert results == []


def test_extract_keeps_partial_valid_scope(mod):
    """Mix of valid + invalid tags → keep the valid ones."""
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "identity",
                "content": "Likes sushi",
                "confidence": 0.9,
                "audience_scope": ["personal", "invented-tag"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert len(results) == 1
    assert results[0]["audience_scope"] == ["personal"]


def test_extract_drops_fact_with_missing_scope(mod):
    """audience_scope is required at write time — no fact may land without it."""
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "identity",
                "content": "Likes sushi",
                "confidence": 0.9,
                # audience_scope omitted entirely
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert results == []


# ─── Multiple facts in one response ──────────────────────────────────


def test_extract_returns_multiple_facts_preserving_order(mod):
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "identity",
                "content": "Runs product at Acme",
                "confidence": 0.9,
                "audience_scope": ["professional"],
            },
            {
                "subject_slug": "mike-jones",
                "category": "preference",
                "content": "Prefers async comms",
                "confidence": 0.75,
                "audience_scope": ["professional"],
            },
        ]
    }
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen", "mike-jones"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert [f["subject"] for f in results] == ["sarah-chen", "mike-jones"]


# ─── LLM failure modes ───────────────────────────────────────────────


def test_extract_returns_empty_on_llm_failure(mod):
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _err_infer("network down"),
    )
    assert results == []


def test_extract_returns_empty_on_malformed_json(mod):
    bad = SimpleNamespace(
        ok=True, text="this is not json at all",
        error=None, input_tokens=0, output_tokens=0, model="fake",
    )
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: bad,
    )
    assert results == []


def test_extract_strips_markdown_fence(mod):
    """LLM sometimes wraps JSON in ```json ... ``` despite json_mode."""
    fenced = SimpleNamespace(
        ok=True,
        text='```json\n' + json.dumps({
            "facts": [
                {
                    "subject_slug": "sarah-chen",
                    "category": "identity",
                    "content": "Runs product",
                    "confidence": 0.9,
                    "audience_scope": ["professional"],
                }
            ]
        }) + '\n```',
        error=None, input_tokens=0, output_tokens=0, model="fake",
    )
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: fenced,
    )
    assert len(results) == 1
    assert results[0]["subject"] == "sarah-chen"


def test_extract_returns_empty_when_no_candidates(mod):
    """Empty candidate_slugs → nothing to extract (save the LLM call)."""
    called = [False]
    def tracking_infer(prompt, **kw):
        called[0] = True
        return _ok_infer({"facts": []})
    results = mod.extract_facts_from_text(
        text="...",
        source_context=_source_ctx(),
        candidate_slugs=set(),
        infer_fn=tracking_infer,
    )
    assert results == []
    assert called[0] is False, "should short-circuit before calling LLM"


# ─── source_detail shape per source type ─────────────────────────────


def test_extract_source_detail_for_krisp(mod):
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "identity",
                "content": "Runs product",
                "confidence": 0.9,
                "audience_scope": ["professional"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="transcript body",
        source_context={
            "source": "krisp",
            "event_id": "krisp_meeting_123",
            "attendees": ["sarah@example.com"],
        },
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert results[0]["source_detail"] == "krisp:krisp_meeting_123"
    assert "krisp_meeting_123" in results[0]["idempotency_key"]


def test_extract_source_detail_for_workflowy(mod):
    payload = {
        "facts": [
            {
                "subject_slug": "sarah-chen",
                "category": "identity",
                "content": "Runs product",
                "confidence": 0.9,
                "audience_scope": ["professional"],
            }
        ]
    }
    results = mod.extract_facts_from_text(
        text="note body",
        source_context={"source": "workflowy", "node_id": "node-xyz"},
        candidate_slugs={"sarah-chen"},
        infer_fn=lambda prompt, **kw: _ok_infer(payload),
    )
    assert results[0]["source_detail"] == "workflowy:node-xyz"


# ─── append_pending_review ───────────────────────────────────────────


def test_append_pending_review_creates_file_with_header(mod, tmp_path):
    fact = {
        "id": "connector-sarah-chen-msg-abc",
        "subject": "sarah-chen",
        "category": "preference",
        "content": "Possibly moving to Austin",
        "confidence": 0.45,
        "audience_scope": ["personal"],
        "source_detail": "gmail:msg-abc",
        "reason": "inferred from passing mention",
    }
    facts_dir = tmp_path / "facts"
    mod.append_pending_review(facts_dir, fact)
    review_path = facts_dir / "_pending_review.md"
    assert review_path.exists()
    text = review_path.read_text(encoding="utf-8")
    assert "Possibly moving to Austin" in text
    assert "0.45" in text
    assert "gmail:msg-abc" in text
    assert "sarah-chen" in text


def test_append_pending_review_appends_multiple_entries(mod, tmp_path):
    facts_dir = tmp_path / "facts"
    f1 = {
        "id": "connector-sarah-chen-1",
        "subject": "sarah-chen",
        "category": "preference",
        "content": "First fact",
        "confidence": 0.45,
        "audience_scope": ["personal"],
        "source_detail": "gmail:m1",
        "reason": "a",
    }
    f2 = {
        "id": "connector-mike-jones-1",
        "subject": "mike-jones",
        "category": "identity",
        "content": "Second fact",
        "confidence": 0.5,
        "audience_scope": ["professional"],
        "source_detail": "gmail:m2",
        "reason": "b",
    }
    mod.append_pending_review(facts_dir, f1)
    mod.append_pending_review(facts_dir, f2)
    text = (facts_dir / "_pending_review.md").read_text(encoding="utf-8")
    assert "First fact" in text
    assert "Second fact" in text


def test_append_pending_review_idempotent_on_same_id(mod, tmp_path):
    """Re-running the miner on the same window shouldn't duplicate
    pending-review entries."""
    facts_dir = tmp_path / "facts"
    fact = {
        "id": "connector-sarah-chen-dup",
        "subject": "sarah-chen",
        "category": "preference",
        "content": "Unique content",
        "confidence": 0.45,
        "audience_scope": ["personal"],
        "source_detail": "gmail:dup",
        "reason": "r",
    }
    mod.append_pending_review(facts_dir, fact)
    mod.append_pending_review(facts_dir, fact)
    text = (facts_dir / "_pending_review.md").read_text(encoding="utf-8")
    assert text.count("Unique content") == 1


# --- age normalization: store approx birthdates, not age strings ---

def test_prompt_surfaces_statement_date_when_internal_date_present(mod):
    # Gmail miner passes internalDate as epoch-ms str; the extraction
    # prompt needs to translate that to an ISO date so the LLM can do
    # age math. 1713704568000 = 2024-04-21.
    source_context = {
        "source": "gmail",
        "message_id": "m1",
        "internal_date": "1713704568000",
    }
    prompt = mod.build_extraction_prompt(
        text="hello",
        source_context=source_context,
        candidate_slugs={"jamie-fitzgerald"},
    )
    assert "2024-04-21" in prompt
    assert "STATEMENT DATE" in prompt or "statement date" in prompt.lower()


def test_prompt_includes_age_normalization_rule(mod):
    prompt = mod.build_extraction_prompt(
        text="hello",
        source_context={"source": "gmail", "message_id": "m1"},
        candidate_slugs={"someone"},
    )
    # Must name the rule: convert ages → approximate birth date
    low = prompt.lower()
    assert "age" in low and ("birth" in low or "birthday" in low or "birthdate" in low)
    # Must instruct NOT to store raw age strings ("approaching 5")
    assert (
        "approximate" in low
        or "approx" in low
    )


def test_prompt_gives_concrete_age_normalization_example(mod):
    prompt = mod.build_extraction_prompt(
        text="hello",
        source_context={"source": "gmail", "message_id": "m1"},
        candidate_slugs={"someone"},
    )
    # Concrete worked example — "approaching 5" on 2026-04 → born ~2021-06
    # The LLM gets the arithmetic right much more reliably with an example.
    assert "approaching 5" in prompt.lower() or "approaching 3" in prompt.lower() or "'age" in prompt.lower()
