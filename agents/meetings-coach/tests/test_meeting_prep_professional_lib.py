"""Tests for agents/meetings-coach/scripts/meeting_prep_professional_lib.py.

Contract:
- classify_meeting_type(event, attendees_resolved, self_profile) -> str
  in {recruiter-screen, hiring-manager, hiring-panel, general}
- match_attendee_to_target(attendees, profile) -> dict | None
- match_attendee_to_search_stage(attendees, profile) -> dict | None
- build_llm_prep_prompt(...) -> str (pure, testable without LLM)
- build_llm_prep(...) -> dict (calls LLM; fallback on error)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from agents.shared.self_profile import SelfProfile

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "meeting_prep_professional_lib",
    _SCRIPTS_DIR / "meeting_prep_professional_lib.py",
)
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _event(summary="Chat", description="", attendees=None):
    return {
        "id": "evt-1",
        "summary": summary,
        "description": description,
        "attendees": attendees or [],
        "start": "2026-04-25T10:00:00-07:00",
        "end": "2026-04-25T10:30:00-07:00",
    }


def _att(email, name="", person_data=None):
    return {"email": email, "name": name, "person_data": person_data}


def _profile_with_targets_and_pipeline():
    return SelfProfile(
        profile_md="# Profile\n\n## Level & scope bar\nTest bar.\n",
        level_bar_text="Test bar.",
        target_companies=[
            {"company": "Anthropic", "outcome": "targeted", "tier_company": "A"},
            {"company": "Google DeepMind", "outcome": "targeted", "tier_company": "A"},
            {"company": "Old Employer", "outcome": "landed", "tier_company": "B"},
        ],
        active_search_pipeline=[
            {"company": "Anthropic", "stage": "hiring-manager",
             "last_signal_date": "2026-04-15", "notes": "H-M with Josh"},
            {"company": "FooCorp", "stage": "technical-interview",
             "last_signal_date": "2026-04-10", "notes": "loop prep"},
        ],
        strength_themes=[
            {"theme": "marketplace economics"},
            {"theme": "causal inference"},
        ],
    )


# ---------------------------------------------------------------------------
# classify_meeting_type — no self_profile edge case
# ---------------------------------------------------------------------------


def test_classify_returns_general_when_self_profile_has_no_data():
    """If self/ doesn't exist, never classify as professional."""
    event = _event(
        summary="Phone screen with Acme",
        attendees=[_att("recruiter@acme.com")],
    )
    assert lib.classify_meeting_type(event, [_att("recruiter@acme.com")],
                                     SelfProfile()) == "general"


# ---------------------------------------------------------------------------
# classify_meeting_type — relationship-type hit
# ---------------------------------------------------------------------------


def test_classify_relationship_type_recruiter_wins():
    """Promoted cold recruiter: person file has relationship_type=recruiter."""
    profile = _profile_with_targets_and_pipeline()
    att = _att(
        "jane@lever.co",
        person_data={"slug": "jane-recruiter", "relationship_type": "recruiter"},
    )
    event = _event(summary="Intro call", attendees=[att])
    assert lib.classify_meeting_type(event, [att], profile) == "recruiter-screen"


# ---------------------------------------------------------------------------
# classify_meeting_type — ATS domain match
# ---------------------------------------------------------------------------


def test_classify_ats_domain_triggers_recruiter_screen():
    profile = _profile_with_targets_and_pipeline()
    att = _att("jane@greenhouse-mail.io")
    event = _event(summary="Quick chat", attendees=[att])
    assert lib.classify_meeting_type(event, [att], profile) == "recruiter-screen"


def test_classify_organizer_on_ats_subdomain_triggers_recruiter_screen():
    """Adobe-style scheduled interview: organizer is 'schedule@
    interview.adobe.com', no attendees on the invite (interviewer is
    only named in the description body), title is the generic
    'Meeting Confirmation'. The ATS subdomain signal on the organizer
    alone must classify this as a recruiter screen — otherwise prep
    never fires for big-company in-house interview systems."""
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Meeting Confirmation - Sam Smith",
                   attendees=[])
    event["organizer"] = "schedule@interview.adobe.com"
    assert lib.classify_meeting_type(event, [], profile) == "recruiter-screen"


def test_classify_organizer_on_ats_subdomain_as_dict():
    """Same as above, but `organizer` comes through as a dict (GCal's
    native shape is {email, displayName, self}) — some fetchers pass it
    through verbatim instead of flattening to a string."""
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Final round", attendees=[])
    event["organizer"] = {"email": "no-reply@recruiting.amazon.com",
                          "displayName": "Amazon Interviews"}
    assert lib.classify_meeting_type(event, [], profile) == "recruiter-screen"


