"""build_recipient_context — pure assembler that takes a recipient person,
a set of facts about them, and pre-fetched context blobs (email history,
workflowy, krisp), then returns the bundle draft-compose feeds to the LLM.

The Gmail/Workflowy/Krisp fetchers are separate concerns — this function
is the glue that applies audience filtering and packages the result.
"""
from __future__ import annotations

from agents.shared.context_builder import RecipientContext, build_recipient_context


def _person(**kwargs):
    base = {
        "slug": "test-person",
        "relationship_type": "colleague",
    }
    base.update(kwargs)
    return base


def _fact(fid: str, scope=None, content="x", subject="test-person"):
    return {
        "id": fid,
        "content": content,
        "subject": subject,
        "audience_scope": scope,
        "confidence": 0.9,
        "category": "work",
        "recorded_at": "2026-04-01",
    }


def test_returns_dataclass_with_expected_fields():
    ctx = build_recipient_context(recipient_person=_person(), all_facts=[])
    assert isinstance(ctx, RecipientContext)
    assert ctx.recipient_person["slug"] == "test-person"
    assert ctx.email_history == []
    assert ctx.workflowy_mentions == []
    assert ctx.meeting_summaries == []
    assert ctx.facts_shareable == []
    assert ctx.facts_blocked == []


def test_unscoped_facts_are_shareable():
    # Flux-compatible: fact with no audience_scope is visible to any recipient
    facts = [_fact("f-1", scope=None)]
    ctx = build_recipient_context(recipient_person=_person(), all_facts=facts)
    assert [f["id"] for f in ctx.facts_shareable] == ["f-1"]
    assert ctx.facts_blocked == []


def test_colleague_receives_professional_facts_only():
    facts = [
        _fact("f-pro", scope=["professional"]),
        _fact("f-fam", scope=["family"]),
        _fact("f-per", scope=["personal"]),
    ]
    ctx = build_recipient_context(recipient_person=_person(relationship_type="colleague"), all_facts=facts)
    shareable_ids = [f["id"] for f in ctx.facts_shareable]
    assert "f-pro" in shareable_ids
    assert "f-fam" not in shareable_ids
    assert "f-per" not in shareable_ids
    assert set(ctx.facts_blocked) == {"f-fam", "f-per"}


def test_family_recipient_receives_family_and_personal():
    facts = [
        _fact("f-pro", scope=["professional"]),
        _fact("f-fam", scope=["family"]),
        _fact("f-per", scope=["personal"]),
    ]
    ctx = build_recipient_context(recipient_person=_person(relationship_type="family"), all_facts=facts)
    shareable_ids = set(f["id"] for f in ctx.facts_shareable)
    assert shareable_ids == {"f-fam", "f-per"}
    assert ctx.facts_blocked == ["f-pro"]


def test_multi_scope_fact_visible_when_any_tag_matches():
    facts = [_fact("f-dual", scope=["personal", "family"])]
    # colleague → professional only → fact blocked
    ctx_col = build_recipient_context(recipient_person=_person(relationship_type="colleague"), all_facts=facts)
    assert ctx_col.facts_blocked == ["f-dual"]
    # family → personal + family → fact shareable
    ctx_fam = build_recipient_context(recipient_person=_person(relationship_type="family"), all_facts=facts)
    assert [f["id"] for f in ctx_fam.facts_shareable] == ["f-dual"]


def test_prefetched_inputs_pass_through_unchanged():
    email_history = [{"id": "m-1", "from": "them", "body": "hi"}]
    workflowy = ["Home > Work > Project X"]
    meetings = [{"date": "2026-03-01", "title": "Sync"}]
    ctx = build_recipient_context(
        recipient_person=_person(),
        all_facts=[],
        email_history=email_history,
        workflowy_mentions=workflowy,
        meeting_summaries=meetings,
    )
    assert ctx.email_history == email_history
    assert ctx.workflowy_mentions == workflowy
    assert ctx.meeting_summaries == meetings


def test_none_relationship_defaults_to_professional_audiences():
    facts = [
        _fact("f-pro", scope=["professional"]),
        _fact("f-fam", scope=["family"]),
    ]
    ctx = build_recipient_context(recipient_person=_person(relationship_type=None), all_facts=facts)
    assert [f["id"] for f in ctx.facts_shareable] == ["f-pro"]
    assert ctx.facts_blocked == ["f-fam"]


def test_explicit_override_audiences_bypasses_relationship_mapping():
    facts = [
        _fact("f-pro", scope=["professional"]),
        _fact("f-fam", scope=["family"]),
    ]
    # Even though relationship_type is colleague, override to see family facts
    ctx = build_recipient_context(
        recipient_person=_person(relationship_type="colleague"),
        all_facts=facts,
        override_audiences=["family"],
    )
    shareable_ids = [f["id"] for f in ctx.facts_shareable]
    assert "f-fam" in shareable_ids
    assert "f-pro" not in shareable_ids


# ─── recipient_knows annotation (Phase 5c) ───────────────────────────

def test_recipient_knows_true_when_slug_in_known_by():
    fact = _fact("f-1", scope=["professional"])
    fact["known_by"] = ["test-person", "other-person"]
    ctx = build_recipient_context(
        recipient_person=_person(slug="test-person"),
        all_facts=[fact],
    )
    assert ctx.facts_shareable[0]["recipient_knows"] is True


def test_recipient_knows_false_when_slug_not_in_known_by():
    fact = _fact("f-1", scope=["professional"])
    fact["known_by"] = ["other-person", "someone-else"]
    ctx = build_recipient_context(
        recipient_person=_person(slug="test-person"),
        all_facts=[fact],
    )
    assert ctx.facts_shareable[0]["recipient_knows"] is False


def test_recipient_knows_false_when_known_by_missing():
    fact = _fact("f-1", scope=["professional"])
    # No known_by key at all — treat as "no one we've tracked knows" → False
    ctx = build_recipient_context(
        recipient_person=_person(slug="test-person"),
        all_facts=[fact],
    )
    assert ctx.facts_shareable[0]["recipient_knows"] is False


def test_recipient_knows_case_insensitive_match():
    fact = _fact("f-1", scope=["professional"])
    fact["known_by"] = ["Test-Person"]
    ctx = build_recipient_context(
        recipient_person=_person(slug="test-person"),
        all_facts=[fact],
    )
    assert ctx.facts_shareable[0]["recipient_knows"] is True
