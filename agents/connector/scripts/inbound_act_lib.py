"""Classify an inbound email's pragmatic dimensions before voice calibration.

Replaces the hardcoded act dimensions draft-compose previously shipped.
Structural fields (audience_shape, thread_position, time_pressure) are
computed from headers + dates cheaply. Subjective fields (intent,
imposition, expected_response, sensitivity, emotional_valence) come from
one LLM call with a small JSON-only prompt.

Pure helpers — the LLM call is injected by the caller so tests can run
without network.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


VALID_INTENTS = {
    "inform", "request", "propose", "commit", "acknowledge", "persuade",
    "decline", "follow_up", "introduce", "thank", "escalate", "apologize",
    "congratulate",
}

VALID_EXPECTED_RESPONSE = {
    "action_required", "approval_needed", "fyi_only", "open_discussion",
}

VALID_SENSITIVITY = {"public", "internal", "confidential", "personal"}

VALID_VALENCE = {"positive", "negative", "neutral", "sensitive"}


_DEFAULT_ACT = {
    "intent": "inform",
    "imposition": 0.3,
    "audience_shape": "one_to_one",
    "thread_position": "replying",
    "expected_response": "fyi_only",
    "sensitivity": "public",
    "emotional_valence": "neutral",
    "time_pressure": "on_time",
}


# ---------------------------------------------------------------------------
# Structural inference — no LLM, no cost
# ---------------------------------------------------------------------------

def infer_structural_act(
    inbound: dict,
    history: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    to_count = len(inbound.get("to") or [])
    cc_count = len(inbound.get("cc") or [])
    total = to_count + cc_count
    if total <= 1:
        audience_shape = "one_to_one"
    elif total <= 5:
        audience_shape = "one_to_few"
    else:
        audience_shape = "one_to_many"

    thread_position = "replying" if history else "originating"

    time_pressure = "on_time"
    received_at = inbound.get("received_at")
    if received_at:
        try:
            rt = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            if rt.tzinfo is None:
                rt = rt.replace(tzinfo=timezone.utc)
            reference = now or datetime.now(timezone.utc)
            age_hours = (reference - rt).total_seconds() / 3600
            if age_hours > 72:
                time_pressure = "late_reply"
        except (ValueError, TypeError):
            pass

    return {
        "audience_shape": audience_shape,
        "thread_position": thread_position,
        "time_pressure": time_pressure,
    }


# ---------------------------------------------------------------------------
# LLM pre-pass — subjective fields
# ---------------------------------------------------------------------------

def build_act_classifier_prompt(inbound: dict, history: list[dict]) -> str:
    from_name = inbound.get("from_name", "")
    from_email = inbound.get("from_email", "")
    subject = inbound.get("subject", "")
    body = (inbound.get("body") or "").strip()[:2500]

    history_lines = []
    for m in (history or [])[-8:]:
        who = m.get("from", "?")
        snippet = (m.get("body") or "").strip()[:200].replace("\n", " ")
        history_lines.append(f"  {who}: {snippet}")
    history_block = "\n".join(history_lines) or "  (no prior messages)"

    return f"""Classify this inbound email's pragmatic dimensions. Output JSON only.

INBOUND
  From: {from_name} <{from_email}>
  Subject: {subject}
  Body:
{body}

PRIOR THREAD (for context)
{history_block}

OUTPUT JSON with EXACTLY these fields:
{{
  "intent": "<one of: {', '.join(sorted(VALID_INTENTS))}>",
  "imposition": <float 0.0-1.0: how much does a reply commit the operator to work — 0.0 = trivial ack, 1.0 = major request back on the sender>,
  "expected_response": "<one of: {', '.join(sorted(VALID_EXPECTED_RESPONSE))}>",
  "sensitivity": "<one of: {', '.join(sorted(VALID_SENSITIVITY))}>",
  "emotional_valence": "<one of: {', '.join(sorted(VALID_VALENCE))}>"
}}

Classify the INBOUND's pragmatic shape, not the reply. 'intent' = what
is this sender DOING with this message (requesting / thanking / informing
/ etc.). 'expected_response' = what is this sender expecting from the operator.
'sensitivity' / 'emotional_valence' = the tone of the inbound.
'imposition' = what the sender is asking the operator to do, if anything
(0.0 = nothing / pure FYI, 1.0 = major ask).
"""


def parse_act_response(llm_text: str) -> dict:
    cleaned = (llm_text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return {"error": "non-json", "raw": llm_text}

    if not isinstance(parsed, dict):
        return {"error": "non-object", "raw": llm_text}

    intent = parsed.get("intent")
    if intent not in VALID_INTENTS:
        return {"error": f"unknown intent {intent!r}", "raw": llm_text}

    try:
        imposition = float(parsed.get("imposition", 0.3))
    except (TypeError, ValueError):
        imposition = 0.3
    imposition = max(0.0, min(1.0, imposition))

    exp = parsed.get("expected_response")
    if exp not in VALID_EXPECTED_RESPONSE:
        exp = "fyi_only"

    sens = parsed.get("sensitivity")
    if sens not in VALID_SENSITIVITY:
        sens = "public"

    valence = parsed.get("emotional_valence")
    if valence not in VALID_VALENCE:
        valence = "neutral"

    return {
        "intent": intent,
        "imposition": imposition,
        "expected_response": exp,
        "sensitivity": sens,
        "emotional_valence": valence,
    }


def merge_act_defaults(partial: dict) -> dict:
    out = dict(_DEFAULT_ACT)
    for k, v in (partial or {}).items():
        if v is not None:
            out[k] = v
    return out


def classify_inbound_act(
    inbound: dict,
    history: list[dict],
    llm_fn=None,
    now: datetime | None = None,
) -> dict:
    """Full act classification: structural + LLM-subjective, merged with
    defaults as a floor. Never raises — LLM failures fall through to
    defaults for the subjective fields."""
    structural = infer_structural_act(inbound, history=history, now=now)

    subjective: dict = {}
    if llm_fn is not None:
        try:
            prompt = build_act_classifier_prompt(inbound, history)
            text = llm_fn(prompt)
            parsed = parse_act_response(text)
            if "error" not in parsed:
                subjective = parsed
        except Exception:   # noqa: BLE001
            subjective = {}

    combined = {**structural, **subjective}
    return merge_act_defaults(combined)
