"""Voice calibration — politeness weight, register recommendation, and the
composed voice guidance Huckle Cat passes to the draft-compose LLM.

Math and tables are ported verbatim from Flux/flux/voice/communication.py:
same inputs produce same outputs. The only divergence is that Huckle's
inbound-email flow classifies pragmatic dimensions (intent, sensitivity, etc.)
via an LLM pre-pass instead of Flux's English-keyword heuristics — those
keyword classifiers are intentionally NOT ported.

compose_voice_guidance() is Huckle-specific and is the only public entry
point draft-compose.py should use.
"""
from __future__ import annotations

from agents.shared.audience import defaults_for_relationship


# ---------------------------------------------------------------------------
# Brown & Levinson politeness: W(x) = D(S,H) + P(H,S) + R(x)
# ---------------------------------------------------------------------------

def compute_politeness_weight(
    power_diff: float,
    social_dist: float,
    imposition: float = 0.5,
) -> float:
    p = max(0.0, -power_diff)
    return social_dist + p + imposition


def politeness_strategy(weight: float) -> str:
    if weight < 0.5:
        return "direct"
    if weight < 1.0:
        return "positive_politeness"
    if weight < 1.8:
        return "negative_politeness"
    if weight < 2.5:
        return "indirect"
    return "avoid"


STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "direct": "Be direct and clear. No hedging needed.",
    "positive_politeness": "Be friendly and warm. Show solidarity and common ground.",
    "negative_politeness": "Be respectful and give options. Use hedges like 'Would it be possible...' or 'I was wondering if...'",
    "indirect": "Be very diplomatic. Frame requests as suggestions. Minimize imposition.",
    "avoid": "Consider whether this communication is necessary. If so, use maximum indirectness.",
}


# ---------------------------------------------------------------------------
# Register recommendation (Communication Accommodation Theory)
# ---------------------------------------------------------------------------

PLATFORM_REGISTERS: dict[str, str] = {
    "gmail": "consultative",
    "email": "consultative",
    "slack": "casual",
    "whatsapp": "casual",
    "google_messages": "intimate",
}

DIRECTION_REGISTER_MODIFIERS: dict[str, int] = {
    "upward":   1,
    "downward": -1,
    "lateral":  0,
    "external": 1,
    "personal": -2,
}

REGISTER_SCALE = ["intimate", "casual", "consultative", "formal"]

REGISTER_GUIDANCE: dict[str, str] = {
    "formal":       "Use complete sentences, proper grammar, and professional salutations. Avoid contractions, slang, and emoji.",
    "consultative": "Use standard business tone. Contractions are fine. Clear and professional but not stiff.",
    "casual":       "Relaxed tone. Short sentences, informal language. Emoji OK if it matches your style.",
    "intimate":     "Very informal. Fragments OK. Inside jokes and shorthand fine. Match the conversational energy.",
}


def recommended_register(direction: str, platform: str) -> str:
    base = PLATFORM_REGISTERS.get(platform, "consultative")
    base_idx = REGISTER_SCALE.index(base) if base in REGISTER_SCALE else 2
    modifier = DIRECTION_REGISTER_MODIFIERS.get(direction, 0)
    final_idx = max(0, min(len(REGISTER_SCALE) - 1, base_idx + modifier))
    return REGISTER_SCALE[final_idx]


DIRECTION_DESCRIPTIONS: dict[str, str] = {
    "upward":   "communicating to someone with more authority/seniority — show deference, be concise, respect their time",
    "downward": "communicating to someone you manage/mentor — be clear and directive, empower them, focus on enablement",
    "lateral":  "communicating to a peer/equal — collaborative tone, mutual respect, can be direct",
    "external": "communicating to someone outside your organization — professional, clear context, don't assume shared knowledge",
    "personal": "communicating with a friend or family member — warm, authentic, match their energy",
}


# ---------------------------------------------------------------------------
# CommunicationAct dimension guidance (ported verbatim from Flux)
# ---------------------------------------------------------------------------

AUDIENCE_SHAPES: dict[str, str] = {
    "one_to_one":  "Personal, direct tone. Use their name. Be specific and conversational.",
    "one_to_few":  "Address the group by name or role. Be clear about who needs to do what.",
    "one_to_many": "Broadcast tone. Be concise and scannable. No inside references.",
}

THREAD_POSITIONS: dict[str, str] = {
    "originating":   "Starting a new conversation. Provide full context — the recipient has no prior thread.",
    "replying":      "Responding to their message. Acknowledge what they said before adding your points.",
    "forwarding":    "Sharing someone else's message. Add context about why you're forwarding.",
    "following_up":  "Circling back on a prior thread. Reference the original topic and any time elapsed.",
    "reopening":     "Reviving a dormant thread. Re-establish context before diving in.",
}

EXPECTED_RESPONSES: dict[str, str] = {
    "action_required":  "Be explicit about what you need and by when. End with a clear ask.",
    "approval_needed":  "State what you're requesting approval for. Make it easy to say yes or no.",
    "fyi_only":         "Say explicitly that no response is needed. Keep it brief.",
    "open_discussion":  "Invite input. Ask open-ended questions. Signal that you want their perspective.",
}

