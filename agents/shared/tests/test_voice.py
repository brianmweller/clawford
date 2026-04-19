"""Voice calibration: Brown & Levinson politeness weight, register, and
Huckle's compose_voice_guidance() which assembles a structured prompt for
the draft-compose LLM from (recipient person fields, inbound-act dimensions).

Pure-math and table behaviors are ported verbatim from Flux/flux/voice/
communication.py — same inputs must produce same outputs.
"""
from __future__ import annotations

import pytest

from agents.shared.voice import (
    compose_voice_guidance,
    compute_politeness_weight,
    politeness_strategy,
    recommended_register,
    REGISTER_SCALE,
)


# --- compute_politeness_weight: W(x) = D + P + R ---

def test_weight_when_equals_and_no_distance_is_just_imposition():
    # peer, close, trivial ask
    assert compute_politeness_weight(power_diff=0.0, social_dist=0.0, imposition=0.1) == pytest.approx(0.1)


def test_weight_when_recipient_has_more_power_adds_deference():
    # recipient has power → P(H,S) rises → weight rises
    w = compute_politeness_weight(power_diff=-0.5, social_dist=0.3, imposition=0.5)
    # P = 0.5, D = 0.3, R = 0.5 → 1.3
    assert w == pytest.approx(1.3)


def test_weight_when_speaker_has_more_power_does_not_subtract():
    # Flux clamps P = max(0, -power_diff): speaker-as-boss adds no extra deference
    w = compute_politeness_weight(power_diff=0.8, social_dist=0.3, imposition=0.5)
    # P = 0, D = 0.3, R = 0.5 → 0.8
    assert w == pytest.approx(0.8)


def test_weight_maxes_near_3_for_distant_imposing_request_to_senior():
    # stranger, major imposition, they're very senior
    w = compute_politeness_weight(power_diff=-1.0, social_dist=1.0, imposition=1.0)
    assert w == pytest.approx(3.0)


# --- politeness_strategy boundaries (exact from Flux) ---

@pytest.mark.parametrize("weight,strategy", [
    (0.0,  "direct"),
    (0.49, "direct"),
    (0.5,  "positive_politeness"),
    (0.99, "positive_politeness"),
    (1.0,  "negative_politeness"),
    (1.79, "negative_politeness"),
    (1.8,  "indirect"),
    (2.49, "indirect"),
    (2.5,  "avoid"),
    (3.0,  "avoid"),
])
def test_politeness_strategy_thresholds(weight, strategy):
    assert politeness_strategy(weight) == strategy


# --- recommended_register: platform × direction ---

def test_gmail_lateral_is_consultative_base():
    # gmail base=consultative, lateral modifier=0
    assert recommended_register(direction="lateral", platform="gmail") == "consultative"


def test_gmail_upward_bumps_one_notch_formal():
    # consultative(idx=2) + 1 = formal(idx=3)
    assert recommended_register(direction="upward", platform="gmail") == "formal"


def test_gmail_downward_becomes_casual():
    assert recommended_register(direction="downward", platform="gmail") == "casual"


def test_gmail_personal_clamps_to_intimate():
    # consultative(2) + (-2) = 0 → intimate
    assert recommended_register(direction="personal", platform="gmail") == "intimate"


def test_register_cannot_exceed_formal():
    # formal + upward would be off-scale; must clamp
    # google_messages base=intimate(0), upward=+1 → casual(1). Not relevant here.
    # but gmail upward already formal; going further is also formal.
    assert recommended_register(direction="external", platform="gmail") == "formal"


def test_register_cannot_fall_below_intimate():
    # google_messages base=intimate; personal=-2; must clamp to intimate
    assert recommended_register(direction="personal", platform="google_messages") == "intimate"


def test_unknown_platform_falls_back_to_consultative_base():
    assert recommended_register(direction="lateral", platform="carrier_pigeon") == "consultative"


def test_register_scale_order_is_stable():
    # Downstream code depends on this ordering.
    assert REGISTER_SCALE == ["intimate", "casual", "consultative", "formal"]


# --- compose_voice_guidance: Huckle's top-level builder ---

def test_compose_voice_guidance_returns_structured_fields():
    person = {
        "relationship_type": "family",
        "power_differential": 0.0,
        "social_distance": 0.0,
        "communication_direction": "personal",
    }
    inbound_act = {
        "intent": "inform",
        "audience_shape": "one_to_one",
        "thread_position": "replying",
        "expected_response": "fyi_only",
        "sensitivity": "personal",
        "emotional_valence": "neutral",
        "time_pressure": "on_time",
        "imposition": 0.2,
    }
    guidance = compose_voice_guidance(
        recipient=person,
        inbound_act=inbound_act,
        platform="gmail",
    )
    # Required keys the draft-compose LLM prompt consumes
    for key in [
        "register", "register_guidance",
        "politeness_strategy", "politeness_guidance",
        "direction", "direction_description",
        "power_description", "distance_description",
        "thread_guidance", "response_guidance",
        "sensitivity_guidance", "valence_guidance", "time_pressure_guidance",
        "audience_guidance",
        "politeness_weight",
    ]:
        assert key in guidance, f"missing key: {key}"


def test_family_recipient_on_gmail_gets_intimate_direct():
    # personal direction on gmail clamps to intimate; zero distance + zero power
    # + low imposition should map to direct strategy.
    person = {
        "relationship_type": "family",
        "power_differential": 0.0,
        "social_distance": 0.0,
        "communication_direction": "personal",
    }
    inbound_act = {"intent": "inform", "imposition": 0.1}
    g = compose_voice_guidance(recipient=person, inbound_act=inbound_act, platform="gmail")
    assert g["register"] == "intimate"
    assert g["politeness_strategy"] == "direct"


def test_vendor_recipient_on_gmail_gets_formal_polite():
    # vendor relationship type defaults: external direction, power_diff=+0.3,
    # social_dist=0.6. External + gmail → formal. With imposition=0.5 and
    # speaker-higher-power (P clamps to 0), weight = 0.6 + 0 + 0.5 = 1.1 →
    # negative_politeness.
    person = {
        "relationship_type": "vendor",
        # no overrides → defaults apply
    }
    inbound_act = {"intent": "request", "imposition": 0.5}
    g = compose_voice_guidance(recipient=person, inbound_act=inbound_act, platform="gmail")
    assert g["register"] == "formal"
    assert g["politeness_strategy"] == "negative_politeness"


def test_explicit_person_overrides_beat_relationship_defaults():
    # If the people file has explicit power/distance, those win over
    # RELATIONSHIP_TO_AUDIENCE's DIRECTION_DEFAULTS.
    person = {
        "relationship_type": "vendor",
        "power_differential": -0.9,  # this specific vendor outranks me
        "social_distance": 0.9,
        "communication_direction": "external",
    }
    inbound_act = {"intent": "request", "imposition": 0.5}
    g = compose_voice_guidance(recipient=person, inbound_act=inbound_act, platform="gmail")
    # P(H,S) = 0.9, D = 0.9, R = 0.5 → 2.3 → indirect
    assert g["politeness_strategy"] == "indirect"


def test_unknown_relationship_type_uses_sane_defaults():
    person = {"relationship_type": None}
    inbound_act = {"intent": "inform", "imposition": 0.3}
    g = compose_voice_guidance(recipient=person, inbound_act=inbound_act, platform="gmail")
    # must not raise; register should still be a valid scale value
    assert g["register"] in REGISTER_SCALE
    assert g["politeness_weight"] >= 0.0
