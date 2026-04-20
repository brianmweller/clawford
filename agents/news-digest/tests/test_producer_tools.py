"""Tests for Lowly Worm's Phase C producer tools."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(AGENT_DIR))


@pytest.fixture
def tools_mod(tmp_path, monkeypatch):
    workspace = tmp_path / "news-digest-workspace"
    workspace.mkdir()
    prefs = workspace / "preferences"
    prefs.mkdir()
    cache = workspace / "cache"
    cache.mkdir()
    for mod in list(sys.modules):
        if mod in ("tools",):
            del sys.modules[mod]
    import tools
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "PREFS", str(prefs))
    monkeypatch.setattr(tools, "CACHE", str(cache))
    monkeypatch.setattr(tools, "ENGAGEMENT_PATH", str(prefs / "engagement.jsonl"))
    monkeypatch.setattr(tools, "MORNING_ITEMS_PATH", str(cache / "morning-items.json"))
    return tools


def test_record_engagement_appends_to_jsonl(tools_mod):
    result = tools_mod.record_engagement("3", "thumbs_up")
    assert result["status"] == "ok"
    assert result["article_id"] == "3"
    assert result["action"] == "thumbs_up"

    with open(tools_mod.ENGAGEMENT_PATH, encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["action"] == "thumbs_up"
    assert lines[0]["article_id"] == "3"


def test_record_engagement_enriches_from_cache(tools_mod):
    items = [
        {"title": "Article 1", "topics": ["tech"], "source": "TechCrunch"},
        {"title": "Article 2", "topics": ["ai"], "source": "HN"},
        {"title": "Article 3", "topics": ["biz"], "source": "WSJ"},
    ]
    with open(tools_mod.MORNING_ITEMS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f)

    tools_mod.record_engagement("2", "thumbs_down")

    with open(tools_mod.ENGAGEMENT_PATH, encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["title"] == "Article 2"
    assert entry["topics"] == ["ai"]


def test_record_engagement_invalid_action(tools_mod):
    result = tools_mod.record_engagement("1", "love")
    assert result["status"] == "error"


def test_record_engagement_multiple_appends(tools_mod):
    tools_mod.record_engagement("1", "thumbs_up")
    tools_mod.record_engagement("2", "thumbs_down")
    tools_mod.record_engagement("3", "more")

    with open(tools_mod.ENGAGEMENT_PATH, encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    assert len(lines) == 3


def test_record_engagement_in_executors(tools_mod):
    assert "record_engagement" in tools_mod.EXECUTORS


# ---------------------------------------------------------------------------
# ask_topic — /ask [topic] shells out to on-demand.py
# ---------------------------------------------------------------------------


def test_ask_topic_invokes_on_demand_script(tools_mod, monkeypatch):
    captured = {}

    def fake_run(script_path, *args, **kwargs):
        captured["script"] = script_path
        captured["args"] = list(args)
        return {
            "status": "ok",
            "query": "chip export controls",
            "results": [{"title": "NYT: ...", "url": "..."}],
        }

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error", lambda r: False)

    result = tools_mod.ask_topic("chip export controls")
    assert result["status"] == "ok"
    assert captured["script"].endswith("on-demand.py")
    # Topic is passed as a positional arg, not a flag
    assert "chip export controls" in captured["args"]


def test_ask_topic_empty_input_is_error(tools_mod):
    result = tools_mod.ask_topic("   ")
    assert result["status"] == "error"


def test_ask_topic_surfaces_script_error(tools_mod, monkeypatch):
    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(
        subprocess_helpers, "run_json_script",
        lambda *a, **k: {"__error__": "feed unreachable"},
    )
    monkeypatch.setattr(
        subprocess_helpers, "is_subprocess_error", lambda r: "__error__" in r,
    )
    result = tools_mod.ask_topic("chip export controls")
    assert result["status"] == "error"
    assert "feed" in result["error"]


def test_ask_topic_in_executors(tools_mod):
    assert "ask_topic" in tools_mod.EXECUTORS