SENSITIVITY_LEVELS: dict[str, str] = {
    "public":       "Standard message. No special handling needed.",
    "internal":     "Internal only. Don't include external-facing language or links.",
    "confidential": "Sensitive content. Don't quote or forward. Be discreet in phrasing.",
    "personal":     "Personal/emotional topic. Handle with extra care and empathy.",
}

EMOTIONAL_VALENCES: dict[str, str] = {
    "positive":  "Lead with the good news. Match the energy — be warm and enthusiastic.",
    "negative":  "Empathy first. Acknowledge the difficulty before getting to business.",
    "neutral":   "Standard professional tone. No special emotional calibration needed.",
    "sensitive": "Tread carefully. Be compassionate and measured. Avoid flippancy.",
}

TIME_PRESSURES: dict[str, str] = {
    "on_time":     "Normal response timing. No special mention needed.",
    "late_reply":  "Acknowledge the delay briefly before getting to the substance.",
    "urgent":      "Convey urgency clearly but without panic. Front-load the key information.",
    "no_pressure": "No time sensitivity. Take a relaxed, unhurried tone.",
}


# ---------------------------------------------------------------------------
# Huckle's top-level composer — takes a recipient person record + an LLM-
# classified inbound act and returns structured voice guidance the
# draft-compose LLM renders into a system prompt.
# ---------------------------------------------------------------------------

def _describe_power(power_diff: float) -> str:
    if power_diff < -0.3:
        return "They have more authority/seniority than you"
    if power_diff > 0.3:
        return "You have more authority/seniority than them"
    return "Roughly equal standing"


def _describe_distance(social_dist: float) -> str:
    if social_dist < 0.2:
        return "Very close relationship"
    if social_dist < 0.4:
        return "Moderate familiarity"
    if social_dist < 0.7:
        return "Professional distance"
    return "Formal/distant relationship"


def compose_voice_guidance(
    recipient: dict,
    inbound_act: dict,
    platform: str = "gmail",
) -> dict:
    """Compose the voice guidance bundle for draft-compose.

    Args:
        recipient: person record fields from a people/*.md file. Read:
            - relationship_type (optional)
            - communication_direction, power_differential, social_distance
              (optional overrides; if absent, relationship-type defaults apply)
        inbound_act: LLM-classified dimensions of the inbound email. Read:
            - intent (currently unused by the pure math but surfaced)
            - imposition (0.0-1.0, required for the B&L weight)
            - audience_shape, thread_position, expected_response,
              sensitivity, emotional_valence, time_pressure (all optional;
              fall through to reasonable defaults)
        platform: "gmail" by default. Passed through to register selection.

    Returns:
        Dict containing register, politeness strategy, direction descriptions,
        and per-dimension guidance strings. Ready to be rendered into an
        LLM system prompt by draft-compose.py.
    """
    defaults = defaults_for_relationship(recipient.get("relationship_type"))
    direction = recipient.get("communication_direction") or defaults["direction"]
    power_diff = recipient.get("power_differential")
    if power_diff is None:
        power_diff = defaults["power_diff"]
    social_dist = recipient.get("social_distance")
    if social_dist is None:
        social_dist = defaults["social_dist"]

    imposition = float(inbound_act.get("imposition", 0.5))
    weight = compute_politeness_weight(power_diff, social_dist, imposition)
    strategy = politeness_strategy(weight)
    register = recommended_register(direction, platform)

    audience_shape = inbound_act.get("audience_shape", "one_to_one")
    thread_position = inbound_act.get("thread_position", "replying")
    expected_response = inbound_act.get("expected_response", "fyi_only")
    sensitivity = inbound_act.get("sensitivity", "public")
    emotional_valence = inbound_act.get("emotional_valence", "neutral")
    time_pressure = inbound_act.get("time_pressure", "on_time")

    return {
        "register": register,
        "register_guidance": REGISTER_GUIDANCE.get(register, ""),
        "politeness_strategy": strategy,
        "politeness_guidance": STRATEGY_DESCRIPTIONS.get(strategy, ""),
        "politeness_weight": weight,
        "direction": direction,
        "direction_description": DIRECTION_DESCRIPTIONS.get(direction, ""),
        "power_description": _describe_power(power_diff),
        "distance_description": _describe_distance(social_dist),
        "audience_shape": audience_shape,
        "audience_guidance": AUDIENCE_SHAPES.get(audience_shape, ""),
        "thread_position": thread_position,
        "thread_guidance": THREAD_POSITIONS.get(thread_position, ""),
        "expected_response": expected_response,
        "response_guidance": EXPECTED_RESPONSES.get(expected_response, ""),
        "sensitivity": sensitivity,
        "sensitivity_guidance": SENSITIVITY_LEVELS.get(sensitivity, ""),
        "emotional_valence": emotional_valence,
        "valence_guidance": EMOTIONAL_VALENCES.get(emotional_valence, ""),
        "time_pressure": time_pressure,
        "time_pressure_guidance": TIME_PRESSURES.get(time_pressure, ""),
    }
