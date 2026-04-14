"""Tests for update-preferences.py::call_judge_llm.

Post-Phase-3a, the judge helper calls agents.shared.llm.infer instead
of shelling out to `openclaw infer model run --prompt … --json`. The
test monkeypatches agents.shared.llm.infer and builds an InferResult
to pin the new contract.

These tests also verify the markdown-fence unwrap logic (models often
wrap JSON in ```json … ``` fences) stays intact across the migration,
and that transport-level failure modes (auth failed, timeout) return
None so the caller falls back to coarse multiplicative weight updates.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.shared.llm import InferResult

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_update_prefs():
    path = SCRIPTS_DIR / "update-preferences.py"
    spec = importlib.util.spec_from_file_location("update_preferences", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_update_prefs()


def _fake_infer_ok(text: str) -> InferResult:
    return InferResult(
        text=text,
        model="gpt-5.4",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
    )


def test_judge_returns_parsed_dict_on_happy_path(mod):
    """Happy path: infer returns a JSON string; helper parses it into a dict."""
    judge_json = {
        "reason": "LOW_QUALITY",
        "subtopics": ["clickbait_headline", "ad_copy"],
        "quality_signal": -1,
        "explanation": "headline overpromises and underdelivers",
    }
    with patch(
        "agents.shared.llm.infer",
        return_value=_fake_infer_ok(json.dumps(judge_json)),
    ):
        out = mod.call_judge_llm(
            title="Some clickbaity headline",
            summary="ad copy disguised as news",
            topics=["ai"],
            source="example.com",
            action="thumbs_down",
        )
    assert out == judge_json


def test_judge_unwraps_markdown_fenced_json(mod):
    """Models often wrap JSON in ```json … ``` fences even when asked
    for bare JSON. Helper must strip those before json.loads()."""
    judge_json = {
        "reason": "GOOD_CONTENT",
        "subtopics": ["chip_supply_chain"],
        "quality_signal": 1,
        "explanation": "primary-source reporting on TSMC",
    }
    fenced = "```json\n" + json.dumps(judge_json) + "\n```"
    with patch("agents.shared.llm.infer", return_value=_fake_infer_ok(fenced)):
        out = mod.call_judge_llm(
            title="TSMC capacity report",
            summary="…",
            topics=["chips"],
            source="reuters.com",
            action="thumbs_up",
        )
    assert out == judge_json


def test_judge_unwraps_plain_code_fence(mod):
    """Some models use bare ``` fences without the `json` language tag."""
    judge_json = {"reason": "STALE", "subtopics": [], "quality_signal": 0, "explanation": "old"}
    fenced = "```\n" + json.dumps(judge_json) + "\n```"
    with patch("agents.shared.llm.infer", return_value=_fake_infer_ok(fenced)):
        out = mod.call_judge_llm(
            title="t", summary="s", topics=["ai"], source="s", action="thumbs_down",
        )
    assert out == judge_json


def test_judge_passes_prompt_with_action_and_topics(mod):
    """The prompt passed to infer should encode the user's action + topics
    so the judge has enough context to choose a reason bucket."""
    captured = {}

    def fake_infer(prompt, **kwargs):
        captured["prompt"] = prompt
        return _fake_infer_ok(json.dumps({
            "reason": "GOOD_CONTENT", "subtopics": [], "quality_signal": 1,
            "explanation": "ok",
        }))

    with patch("agents.shared.llm.infer", side_effect=fake_infer):
        mod.call_judge_llm(
            title="Apple chip launch",
            summary="M5 details",
            topics=["tech", "chips"],
            source="bloomberg.com",
            action="thumbs_up",
        )
    prompt = captured["prompt"]
    assert "liked" in prompt  # action_word for thumbs_up
    assert "tech" in prompt
    assert "Apple chip launch" in prompt


def test_judge_returns_none_on_infer_error(mod):
    """Non-ok InferResult → return None so caller falls back to coarse updates."""
    err = InferResult(text="", error="auth refused", returncode=401)
    with patch("agents.shared.llm.infer", return_value=err):
        out = mod.call_judge_llm(
            title="t", summary="s", topics=["ai"], source="src", action="thumbs_up",
        )
    assert out is None


def test_judge_returns_none_on_empty_text(mod):
    with patch("agents.shared.llm.infer", return_value=_fake_infer_ok("")):
        out = mod.call_judge_llm(
            title="t", summary="s", topics=["ai"], source="src", action="thumbs_up",
        )
    assert out is None


def test_judge_returns_none_when_text_is_not_valid_json(mod):
    """Model returns thoughtful prose but no JSON → return None."""
    with patch(
        "agents.shared.llm.infer",
        return_value=_fake_infer_ok("here's a thoughtful explanation but no JSON"),
    ):
        out = mod.call_judge_llm(
            title="t", summary="s", topics=["ai"], source="src", action="thumbs_down",
        )
    assert out is None


def test_judge_uses_json_mode(mod):
    """The call should request json_mode=True so the model's text
    format is constrained to a JSON object."""
    captured = {}

    def fake_infer(prompt, **kwargs):
        captured.update(kwargs)
        return _fake_infer_ok(json.dumps({
            "reason": "GOOD_CONTENT", "subtopics": [], "quality_signal": 1,
            "explanation": "ok",
        }))

    with patch("agents.shared.llm.infer", side_effect=fake_infer):
        mod.call_judge_llm(
            title="t", summary="s", topics=["ai"], source="src", action="thumbs_up",
        )
    assert captured.get("json_mode") is True


def test_judge_script_imports_shared_llm_module():
    """Post-Phase-3a, the script must use agents.shared.llm."""
    src = (SCRIPTS_DIR / "update-preferences.py").read_text(encoding="utf-8")
    assert (
        "agents.shared.llm" in src
        or "from agents.shared import llm" in src
    ), "update-preferences.py must import from agents.shared.llm"


def test_judge_script_no_longer_shells_out_to_openclaw_infer():
    """The old subprocess.run(['openclaw', 'infer', …]) pattern must be gone."""
    src = (SCRIPTS_DIR / "update-preferences.py").read_text(encoding="utf-8")
    assert '"openclaw", "infer"' not in src
    assert "openclaw infer model run" not in src


def test_judge_does_not_import_openai_module():
    """Raw API keys must not return — the OpenAI SDK import is banned
    fleet-wide (memory: feedback_no_api_keys_ever.md)."""
    src = (SCRIPTS_DIR / "update-preferences.py").read_text(encoding="utf-8")
    assert "from openai import" not in src
    assert "OPENAI_API_KEY" not in src