def test_classify_organizer_non_ats_stays_general():
    """Regression guard: ordinary meeting with an unrelated organizer
    domain must not be dragged into recruiter-screen."""
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Team sync", attendees=[])
    event["organizer"] = "boss@example.com"
    assert lib.classify_meeting_type(event, [], profile) == "general"


# ---------------------------------------------------------------------------
# classify_meeting_type — target company domain match
# ---------------------------------------------------------------------------


def test_classify_target_company_domain_is_hiring_panel_default():
    """Attendee at a target company, no hiring-manager keywords → panel."""
    profile = _profile_with_targets_and_pipeline()
    att = _att("alice@anthropic.com", name="Alice")
    event = _event(summary="Anthropic interview loop", attendees=[att])
    assert lib.classify_meeting_type(event, [att], profile) == "hiring-panel"


def test_classify_target_company_with_hiring_manager_keyword():
    profile = _profile_with_targets_and_pipeline()
    att = _att("josh@anthropic.com", name="Josh")
    event = _event(
        summary="Hiring manager 1:1 with Josh",
        attendees=[att],
    )
    assert lib.classify_meeting_type(event, [att], profile) == "hiring-manager"


def test_classify_multi_token_target_matches_first_token():
    """Google DeepMind (two tokens) — attendee @google.com matches."""
    profile = _profile_with_targets_and_pipeline()
    att = _att("ashley@google.com")
    event = _event(summary="DS loop prep", attendees=[att])
    assert lib.classify_meeting_type(event, [att], profile) == "hiring-panel"


# ---------------------------------------------------------------------------
# classify_meeting_type — title keyword + person file
# ---------------------------------------------------------------------------


def test_classify_title_keyword_with_known_person_is_recruiter_screen():
    """Known contact + interview/phone-screen keyword."""
    profile = _profile_with_targets_and_pipeline()
    att = _att(
        "friend@neutralcorp.com",
        person_data={"slug": "friend", "relationship_type": "friend"},
    )
    event = _event(summary="Phone screen", attendees=[att])
    assert lib.classify_meeting_type(event, [att], profile) == "recruiter-screen"


def test_classify_title_keyword_without_person_file_stays_general():
    """Title match but no known attendee = too ambiguous to classify."""
    profile = _profile_with_targets_and_pipeline()
    att = _att("mystery@unknown.com")  # person_data=None
    event = _event(summary="Phone screen about something", attendees=[att])
    assert lib.classify_meeting_type(event, [att], profile) == "general"


# ---------------------------------------------------------------------------
# classify_meeting_type — no match
# ---------------------------------------------------------------------------


def test_classify_family_attendee_is_general():
    profile = _profile_with_targets_and_pipeline()
    att = _att(
        "alex@example.com",
        person_data={"slug": "alex-rivera", "relationship_type": "partner"},
    )
    event = _event(summary="Weekend plans", attendees=[att])
    assert lib.classify_meeting_type(event, [att], profile) == "general"


# ---------------------------------------------------------------------------
# Target and pipeline matchers
# ---------------------------------------------------------------------------


def test_match_attendee_to_target_finds_anthropic():
    profile = _profile_with_targets_and_pipeline()
    match = lib.match_attendee_to_target(
        [_att("alice@anthropic.com")], profile,
    )
    assert match is not None
    assert match["company"] == "Anthropic"


def test_match_attendee_to_target_excludes_landed_companies():
    profile = _profile_with_targets_and_pipeline()
    # Old Employer has outcome="landed" — current_targets filters it out.
    match = lib.match_attendee_to_target(
        [_att("foo@oldemployer.com")], profile,
    )
    assert match is None


def test_match_attendee_to_search_stage_finds_anthropic():
    profile = _profile_with_targets_and_pipeline()
    stage = lib.match_attendee_to_search_stage(
        [_att("alice@anthropic.com")], profile,
    )
    assert stage is not None
    assert stage["stage"] == "hiring-manager"


def test_match_attendee_to_search_stage_returns_none_when_no_match():
    profile = _profile_with_targets_and_pipeline()
    stage = lib.match_attendee_to_search_stage(
        [_att("stranger@randomco.com")], profile,
    )
    assert stage is None


# ---------------------------------------------------------------------------
# build_llm_prep_prompt — pure, testable
# ---------------------------------------------------------------------------


def test_llm_prep_prompt_includes_meeting_title_and_attendee():
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="H-M chat", attendees=[_att("josh@anthropic.com")])
    prompt = lib.build_llm_prep_prompt(
        event=event,
        attendees_resolved=[_att("josh@anthropic.com", name="Josh")],
        self_profile=profile,
        meeting_type="hiring-manager",
        target_company_match={"company": "Anthropic", "tier_company": "A"},
        search_stage_match={"stage": "hiring-manager",
                            "last_signal_date": "2026-04-15",
                            "notes": "H-M with Josh"},
    )
    assert "H-M chat" in prompt
    assert "Josh" in prompt
    assert "Anthropic" in prompt
    assert "hiring-manager" in prompt
    # Output schema hint — new 8-field schema
    assert "recipient_model" in prompt
    assert "objective" in prompt
    assert "fit_pitch" in prompt
    assert "compelling_angle" in prompt
    assert "fit_evidence" in prompt
    assert "evaluation_questions" in prompt
    assert "red_flags" in prompt


