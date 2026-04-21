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

from compose_lib import (  # type: ignore
    apply_post_processing,
    build_compose_prompt,
    parse_compose_result,
)


def _ctx(**overrides):
    base = RecipientContext(
        recipient_person={
            "slug": "priya-rivera",
            "full_name": "Priya Rivera",
            "relationship_type": "family",
            "tone": "warm",
        },
        facts_shareable=[
            {"id": "f-001", "content": "Priya is traveling next week", "recorded_at": "2026-04-10"},
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

def _self_profile(**overrides):
    base = {
        "profile_md": "# the operator\n\n## Self-description\nMarketplace AI leader.\n\n## Level & scope bar\nMust own a lever OR be C-suite-adjacent.\n",
        "level_bar_text": "Must own a lever OR be C-suite-adjacent.",
        "employer_history": [
            {"company": "Example Corp", "title": "Director, Marketplace Data Science",
             "start": "2024-05-01", "end": "2026-01-31"},
        ],
        "current_targets": [
            {"company": "Anthropic", "tier_company": "A", "tier_opportunity": "A",
             "role_type": "Head of AI Science", "outcome": "targeted"},
            {"company": "Wayfair", "tier_company": "B", "tier_opportunity": "A",
             "role_type": "Chief Data Officer", "outcome": "targeted"},
        ],
        "strength_themes": [
            {"theme": "Marketplace systems thinking"},
            {"theme": "Causal ML rigor"},
        ],
    }
    base.update(overrides)
    return base


def test_prompt_cold_inbound_adds_self_context_block():
    prompt = build_compose_prompt(
        _ctx(), _voice(), _inbound(),
        cold_inbound=True, self_profile=_self_profile(),
    )
    assert "SELF CONTEXT" in prompt
    assert "Anthropic" in prompt
    assert "Wayfair" in prompt
    assert "Must own a lever" in prompt
    assert "Marketplace systems thinking" in prompt


def test_prompt_cold_inbound_requests_fit_assessment():
    prompt = build_compose_prompt(
        _ctx(), _voice(), _inbound(),
        cold_inbound=True, self_profile=_self_profile(),
    )
    assert "fit_assessment" in prompt
    assert "FIT CHECK" in prompt
    # Tier-driven strategy guidance
    assert "A-tier" in prompt or "engage warmly" in prompt.lower()


def test_prompt_warm_inbound_omits_self_context():
    """Non-cold (known-sender) drafts don't get the SELF CONTEXT block
    or the fit_assessment schema — existing behavior preserved."""
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    assert "SELF CONTEXT" not in prompt
    assert "fit_assessment" not in prompt
    assert "FIT CHECK" not in prompt


def test_prompt_cold_inbound_without_profile_still_builds():
    """If SELF CONTEXT data is missing (profile hasn't been synthesized
    yet), the prompt should still render a valid fit-check structure
    without crashing."""
    prompt = build_compose_prompt(
        _ctx(), _voice(), _inbound(),
        cold_inbound=True, self_profile=None,
    )
    assert "FIT CHECK" in prompt
    assert "fit_assessment" in prompt


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


def test_prompt_renders_voice_profile_when_present():
    voice = _voice()
    voice["profile_present"] = True
    voice["profile_greeting"] = "Hey {name},"
    voice["profile_signoff"] = "Love, the operator"
    voice["profile_register"] = "intimate"
    voice["profile_patterns"] = ["double-dash em-dashes", "heavy contractions"]
    voice["profile_anti_patterns"] = ["no formal salutations"]
    voice["profile_opening_phrases"] = ["Thanks --", "Yep,"]
    voice["profile_distinctive_traits"] = "Warm-but-efficient rhythm."
    prompt = build_compose_prompt(_ctx(), voice, _inbound())
    assert "LEARNED VOICE PROFILE" in prompt
    assert "Hey {name}" in prompt
    # Signoff is appended post-process, not rendered into the prompt —
    # keeping it out prevents the LLM from emitting one itself.
    assert "Love, the operator" not in prompt
    assert "appended post-process" in prompt
    assert "double-dash em-dashes" in prompt
    assert "no formal salutations" in prompt
    assert "Thanks --" in prompt
    assert "Warm-but-efficient rhythm" in prompt


def test_prompt_omits_profile_section_when_absent():
    voice = _voice()
    voice["profile_present"] = False
    prompt = build_compose_prompt(_ctx(), voice, _inbound())
    assert "LEARNED VOICE PROFILE" not in prompt


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
    assert "Priya is traveling next week" in prompt
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


# --- apply_post_processing: signoff normalization ---

def _parsed_with(draft: str, reply_needed: bool = True) -> dict:
    return {
        "reply_needed": reply_needed,
        "objective": "x",
        "current_state_and_gap": "x",
        "leverage": "x",
        "strategy": "x",
        "recipient_model": "x",
        "draft_text": draft,
        "no_reply_fyi": "" if reply_needed else "noted",
        "reasoning_summary": "x",
        "cited_fact_ids": [],
    }


def test_signoff_appended_when_absent():
    parsed = _parsed_with("Thanks for reaching out. Let's catch up soon.")
    out = apply_post_processing(parsed, {"profile_signoff": "Best,\nBrian"})
    assert out["draft_text"].endswith("Best,\nBrian")
    assert out["draft_text"].startswith("Thanks for reaching out")


def test_signoff_normalizes_bare_name_to_full_form():
    # LLM emitted "the operator" alone; canonical is "Best,\nBrian"
    draft = "Let's catch up.\n\nBrian"
    out = apply_post_processing(_parsed_with(draft), {"profile_signoff": "Best,\nBrian"})
    assert out["draft_text"].endswith("Best,\nBrian")
    # Must not leave a stray "the operator" before the normalized signoff
    assert "the operator\n\nBest,\nBrian" not in out["draft_text"]


def test_signoff_not_duplicated_when_already_canonical():
    draft = "Let's catch up.\n\nBest,\nBrian"
    out = apply_post_processing(_parsed_with(draft), {"profile_signoff": "Best,\nBrian"})
    assert out["draft_text"].count("the operator") == 1
    assert out["draft_text"].endswith("Best,\nBrian")


def test_signoff_replaces_close_variant():
    # LLM emitted "Best, the operator" single-line; canonical is two lines
    draft = "Let's catch up.\n\nBest, the operator"
    out = apply_post_processing(_parsed_with(draft), {"profile_signoff": "Best,\nBrian"})
    assert out["draft_text"].endswith("Best,\nBrian")
    assert out["draft_text"].count("the operator") == 1


def test_signoff_single_line_variant():
    # Some circles use "Love, the operator" as one-line
    draft = "Miss you.\n\nBrian"
    out = apply_post_processing(_parsed_with(draft), {"profile_signoff": "Love, the operator"})
    assert out["draft_text"].endswith("Love, the operator")
    assert out["draft_text"].count("the operator") == 1


def test_signoff_noop_when_profile_missing():
    parsed = _parsed_with("Let's catch up.\n\nBrian")
    out = apply_post_processing(parsed, None)
    assert out["draft_text"] == "Let's catch up.\n\nBrian"


def test_signoff_noop_when_reply_not_needed():
    parsed = _parsed_with("", reply_needed=False)
    out = apply_post_processing(parsed, {"profile_signoff": "Best,\nBrian"})
    assert out["draft_text"] == ""


def test_signoff_noop_when_error():
    parsed = {"error": "bad json", "draft_text": ""}
    out = apply_post_processing(parsed, {"profile_signoff": "Best,\nBrian"})
    assert out.get("draft_text", "") == ""


def test_prompt_no_longer_forbids_signature_block():
    # Schema hint used to say "no signature block" which contradicted the
    # history-matching rule. We now append the signoff post-process.
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    assert "no signature block" not in prompt


# --- apply_post_processing: dehardwrap ---

def test_dehardwrap_collapses_wrapped_prose():
    # LLMs love to hard-wrap at ~68 chars; Gmail renders those as visible
    # early linebreaks. Post-process joins wrapped lines.
    wrapped = (
        "I'd be happy to catch up and hear more about what you're exploring --\n"
        "and where you'd most value a perspective as you think through next\n"
        "steps."
    )
    out = apply_post_processing(_parsed_with(wrapped), None)
    assert "\n" not in out["draft_text"].rstrip(), out["draft_text"]


def test_dehardwrap_preserves_paragraph_breaks():
    wrapped = (
        "Hi Jamie,\n"
        "\n"
        "I'm glad the frameworks have been useful, and no worries at all\n"
        "about the missed note.\n"
        "\n"
        "Would Wednesday 9:30-12pm PT or Thursday 2-5pm PT work for a\n"
        "catch-up call?"
    )
    out = apply_post_processing(_parsed_with(wrapped), None)
    paragraphs = out["draft_text"].split("\n\n")
    assert len(paragraphs) == 3
    assert paragraphs[0] == "Hi Jamie,"
    assert "I'm glad the frameworks have been useful, and no worries at all about the missed note." in paragraphs[1]
    assert "Wednesday 9:30-12pm PT or Thursday 2-5pm PT" in paragraphs[2]


def test_dehardwrap_preserves_bullet_lists():
    wrapped = (
        "Here are a few times that work:\n"
        "\n"
        "- Tuesday 10am-12pm\n"
        "- Wednesday 2-4pm\n"
        "- Thursday 9-11am\n"
        "\n"
        "Let me know which fits."
    )
    out = apply_post_processing(_parsed_with(wrapped), None)
    # The bullet paragraph must keep its internal line breaks
    assert "- Tuesday 10am-12pm\n- Wednesday 2-4pm\n- Thursday 9-11am" in out["draft_text"]


def test_dehardwrap_preserves_numbered_lists():
    wrapped = (
        "Two options:\n"
        "\n"
        "1. Lunch Tuesday\n"
        "2. Dinner Thursday"
    )
    out = apply_post_processing(_parsed_with(wrapped), None)
    assert "1. Lunch Tuesday\n2. Dinner Thursday" in out["draft_text"]


def test_dehardwrap_and_signoff_compose():
    # Combined: hard-wrapped body with bare "the operator" sign-off; canonical
    # signoff is "Best,\nBrian". After post-process, paragraphs should be
    # unwrapped AND the signoff normalized.
    draft = (
        "Hi Jamie,\n"
        "\n"
        "Glad the frameworks have been useful, and no worries at all about\n"
        "the missed note. Hope things have settled down.\n"
        "\n"
        "the operator"
    )
    out = apply_post_processing(
        _parsed_with(draft),
        {"profile_signoff": "Best,\nBrian"},
    )
    assert out["draft_text"].endswith("Best,\nBrian")
    assert (
        "Glad the frameworks have been useful, and no worries at all about "
        "the missed note. Hope things have settled down."
        in out["draft_text"]
    )
    assert out["draft_text"].count("the operator") == 1


def test_dehardwrap_does_not_touch_single_line_paragraphs():
    short = "Yes!"
    out = apply_post_processing(_parsed_with(short), None)
    assert out["draft_text"] == "Yes!"


def test_prompt_tells_llm_not_to_hardwrap():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    # Belt-and-suspenders: explicit prompt instruction alongside the
    # deterministic post-process.
    assert "hard-wrap" in prompt.lower() or "hardwrap" in prompt.lower()


# --- greeting vs reply-opener disambiguation ---

def _voice_with_profile(thread_position: str = "replying") -> dict:
    v = _voice()
    v["thread_position"] = thread_position
    v["profile_present"] = True
    v["profile_greeting"] = "Hi {name},"
    v["profile_signoff"] = "Best,\nBrian"
    v["profile_register"] = "consultative"
    v["profile_patterns"] = [
        "Opens replies with 'Thanks, {name}.' or 'Thank you, {name}.' before any substance",
        "Contractions are near-universal",
    ]
    v["profile_anti_patterns"] = []
    v["profile_opening_phrases"] = ["Hi {name},", "Thanks, {name}."]
    v["profile_distinctive_traits"] = "Consultative-warm."
    return v


def test_prompt_flags_greeting_and_opener_as_alternatives():
    prompt = build_compose_prompt(_ctx(), _voice_with_profile(), _inbound())
    # The profile section must explicitly tell the LLM these are
    # mutually exclusive — don't stack "Hi Jamie," and "Thanks, Jamie."
    assert "ALTERNATIVES" in prompt or "alternatives" in prompt
    assert "never both" in prompt.lower() or "not both" in prompt.lower() or "one or the other" in prompt.lower()


def test_prompt_surfaces_thread_position_for_opener_choice():
    # When replying, the reply-opener ("Thanks, {name}.") is idiomatic;
    # when originating, the greeting ("Hi {name},") is. The prompt must
    # name thread_position so the LLM can pick correctly.
    prompt_reply = build_compose_prompt(_ctx(), _voice_with_profile("replying"), _inbound())
    prompt_orig = build_compose_prompt(_ctx(), _voice_with_profile("originating"), _inbound())
    # Thread position must appear in both (already surfaced via thread_guidance),
    # plus the opener-choice rule must reference it
    assert "thread_position" in prompt_reply.lower() or "replying" in prompt_reply.lower()
    assert "originating" in prompt_orig.lower() or "thread_position" in prompt_orig.lower()


def test_prompt_does_not_stack_both_opener_forms_in_example():
    # Acceptance guard: the prompt must not include anywhere text that
    # models the doubled-name failure (like a sample showing both).
    prompt = build_compose_prompt(_ctx(), _voice_with_profile(), _inbound())
    # Case-insensitive search for a "Hi Name,\n\nThanks, Name." pattern
    # (our failure mode) — shouldn't appear as an example.
    lowered = prompt.lower()
    assert not (
        "hi jamie,\n\nthanks, jamie" in lowered
        or "hi {name},\n\nthanks, {name}" in lowered
    )


# --- recipient timezone awareness ---

def _ctx_with_tz(tz: str):
    ctx = _ctx()
    ctx.recipient_person = dict(ctx.recipient_person)
    ctx.recipient_person["timezone"] = tz
    return ctx


def test_prompt_surfaces_recipient_timezone_when_present():
    prompt = build_compose_prompt(_ctx_with_tz("America/New_York"), _voice(), _inbound())
    # RECIPIENT block must include the tz so the LLM knows what
    # "after 4pm" means for them
    assert "America/New_York" in prompt or "New_York" in prompt


def test_prompt_omits_recipient_tz_line_when_unknown():
    prompt = build_compose_prompt(_ctx(), _voice(), _inbound())
    # The tz line renders only when present; absence means no hint,
    # which is better than "timezone: unknown"
    assert "Time zone: " not in prompt or "Time zone: America" in prompt


def test_open_slots_render_dual_tz_when_recipient_differs():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
    slots = [
        (datetime(2026, 4, 21, 11, 0, tzinfo=PT), datetime(2026, 4, 21, 11, 30, tzinfo=PT)),
    ]
    ctx = _ctx_with_tz("America/New_York")
    prompt = build_compose_prompt(ctx, _voice(), _inbound(), availability_slots=slots)
    # Should show something like "11:00 PT / 14:00 ET" or include both offsets.
    # Accept any rendering that shows the 14:00 translation.
    assert "14:00" in prompt


def test_open_slots_single_tz_when_recipient_matches():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
    slots = [
        (datetime(2026, 4, 21, 11, 0, tzinfo=PT), datetime(2026, 4, 21, 11, 30, tzinfo=PT)),
    ]
    ctx = _ctx_with_tz("America/Los_Angeles")
    prompt = build_compose_prompt(ctx, _voice(), _inbound(), availability_slots=slots)
    # Same tz: don't duplicate
    # 11:00 should appear once in the slots section, not twice
    slots_idx = prompt.find("OPEN SLOTS (propose")
    assert slots_idx != -1
    slots_section_end = prompt.find("\n\n", slots_idx + 50)
    slots_section = prompt[slots_idx:slots_section_end]
    assert slots_section.count("11:00") == 1
