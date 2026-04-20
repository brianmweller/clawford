"""Pure helpers for classifying an inbound email's pragmatic dimensions.

classify_inbound_act() is Huckle's LLM pre-pass that extracts the eight
communication-act fields (intent, imposition, audience_shape,
thread_position, expected_response, sensitivity, emotional_valence,
time_pressure) from an incoming email BEFORE voice calibration runs.
Replaces the hardcoded defaults that draft-compose.py previously shipped.

Pure functions only here; the actual LLM call is injected by the caller.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from inbound_act_lib import (  # type: ignore
    build_act_classifier_prompt,
    classify_inbound_act,
    infer_structural_act,
    merge_act_defaults,
    parse_act_response,
)


# --- infer_structural_act: mechanical fields from headers/dates ---

def test_audience_shape_one_to_one():
    inbound = {"to": ["sam.smith@example.com"], "cc": []}
    assert infer_structural_act(inbound)["audience_shape"] == "one_to_one"


def test_audience_shape_one_to_few():
    inbound = {
        "to": ["sam.smith@example.com", "alex.rivera@example.com"],
        "cc": ["josh@ex.com"],
    }
    assert infer_structural_act(inbound)["audience_shape"] == "one_to_few"


def test_audience_shape_one_to_many():
    inbound = {
        "to": [f"u{i}@ex.com" for i in range(8)],
        "cc": [],
    }
    assert infer_structural_act(inbound)["audience_shape"] == "one_to_many"


def test_thread_position_always_replying_when_history_present():
    inbound = {"to": ["operator@ex.com"]}
    history = [{"from": "the operator", "body": "hi", "date": "2026-04-01T00:00:00Z"}]
    assert infer_structural_act(inbound, history=history)["thread_position"] == "replying"


def test_thread_position_originating_when_no_history():
    inbound = {"to": ["operator@ex.com"]}
    assert infer_structural_act(inbound, history=[])["thread_position"] == "originating"


def test_time_pressure_on_time_when_recent():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    inbound = {
        "to": ["operator@ex.com"],
        "received_at": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    }
    assert infer_structural_act(inbound, now=now)["time_pressure"] == "on_time"


def test_time_pressure_late_reply_when_older_than_72h():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    inbound = {
        "to": ["operator@ex.com"],
        "received_at": (now - timedelta(days=5)).isoformat().replace("+00:00", "Z"),
    }
    assert infer_structural_act(inbound, now=now)["time_pressure"] == "late_reply"


# --- build_act_classifier_prompt: LLM prompt builder ---

def test_classifier_prompt_includes_inbound_body():
    inbound = {"from_name": "Josh", "subject": "Re: Hi", "body": "Can you help with X?"}
    prompt = build_act_classifier_prompt(inbound, history=[])
    assert "Can you help with X?" in prompt
    assert "Re: Hi" in prompt


def test_classifier_prompt_requests_all_subjective_fields():
    prompt = build_act_classifier_prompt({"subject": "s", "body": "b"}, history=[])
    for field in ("intent", "imposition", "expected_response", "sensitivity", "emotional_valence"):
        assert field in prompt


def test_classifier_prompt_documents_valid_values():
    prompt = build_act_classifier_prompt({"subject": "s", "body": "b"}, history=[])
    # At minimum, the prompt must enumerate the known intent types so LLM
    # doesn't invent its own
    assert "request" in prompt
    assert "thank" in prompt
    assert "decline" in prompt


# --- parse_act_response ---

def test_parse_valid_response():
    raw = json.dumps({
        "intent": "request",
        "imposition": 0.6,
        "expected_response": "action_required",
        "sensitivity": "public",
        "emotional_valence": "neutral",
    })
    out = parse_act_response(raw)
    assert "error" not in out
    assert out["intent"] == "request"
    assert out["imposition"] == 0.6


def test_parse_strips_markdown_fences():
    raw = "```json\n" + json.dumps({
        "intent": "thank", "imposition": 0.1,
        "expected_response": "fyi_only", "sensitivity": "public",
        "emotional_valence": "positive",
    }) + "\n```"
    out = parse_act_response(raw)
    assert out.get("intent") == "thank"


def test_parse_clamps_imposition_to_unit_interval():
    raw = json.dumps({
        "intent": "request", "imposition": 2.5,
        "expected_response": "action_required", "sensitivity": "public",
        "emotional_valence": "neutral",
    })
    out = parse_act_response(raw)
    assert 0.0 <= out["imposition"] <= 1.0


def test_parse_malformed_returns_error():
    assert "error" in parse_act_response("not json")


def test_parse_unknown_intent_returns_error():
    raw = json.dumps({
        "intent": "made-up-thing", "imposition": 0.3,
        "expected_response": "fyi_only", "sensitivity": "public",
        "emotional_valence": "neutral",
    })
    out = parse_act_response(raw)
    assert "error" in out


# --- merge_act_defaults: fill missing fields with sensible defaults ---

def test_merge_fills_all_required_fields():
    merged = merge_act_defaults({"intent": "thank"})
    for key in [
        "intent", "imposition", "audience_shape", "thread_position",
        "expected_response", "sensitivity", "emotional_valence", "time_pressure",
    ]:
        assert key in merged


def test_merge_preserves_explicit_values():
    merged = merge_act_defaults({"intent": "request", "imposition": 0.7})
    assert merged["intent"] == "request"
    assert merged["imposition"] == 0.7


# --- classify_inbound_act: end-to-end orchestration ---

def test_classify_uses_injected_llm_and_merges_structural():
    inbound = {
        "from_name": "Josh", "subject": "Can we talk?", "body": "Need advice.",
        "to": ["operator@ex.com"], "cc": [],
    }

    def fake_llm(prompt: str) -> str:
        return json.dumps({
            "intent": "request",
            "imposition": 0.5,
            "expected_response": "open_discussion",
            "sensitivity": "personal",
            "emotional_valence": "neutral",
        })

    act = classify_inbound_act(inbound, history=[], llm_fn=fake_llm)
    assert act["intent"] == "request"
    assert act["audience_shape"] == "one_to_one"   # structural
    assert act["thread_position"] == "originating"  # structural (no history)
    assert "time_pressure" in act


def test_classify_falls_back_to_defaults_on_llm_failure():
    def failing_llm(prompt: str) -> str:
        return json.dumps({"error": "network down"})

    act = classify_inbound_act(
        {"to": ["x@ex.com"], "subject": "s", "body": "b"},
        history=[], llm_fn=failing_llm,
    )
    # Must still produce a complete act dict so compose doesn't KeyError
    assert "intent" in act
    assert "imposition" in act
