"""Tests for update-preferences.py::call_judge_llm.

Previously the judge used `from openai import OpenAI` + an OPENAI_API_KEY
env var, which violates feedback_no_api_keys_ever. The function silently
caught all exceptions and returned None, so when the key wasn't set (and
it wasn't), every judge call was a no-op for 8 days — only the coarse
topic/source weight nudges happened, no LLM-driven subtopic enrichment.

The fixed implementation calls `openclaw infer model run --prompt … --json`
(the same pattern used by `_summarize_linkedin_thread` in fetch-and-rank.py).
These tests pin the contract:

  - subprocess command shape
  - happy-path JSON parse from openclaw outputs[0].text
  - error returns None (caller falls back to broad-only updates)
  - markdown-fenced JSON in the model output is unwrapped
  - missing openclaw CLI returns None
  - subprocess timeout returns None

We do NOT actually call openclaw — every test mocks subprocess.run.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


def _fake_openclaw_response(text: str) -> MagicMock:
    """Build a fake subprocess result mimicking `openclaw infer --json` output."""
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = json.dumps({
        "ok": True,
        "capability": "model.run",
        "transport": "local",
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "outputs": [{"text": text, "mediaUrl": None}],
    })
    proc.stderr = ""
    return proc


def test_judge_returns_parsed_dict_on_happy_path(mod):
    """Happy path: openclaw returns JSON with the judge's structured response."""
    judge_json = {
        "reason": "LOW_QUALITY",
        "subtopics": ["clickbait_headline", "ad_copy"],
        "quality_signal": -1,
        "explanation": "headline overpromises and underdelivers",
    }
    fake = _fake_openclaw_response(json.dumps(judge_json))
    with patch.object(mod.subprocess, "run", return_value=fake):
        out = mod.call_judge_llm(
            title="Some clickbaity headline",
            summary="ad copy disguised as news",
            topics=["ai"],
            source="example.com",
            action="thumbs_down",
        )
    assert out == judge_json


def test_judge_unwraps_markdown_fenced_json(mod):
    """Models often wrap JSON in ```json … ``` fences. Helper must strip."""
    judge_json = {
        "reason": "GOOD_CONTENT",
        "subtopics": ["chip_supply_chain"],
        "quality_signal": 1,
        "explanation": "primary-source reporting on TSMC",
    }
    fenced = "```json\n" + json.dumps(judge_json) + "\n```"
    fake = _fake_openclaw_response(fenced)
    with patch.object(mod.subprocess, "run", return_value=fake):
        out = mod.call_judge_llm(
            title="TSMC capacity report", summary="…", topics=["chips"],
            source="reuters.com", action="thumbs_up",
        )
    assert out == judge_json


def test_judge_calls_openclaw_with_correct_command_shape(mod):
    """The helper MUST shell out to `openclaw infer model run --prompt … --json`,
    NOT to `openai`/`anthropic`/`claude` and never with raw API keys."""
    fake = _fake_openclaw_response(json.dumps({
        "reason": "GOOD_CONTENT", "subtopics": [], "quality_signal": 1,
        "explanation": "ok",
    }))
    with patch.object(mod.subprocess, "run", return_value=fake) as mock_run:
        mod.call_judge_llm(
            title="t", summary="s", topics=["ai"],
            source="src", action="thumbs_up",
        )
    assert mock_run.called
    args = mock_run.call_args.args[0]
    assert args[0] == "openclaw"
    assert "infer" in args
    assert "model" in args
    assert "run" in args
    assert "--prompt" in args
    assert "--json" in args
    # No API key environment variable should be required by the helper.
    # (We rely on Sam's openclaw codex auth, not raw keys.)


def test_judge_returns_none_on_subprocess_failure(mod):
    """Non-zero exit → return None so caller falls back to coarse updates."""
    fake = MagicMock()
    fake.returncode = 1
    fake.stdout = ""
    fake.stderr = "auth refused"
    with patch.object(mod.subprocess, "run", return_value=fake):
        out = mod.call_judge_llm(
            title="t", summary="s", topics=["ai"],
            source="src", action="thumbs_up",
        )
    assert out is None


def test_judge_returns_none_on_missing_openclaw_cli(mod):
    """If openclaw is not on PATH, FileNotFoundError → return None."""
    with patch.object(mod.subprocess, "run", side_effect=FileNotFoundError):
        out = mod.call_judge_llm(
            title="t", summary="s", topics=["ai"],
            source="src", action="thumbs_up",
        )
    assert out is None


def test_judge_returns_none_on_timeout(mod):
    """Slow openclaw call → TimeoutExpired → return None."""
    with patch.object(
        mod.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=30),
    ):
        out = mod.call_judge_llm(
            title="t", summary="s", topics=["ai"],
            source="src", action="thumbs_up",
        )
    assert out is None


def test_judge_returns_none_when_outputs_text_is_garbage(mod):
    """Model returns non-JSON text → return None (don't crash)."""
    fake = _fake_openclaw_response("here's a thoughtful explanation but no JSON")
    with patch.object(mod.subprocess, "run", return_value=fake):
        out = mod.call_judge_llm(
            title="t", summary="s", topics=["ai"],
            source="src", action="thumbs_down",
        )
    assert out is None


def test_judge_does_not_import_openai_module(mod):
    """The fixed implementation must not import the `openai` package.
    Importing openai means OPENAI_API_KEY would be required at runtime."""
    src = (SCRIPTS_DIR / "update-preferences.py").read_text(encoding="utf-8")
    assert "from openai import" not in src, (
        "update-preferences.py must not import the openai package — "
        "use openclaw infer model run instead (no API keys ever)."
    )
    assert "OPENAI_API_KEY" not in src, (
        "update-preferences.py must not reference OPENAI_API_KEY."
    )
