"""Pure helpers for Huckle Cat's draft-compose flow.

build_compose_prompt: turns a RecipientContext + voice guidance + an
inbound email into the LLM prompt for generating a draft reply. The prompt
forces a four-step structured reasoning pass (objective, state/gap,
strategy, recipient_model) BEFORE any prose — a draft without explicit
intent is a pleasantry, not a reply.

parse_compose_result: validates all four reasoning fields are populated,
then strips any cited_fact_ids the LLM invented that weren't in the
shareable set.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from agents.shared.context_builder import RecipientContext

from compose_lib import build_compose_prompt, parse_compose_result  # type: ignore


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


def _valid_llm_json(**overrides):
    base = {
        "reply_needed": True,
        "objective": "Say yes to lunch while keeping plans flexible",
        "current_state_and_gap": "the operator is free Sunday; needs to lock in time and venue.",
        "leverage": "Known Sunday availability; prior shared lunch spots both already like.",
        "strategy": "Confirm enthusiastically, propose a specific time, leave venue open.",
        "recipient_model": "Priya expects a warm, quick yes; overthinking reads as distance.",
        "draft_text": "Yes! How about 12:30 — you pick the spot?",
        "no_reply_fyi": "",
        "reasoning_summary": "Objective: accept lunch; strategy: quick warm yes with a time anchor.",
        "cited_fact_ids": [],
    }
    base.update(overrides)
    return json.dumps(base)


def _valid_no_reply_json(**overrides):
    base = {
        "reply_needed": False,
        "objective": "Preserve the warm closeout impression Josh extended; don't dilute it.",
        "current_state_and_gap": "the operator's prior email already deployed the substantive pitch. Josh's reply is a gracious closeout. No gap to close.",
        "leverage": "Restraint is the leverage; the prior email already landed.",
        "strategy": "Silence is the move. A reply would re-open a thread Josh just closed warmly.",
        "recipient_model": "Josh is not expecting a substantive reply; silence reads as confidence.",
        "draft_text": "",
        "no_reply_fyi": "Josh — warm closeout after rejection. Door open for future roles. No reply needed.",
        "reasoning_summary": "Warm closeout; prior pitch already landed; restraint preserves the frame.",
        "cited_fact_ids": [],
    }
    base.update(overrides)
    return json.dumps(base)


# --- build_compose_prompt ---

def test_prompt_requires_five_step_reasoning_in_order():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    for label in ("OBJECTIVE", "CURRENT STATE AND GAP", "LEVERAGE", "STRATEGY", "RECIPIENT MODEL"):
        assert label in prompt, f"missing label: {label}"
    # Order is meaningful — leverage must come BEFORE strategy
    idx_obj = prompt.index("OBJECTIVE")
    idx_lev = prompt.index("LEVERAGE")
    idx_strat = prompt.index("STRATEGY")
    idx_recip = prompt.index("RECIPIENT MODEL")
    assert idx_obj < idx_lev < idx_strat < idx_recip


def test_prompt_explicitly_rejects_pleasantry_drafts():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    # Some version of "pleasantry, not a reply" must land
    assert "pleasantry" in prompt.lower()


def test_prompt_schema_hints_all_required_fields():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    for key in (
        "reply_needed", "objective", "current_state_and_gap", "leverage", "strategy",
        "recipient_model", "draft_text", "no_reply_fyi", "reasoning_summary", "cited_fact_ids",
    ):
        assert key in prompt, f"output schema missing {key}"


def test_prompt_instructs_on_when_reply_not_needed():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    # Must explicitly name cases where silence is the right call
    assert "warm closeout" in prompt.lower() or "closeout" in prompt.lower()
    # Must also flag that leverage-available-now can override the no-reply default
    assert "leverage" in prompt.lower()


def test_prompt_labels_history_as_voice_anchor():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    assert "VOICE ANCHOR" in prompt
    # And the prompt must make clear history beats abstract register
    assert "HISTORY WINS" in prompt


def test_prompt_flags_voice_anchors_as_context_specific():
    # Prevents the "use most-recent anchor regardless of context" failure
    # that shipped "she likes flipping through it" in reply to a gift offer.
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    assert "CONTEXT-SPECIFIC" in prompt or "context-specific" in prompt


def test_prompt_recipient_model_covers_emotional_dimension():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    # Must explicitly name the emotional-outcome dimension, not just task
    assert "emotional" in prompt.lower()
    # Must name at least one concrete emotional-pattern example so the LLM
    # has anchors beyond the abstract label
    examples = ["gift-giver", "advice-giver", "well-wisher", "closeout-sender"]
    found = sum(1 for e in examples if e.lower() in prompt.lower())
    assert found >= 2, f"expected at least 2 emotional-pattern examples, found {found}"


def test_prompt_requires_concrete_scheduling_proposal():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    # The SCHEDULING RULE must appear so drafts don't round-trip decisions
    assert "SCHEDULING RULE" in prompt
    # Must explicitly reject the "happy to if you're around" failure mode
    assert "if you're around" in prompt.lower() or "defer" in prompt.lower()
    # Must mention freehand fallback when OPEN SLOTS don't match
    assert "FREEHAND" in prompt or "freehand" in prompt


def test_prompt_instructs_date_reanchoring_for_stale_inbounds():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    # Stale relative dates ("next week") must re-anchor to reply date
    assert "re-anchor" in prompt.lower() or "reanchor" in prompt.lower()


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
    assert "Tue" in prompt or "Tuesday" in prompt or "Apr 21" in prompt
    assert "11:00" in prompt


def test_prompt_omits_open_slots_section_when_no_slots():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound(), availability_slots=None)
    # The OPEN SLOTS *header* (with its parenthetical) only renders when slots
    # are provided. The TASK section still mentions "OPEN SLOTS" as guidance;
    # that's fine — we just don't want the listing block.
    assert "OPEN SLOTS (propose" not in prompt


# --- parse_compose_result ---

def test_parse_valid_result_returns_normalized_dict():
    result = parse_compose_result(_valid_llm_json(), shareable_ids={"f-001"})
    assert "error" not in result
    assert result["reply_needed"] is True
    assert result["objective"].startswith("Say yes")
    assert result["draft_text"].startswith("Yes!")
    assert result["cited_fact_ids"] == []


def test_parse_valid_no_reply_result():
    result = parse_compose_result(_valid_no_reply_json(), shareable_ids=set())
    assert "error" not in result
    assert result["reply_needed"] is False
    assert result["draft_text"] == ""
    assert "warm closeout" in result["no_reply_fyi"].lower()


def test_parse_rejects_missing_reply_needed():
    raw = json.loads(_valid_llm_json())
    del raw["reply_needed"]
    result = parse_compose_result(json.dumps(raw), shareable_ids=set())
    assert "error" in result
    assert "reply_needed" in result["error"]


def test_parse_rejects_non_bool_reply_needed():
    raw = json.loads(_valid_llm_json())
    raw["reply_needed"] = "yes"
    result = parse_compose_result(json.dumps(raw), shareable_ids=set())
    assert "error" in result


def test_parse_rejects_reply_needed_true_with_empty_draft():
    bad = _valid_llm_json(draft_text="")
    result = parse_compose_result(bad, shareable_ids=set())
    assert "error" in result
    assert "draft_text" in result["error"]


def test_parse_rejects_reply_needed_false_with_empty_fyi():
    bad = _valid_no_reply_json(no_reply_fyi="")
    result = parse_compose_result(bad, shareable_ids=set())
    assert "error" in result
    assert "no_reply_fyi" in result["error"]


def test_parse_rejects_missing_objective():
    bad = _valid_llm_json(objective="")
    result = parse_compose_result(bad, shareable_ids=set())
    assert "error" in result
    assert "reasoning" in result["error"].lower() or "objective" in result["error"].lower()


def test_parse_rejects_missing_strategy():
    bad = _valid_llm_json(strategy="")
    result = parse_compose_result(bad, shareable_ids=set())
    assert "error" in result


def test_parse_rejects_missing_recipient_model():
    bad = _valid_llm_json(recipient_model="")
    result = parse_compose_result(bad, shareable_ids=set())
    assert "error" in result


def test_parse_rejects_missing_leverage():
    bad = _valid_llm_json(leverage="")
    result = parse_compose_result(bad, shareable_ids=set())
    assert "error" in result


def test_parse_valid_result_includes_leverage_field():
    result = parse_compose_result(_valid_llm_json(), shareable_ids=set())
    assert "leverage" in result
    assert result["leverage"].startswith("Known Sunday")


def test_parse_strips_markdown_json_fences():
    # claude CLI often wraps output in ```json ... ```
    fenced = f"```json\n{_valid_llm_json()}\n```"
    result = parse_compose_result(fenced, shareable_ids=set())
    assert "error" not in result
    assert result["objective"].startswith("Say yes")


def test_parse_strips_bare_triple_backtick_fences():
    fenced = f"```\n{_valid_llm_json()}\n```"
    result = parse_compose_result(fenced, shareable_ids=set())
    assert "error" not in result


def test_prompt_includes_full_history_not_truncated_at_300_chars():
    """The Sharon/Nando/Dana voucher mention in a real the operator email was past
    the 300-char cutoff in an earlier compose_lib version, which made the
    LLM miss the leverage. History must be preserved in full."""
    ctx = _ctx()
    long_body = "first sentence. " + ("filler content. " * 20) + "Sharon, Nando, Dana."
    ctx.email_history = [{"from": "the operator", "body": long_body, "date": "2026-04-14T14:42:20Z"}]
    prompt = build_compose_prompt(ctx, _voice(), _inbound())
    assert "Sharon, Nando, Dana" in prompt


def test_parse_rejects_missing_current_state_and_gap():
    bad = _valid_llm_json(current_state_and_gap="")
    result = parse_compose_result(bad, shareable_ids=set())
    assert "error" in result


def test_parse_strips_invented_citations_not_in_shareable_set():
    txt = _valid_llm_json(cited_fact_ids=["f-001", "f-hallucinated"])
    result = parse_compose_result(txt, shareable_ids={"f-001"})
    assert result["cited_fact_ids"] == ["f-001"]


def test_parse_malformed_json_returns_error_shape():
    result = parse_compose_result("not json", shareable_ids=set())
    assert "error" in result
    assert result.get("draft_text") in (None, "")




def test_parse_empty_cited_ids_is_valid():
    txt = _valid_llm_json(cited_fact_ids=[])
    result = parse_compose_result(txt, shareable_ids={"f-001"})
    assert result["cited_fact_ids"] == []
    assert "error" not in result
