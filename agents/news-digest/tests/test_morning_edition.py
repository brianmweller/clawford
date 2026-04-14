"""Tests for news-digest/scripts/morning-edition.py.

The Phase-3b replacement for the OpenClaw morning-edition LLM cron.
Orchestrates: read cache/ranked-<today>.json → call llm.infer(json_mode)
→ parse the returned JSON array → write cache/morning-items.json.

Pure Python + one llm.infer() call. No direct Telegram delivery (that's
morning-fleet-deliver.py, which fires separately at 12:00 UTC).

These tests monkeypatch agents.shared.llm.infer so no real network
calls happen. They use tmp_path for workspace isolation.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.shared.llm import InferResult

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_morning_edition():
    # feedparser stub so the fetch-and-rank side-imports don't explode
    # on a bare venv.
    import types
    for name in ("feedparser",):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    path = SCRIPTS_DIR / "morning-edition.py"
    spec = importlib.util.spec_from_file_location("morning_edition", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_morning_edition()


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    """Redirect the module's WORKSPACE + CACHE_DIR to an isolated tmp
    dir so tests never touch ~/.openclaw/ on the host."""
    ws = tmp_path / "news-digest-workspace"
    cache = ws / "cache"
    cache.mkdir(parents=True)

    me = _load_morning_edition()
    monkeypatch.setattr(me, "WORKSPACE", ws)
    monkeypatch.setattr(me, "CACHE_DIR", cache)
    return me, ws, cache


def _write_ranked(cache: Path, articles: list[dict]) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = cache / f"ranked-{today}.json"
    path.write_text(json.dumps({"articles": articles}), encoding="utf-8")
    return path


SAMPLE_ARTICLES = [
    {
        "id": "a1",
        "title": "OpenAI ships GPT-5.5",
        "summary": "New model, better reasoning",
        "link": "https://reuters.example.com/a1",
        "source": "reuters",
        "source_label": "Reuters",
        "topics": ["ai", "tech"],
        "rank": 0.95,
    },
    {
        "id": "a2",
        "title": "Fed raises rates 25bp",
        "summary": "Inflation still above target",
        "link": "https://wsj.example.com/a2",
        "source": "wsj",
        "source_label": "WSJ",
        "topics": ["economics", "markets"],
        "rank": 0.88,
    },
]

SAMPLE_LLM_OUTPUT = [
    {
        "num": 1,
        "category": "🤖 AI & Tech",
        "extended_headline": "OpenAI's GPT-5.5 launch hints at next-gen reasoning gains.",
        "title": "OpenAI ships GPT-5.5",
        "url": "https://reuters.example.com/a1",
        "source_label": "Reuters",
        "topics": ["ai", "tech"],
    },
    {
        "num": 2,
        "category": "💰 Economics",
        "extended_headline": "Fed's 25bp hike signals inflation is not yet contained.",
        "title": "Fed raises rates 25bp",
        "url": "https://wsj.example.com/a2",
        "source_label": "WSJ",
        "topics": ["economics", "markets"],
    },
]


def _fake_infer_ok(items: list[dict]) -> InferResult:
    """Build a successful InferResult wrapping items in the
    `{"items": [...]}` envelope the LLM returns under json_mode=True.

    The OpenAI Responses API with text.format.type=json_object forces
    a JSON OBJECT return (not a bare array), so the prompt asks for
    `{"items": [...]}` and the parser extracts the array.
    """
    return InferResult(
        text=json.dumps({"items": items}),
        model="gpt-5.4",
        input_tokens=1500,
        output_tokens=400,
        total_tokens=1900,
    )


# ─── load_ranked_articles ────────────────────────────────────────────


def test_load_ranked_articles_reads_today_file(fake_workspace):
    me, ws, cache = fake_workspace
    _write_ranked(cache, SAMPLE_ARTICLES)

    articles, date_str = me.load_ranked_articles()

    assert len(articles) == 2
    assert articles[0]["id"] == "a1"
    assert date_str == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_load_ranked_articles_raises_when_file_missing(fake_workspace):
    me, ws, cache = fake_workspace
    with pytest.raises(FileNotFoundError):
        me.load_ranked_articles()


def test_load_ranked_articles_raises_when_empty(fake_workspace):
    me, ws, cache = fake_workspace
    _write_ranked(cache, [])  # zero articles
    with pytest.raises(RuntimeError, match="no articles"):
        me.load_ranked_articles()


# ─── build_prompt ────────────────────────────────────────────────────


def test_build_prompt_includes_ranked_article_titles(mod):
    prompt = mod.build_prompt(SAMPLE_ARTICLES)
    assert "OpenAI ships GPT-5.5" in prompt
    assert "Fed raises rates 25bp" in prompt


def test_build_prompt_mentions_category_labels(mod):
    """The prompt must tell the LLM which category labels to use."""
    prompt = mod.build_prompt(SAMPLE_ARTICLES)
    assert "🤖 AI & Tech" in prompt
    assert "💰 Economics" in prompt
    assert "🌍 World" in prompt
    assert "🏛️ US Policy" in prompt
    assert "🔗 LinkedIn" in prompt
    assert "📋 Also Noted" in prompt


def test_build_prompt_asks_for_items_object(mod):
    """Under json_mode=True the Responses API forces an object return,
    so the prompt asks for {"items": [...]} shape."""
    prompt = mod.build_prompt(SAMPLE_ARTICLES)
    assert '"items"' in prompt
    assert "JSON object" in prompt or "json object" in prompt.lower()


# ─── parse_items_response ───────────────────────────────────────────


def test_parse_items_response_extracts_items_from_wrapper_object(mod):
    """Under json_mode=True the LLM returns {"items": [...]} because
    the Responses API forces a JSON object. parse_items_response
    unwraps the `items` key."""
    text = json.dumps({"items": SAMPLE_LLM_OUTPUT})
    items = mod.parse_items_response(text)
    assert len(items) == 2
    assert items[0]["num"] == 1
    assert items[0]["category"] == "🤖 AI & Tech"


def test_parse_items_response_accepts_bare_array_as_fallback(mod):
    """If the model disregards json_mode and returns a bare array
    (possible when the prompt is explicit), parse_items_response
    should still handle it for forward compatibility."""
    text = json.dumps(SAMPLE_LLM_OUTPUT)
    items = mod.parse_items_response(text)
    assert len(items) == 2


def test_parse_items_response_unwraps_markdown_fence(mod):
    """Even with json_mode=True, models sometimes wrap in ```json…```."""
    fenced = "```json\n" + json.dumps({"items": SAMPLE_LLM_OUTPUT}) + "\n```"
    items = mod.parse_items_response(fenced)
    assert len(items) == 2


def test_parse_items_response_unwraps_bare_fence(mod):
    fenced = "```\n" + json.dumps({"items": SAMPLE_LLM_OUTPUT}) + "\n```"
    items = mod.parse_items_response(fenced)
    assert len(items) == 2


def test_parse_items_response_strips_leading_whitespace(mod):
    text = "\n\n  " + json.dumps({"items": SAMPLE_LLM_OUTPUT})
    items = mod.parse_items_response(text)
    assert len(items) == 2


def test_parse_items_response_raises_when_dict_has_no_items_key(mod):
    """A wrapper object that doesn't have 'items' is a contract
    violation — the prompt explicitly asks for that shape."""
    text = json.dumps({"results": SAMPLE_LLM_OUTPUT})
    with pytest.raises(ValueError, match="items"):
        mod.parse_items_response(text)


def test_parse_items_response_raises_when_items_is_not_list(mod):
    text = json.dumps({"items": {"not": "a list"}})
    with pytest.raises(ValueError, match="array|list"):
        mod.parse_items_response(text)


def test_parse_items_response_raises_on_invalid_json(mod):
    with pytest.raises(json.JSONDecodeError):
        mod.parse_items_response("this is not JSON at all")


# ─── write_morning_items ─────────────────────────────────────────────


def test_write_morning_items_creates_file(fake_workspace):
    me, ws, cache = fake_workspace
    path = me.write_morning_items(SAMPLE_LLM_OUTPUT, "2026-04-14")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert data[0]["num"] == 1


def test_write_morning_items_atomic_no_tmp_leftover(fake_workspace):
    me, ws, cache = fake_workspace
    me.write_morning_items(SAMPLE_LLM_OUTPUT, "2026-04-14")
    # Tmp file should have been os.replace'd away
    assert not (cache / "morning-items.json.tmp").exists()


def test_write_morning_items_overwrites_previous(fake_workspace):
    me, ws, cache = fake_workspace
    (cache / "morning-items.json").write_text("[]", encoding="utf-8")
    me.write_morning_items(SAMPLE_LLM_OUTPUT, "2026-04-14")
    data = json.loads((cache / "morning-items.json").read_text())
    assert len(data) == 2


# ─── run() orchestrator ─────────────────────────────────────────────


def test_run_happy_path_writes_items_and_returns_ok(fake_workspace):
    me, ws, cache = fake_workspace
    _write_ranked(cache, SAMPLE_ARTICLES)

    with patch("agents.shared.llm.infer", return_value=_fake_infer_ok(SAMPLE_LLM_OUTPUT)):
        result = me.run()

    assert result["status"] == "ok"
    assert result["items_count"] == 2
    assert result["ranked_article_count"] == 2
    assert (cache / "morning-items.json").exists()


def test_run_passes_json_mode_true_to_infer(fake_workspace):
    """json_mode=True constrains the codex backend output shape."""
    me, ws, cache = fake_workspace
    _write_ranked(cache, SAMPLE_ARTICLES)

    captured = {}
    def fake_infer(prompt, **kwargs):
        captured.update(kwargs)
        return _fake_infer_ok(SAMPLE_LLM_OUTPUT)

    with patch("agents.shared.llm.infer", side_effect=fake_infer):
        me.run()

    assert captured.get("json_mode") is True


def test_run_raises_on_infer_failure(fake_workspace):
    me, ws, cache = fake_workspace
    _write_ranked(cache, SAMPLE_ARTICLES)

    err = InferResult(text="", error="network down", returncode=500)
    with patch("agents.shared.llm.infer", return_value=err):
        with pytest.raises(RuntimeError, match="infer failed"):
            me.run()


def test_run_raises_on_empty_items_list(fake_workspace):
    me, ws, cache = fake_workspace
    _write_ranked(cache, SAMPLE_ARTICLES)

    with patch("agents.shared.llm.infer", return_value=_fake_infer_ok([])):
        with pytest.raises(RuntimeError, match="empty"):
            me.run()


def test_run_raises_on_missing_ranked_file(fake_workspace):
    me, ws, cache = fake_workspace
    # No ranked file written
    with patch("agents.shared.llm.infer", return_value=_fake_infer_ok(SAMPLE_LLM_OUTPUT)):
        with pytest.raises(FileNotFoundError):
            me.run()


# ─── main() SCRIPT_CONTRACT wrapper ─────────────────────────────────


def test_main_returns_zero_on_happy_path(fake_workspace, capsys):
    me, ws, cache = fake_workspace
    _write_ranked(cache, SAMPLE_ARTICLES)

    with patch("agents.shared.llm.infer", return_value=_fake_infer_ok(SAMPLE_LLM_OUTPUT)):
        rc = me.main()

    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0  # single line
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["items_count"] == 2


def test_main_returns_zero_on_infer_failure_with_error_json(fake_workspace, capsys):
    me, ws, cache = fake_workspace
    _write_ranked(cache, SAMPLE_ARTICLES)

    err = InferResult(text="", error="auth expired", returncode=401)
    with patch("agents.shared.llm.infer", return_value=err):
        rc = me.main()

    assert rc == 0  # SCRIPT_CONTRACT: always exit 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "auth expired" in payload["error"]
    assert "🐛" in payload["alert"]


def test_main_returns_zero_on_missing_ranked_file(fake_workspace, capsys):
    me, ws, cache = fake_workspace
    rc = me.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "ranked" in payload["error"].lower()


def test_main_imports_shared_llm_module():
    """Positive assertion: the script uses agents.shared.llm."""
    src = (SCRIPTS_DIR / "morning-edition.py").read_text(encoding="utf-8")
    assert (
        "agents.shared.llm" in src
        or "from agents.shared import llm" in src
    )
