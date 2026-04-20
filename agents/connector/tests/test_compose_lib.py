"""Pure helpers for Huckle Cat's draft-compose flow.

build_compose_prompt: turns a RecipientContext + voice guidance + an
inbound email into the LLM prompt for generating a draft reply.

parse_compose_result: normalizes the JSON the LLM returns, enforcing that
any cited_fact_ids refer to facts we told the LLM were shareable (so the
LLM can't invent a reference to a fact that was blocked by the audience
filter).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make sibling scripts importable for tests
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from agents.shared.context_builder import RecipientContext

from compose_lib import build_compose_prompt, parse_compose_result  # type: ignore


# --- build_compose_prompt ---

def _ctx(**overrides):
    base = RecipientContext(
        recipient_person={
            "slug": "priya-rivera",
            "full_name": "Priya Rivera",
            "relationship_type": "family",
            "tone": "warm",
        },
        facts_shareable=[
            {"id": "f-001", "content": "Priya's chemo is Monday", "recorded_at": "2026-04-10"},
        ],
        facts_blocked=["f-999-secret"],
        target_audiences=["personal", "family"],
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _voice():
    return {
        "register": "intimate",
        "register_guidance": "Very informal. Fragments OK.",
        "politeness_strategy": "direct",
        "politeness_guidance": "Be direct and clear.",
        "direction": "personal",
        "direction_description": "warm, authentic, match their energy",
        "power_description": "Roughly equal standing",
        "distance_description": "Very close relationship",
        "thread_guidance": "Responding to their message.",
        "response_guidance": "Keep it brief.",
        "sensitivity_guidance": "Personal/emotional topic.",
        "valence_guidance": "Standard professional tone.",
        "time_pressure_guidance": "Normal response timing.",
        "audience_guidance": "Personal, direct tone.",
    }


def _inbound():
    return {
        "from_email": "priya@example.com",
        "from_name": "Priya Rivera",
        "subject": "Sunday lunch?",
        "body": "Hey honey, are you free for lunch on Sunday?",
        "received_at": "2026-04-18T14:30:00Z",
    }


def test_prompt_includes_recipient_name():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    assert "Priya Rivera" in prompt


def test_prompt_includes_register_and_politeness_strategy():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    assert "intimate" in prompt
    assert "direct" in prompt


def test_prompt_includes_shareable_facts_but_never_blocked():
    ctx = _ctx()
    ctx.facts_blocked = ["f-999-secret"]
    prompt = build_compose_prompt(ctx, _voice(), _inbound())
    assert "Priya's chemo is Monday" in prompt
    assert "f-001" in prompt
    assert "f-999-secret" not in prompt


def test_prompt_includes_inbound_subject_and_body():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    assert "Sunday lunch?" in prompt
    assert "are you free for lunch on Sunday?" in prompt


def test_prompt_includes_availability_slots_when_provided():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
    slots = [
        (datetime(2026, 4, 21, 11, 0, tzinfo=PT), datetime(2026, 4, 21, 11, 30, tzinfo=PT)),
        (datetime(2026, 4, 22, 14, 0, tzinfo=PT), datetime(2026, 4, 22, 14, 30, tzinfo=PT)),
    ]
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound(), availability_slots=slots)
    # Should surface the day and time in a recognizable form
    assert "Tue" in prompt or "Tuesday" in prompt or "Apr 21" in prompt
    assert "11:00" in prompt


def test_prompt_omits_availability_section_when_no_slots():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound(), availability_slots=None)
    # Simple sentinel: no "Available slots" header
    assert "Available slots" not in prompt


def test_prompt_requests_json_output_shape():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    # The LLM must return JSON with these keys so draft-compose can parse it
    assert "draft_text" in prompt
    assert "reasoning_summary" in prompt
    assert "cited_fact_ids" in prompt


# --- parse_compose_result ---

def test_parse_valid_result_returns_normalized_dict():
    llm_text = json.dumps({
        "draft_text": "Hi Priya, yes — Sunday works. See you at 12?",
        "reasoning_summary": "Intimate register, chemo fact cited for context",
        "cited_fact_ids": ["f-001"],
    })
    result = parse_compose_result(llm_text, shareable_ids={"f-001"})
    assert result["draft_text"].startswith("Hi Priya")
    assert result["reasoning_summary"]
    assert result["cited_fact_ids"] == ["f-001"]


def test_parse_strips_invented_citations_not_in_shareable_set():
    # LLM might hallucinate a fact_id; we must drop it before showing the user
    llm_text = json.dumps({
        "draft_text": "Hi!",
        "reasoning_summary": "r",
        "cited_fact_ids": ["f-001", "f-hallucinated"],
    })
    result = parse_compose_result(llm_text, shareable_ids={"f-001"})
    assert result["cited_fact_ids"] == ["f-001"]


def test_parse_malformed_json_returns_error_shape():
    result = parse_compose_result("not json", shareable_ids=set())
    assert "error" in result
    assert result.get("draft_text") in (None, "")


def test_parse_missing_required_fields_returns_error():
    # LLM returned JSON but omitted draft_text
    llm_text = json.dumps({"reasoning_summary": "r"})
    result = parse_compose_result(llm_text, shareable_ids=set())
    assert "error" in result


def test_parse_empty_cited_ids_is_valid():
    # Many drafts won't need to cite facts — that's fine
    llm_text = json.dumps({
        "draft_text": "Got it, thanks!",
        "reasoning_summary": "Simple acknowledgement",
        "cited_fact_ids": [],
    })
    result = parse_compose_result(llm_text, shareable_ids={"f-001"})
    assert result["cited_fact_ids"] == []
    assert "error" not in result
