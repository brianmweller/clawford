"""Tests for agents/shared/confirm_pending_tool.py — the shared
text-approval tool. Mirrors the dispatcher's _handle_confirm path but
is callable from the LLM when the operator gives natural-language approval.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


@pytest.fixture
def pa_and_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    for mod in list(sys.modules):
        if mod in ("pending_actions", "confirm_pending_tool"):
            del sys.modules[mod]
    import pending_actions
    import confirm_pending_tool
    (tmp_path / "shopping-workspace").mkdir()
    return pending_actions, confirm_pending_tool


def test_schema_shape(pa_and_tool):
    _, tool = pa_and_tool
    schema = tool.TOOL_SCHEMA
    assert schema["name"] == "confirm_pending"
    params = schema["parameters"]
    assert set(params["required"]) == {"action_id", "reason"}
    props = params["properties"]
    assert "action_id" in props and "reason" in props


def test_confirm_pending_runs_executor_and_removes_action(pa_and_tool):
    pa, tool = pa_and_tool
    staged = pa.stage("shopping", "reorder", {"source": "costco"},
                      "Reorder Kirkland Water",
                      confirm_label="OK", cancel_label="No")
    action_id = staged["action_id"]

    captured = {}
    def fake_executor(**kw):
        captured["kw"] = kw
        return {"status": "ok", "item": "Kirkland"}

    out = tool.confirm_pending(
        action_id=action_id,
        reason='the operator said "do it"',
        agent_id="shopping",
        executors={"confirm_reorder": fake_executor},
    )

    assert out["status"] == "ok"
    assert out["action_id"] == action_id
    assert out["summary"] == "Reorder Kirkland Water"
    assert out["reason"] == 'the operator said "do it"'
    assert out["item"] == "Kirkland"
    assert captured["kw"] == {"source": "costco"}
    # action removed
    assert pa.load_by_id("shopping", action_id) is None


def test_confirm_pending_unknown_action_id(pa_and_tool):
    _, tool = pa_and_tool
    out = tool.confirm_pending(
        action_id="act_doesnotexist",
        reason="the operator approved",
        agent_id="shopping",
        executors={},
    )
    assert out["status"] == "error"
    assert "act_doesnotexist" in out["error"]


def test_confirm_pending_executor_raises(pa_and_tool):
    pa, tool = pa_and_tool
    staged = pa.stage("shopping", "reorder", {"x": 1}, "Boom",
                      confirm_label="OK", cancel_label="No")
    action_id = staged["action_id"]

    def bad(**kw):
        raise RuntimeError("upstream broke")

    out = tool.confirm_pending(
        action_id=action_id, reason="approved",
        agent_id="shopping",
        executors={"confirm_reorder": bad},
    )
    assert out["status"] == "error"
    assert "upstream broke" in out["error"]
    # On exception, action is preserved so retries are possible.
    assert pa.load_by_id("shopping", action_id) is not None


def test_confirm_pending_missing_executor(pa_and_tool):
    pa, tool = pa_and_tool
    staged = pa.stage("shopping", "weird_kind", {"x": 1}, "What",
                      confirm_label="OK", cancel_label="No")
    action_id = staged["action_id"]

    out = tool.confirm_pending(
        action_id=action_id, reason="approved",
        agent_id="shopping", executors={},
    )
    assert out["status"] == "error"
    assert "weird_kind" in out["error"]