def test_llm_prep_prompt_mentions_level_bar_when_present():
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Recruiter call")
    prompt = lib.build_llm_prep_prompt(
        event=event,
        attendees_resolved=[_att("jane@lever.co")],
        self_profile=profile,
        meeting_type="recruiter-screen",
        target_company_match=None,
        search_stage_match=None,
    )
    assert "Test bar" in prompt


def test_llm_prep_prompt_omits_pipeline_section_when_no_match():
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Random recruiter")
    prompt = lib.build_llm_prep_prompt(
        event=event,
        attendees_resolved=[_att("jane@lever.co")],
        self_profile=profile,
        meeting_type="recruiter-screen",
        target_company_match=None,
        search_stage_match=None,
    )
    # No late-stage-with-<company> mention
    assert "Anthropic" not in prompt or "Not in active pipeline" in prompt


# ---------------------------------------------------------------------------
# build_llm_prep — integration with fake infer
# ---------------------------------------------------------------------------


def test_build_llm_prep_parses_full_schema(monkeypatch):
    """8-field schema: recipient_model, objective, fit_pitch,
    compelling_angle, fit_evidence, evaluation_questions, red_flags,
    prep_summary."""
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Call with Josh")

    class _FakeResult:
        ok = True
        text = (
            '{"recipient_model": "Michelle needs to hear a crisp pitch.",'
            ' "objective": "Evaluate match + make the case if it is.",'
            ' "fit_pitch": "Led causal-ML orgs at scale across two marketplaces.",'
            ' "compelling_angle": "Flywheel economics is my home turf.",'
            ' "fit_evidence": ["Shipped X", "Authored Y", "Built Z"],'
            ' "evaluation_questions": ["Q1", "Q2", "Q3"],'
            ' "red_flags": ["R1"],'
            ' "prep_summary": "One-line framing."}'
        )

    def _fake_infer(**kwargs):
        return _FakeResult()

    monkeypatch.setattr(lib, "infer", _fake_infer)

    out = lib.build_llm_prep(
        event=event,
        attendees_resolved=[_att("josh@anthropic.com", name="Josh")],
        self_profile=profile,
        meeting_type="hiring-manager",
    )
    assert out["recipient_model"].startswith("Michelle")
    assert "Evaluate match" in out["objective"]
    assert "causal-ML" in out["fit_pitch"]
    assert "Flywheel" in out["compelling_angle"]
    assert out["fit_evidence"] == ["Shipped X", "Authored Y", "Built Z"]
    assert out["evaluation_questions"] == ["Q1", "Q2", "Q3"]
    assert out["red_flags"] == ["R1"]
    assert out["prep_summary"] == "One-line framing."


def test_build_llm_prep_accepts_legacy_field_names(monkeypatch):
    """Earlier prompt versions emitted questions_to_ask + talking_points.
    The normalizer must still accept those — callers migrate over time."""
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Call")

    class _FakeResult:
        ok = True
        text = (
            '{"questions_to_ask": ["Legacy Q"],'
            ' "talking_points": ["Legacy TP"]}'
        )

    monkeypatch.setattr(lib, "infer", lambda **kwargs: _FakeResult())

    out = lib.build_llm_prep(
        event=event,
        attendees_resolved=[_att("x@y.com")],
        self_profile=profile,
        meeting_type="recruiter-screen",
    )
    assert out["evaluation_questions"] == ["Legacy Q"]
    assert out["fit_evidence"] == ["Legacy TP"]


def test_build_llm_prep_returns_error_on_llm_failure(monkeypatch):
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Call")

    class _FakeResult:
        ok = False
        text = ""
        error = "timeout"

    monkeypatch.setattr(lib, "infer", lambda **kwargs: _FakeResult())

    out = lib.build_llm_prep(
        event=event,
        attendees_resolved=[_att("x@y.com")],
        self_profile=profile,
        meeting_type="recruiter-screen",
    )
    assert "error" in out


def test_build_llm_prep_returns_error_on_malformed_json(monkeypatch):
    profile = _profile_with_targets_and_pipeline()
    event = _event(summary="Call")

    class _FakeResult:
        ok = True
        text = "not valid json"

    monkeypatch.setattr(lib, "infer", lambda **kwargs: _FakeResult())

    out = lib.build_llm_prep(
        event=event,
        attendees_resolved=[_att("x@y.com")],
        self_profile=profile,
        meeting_type="recruiter-screen",
    )
    assert "error" in out
