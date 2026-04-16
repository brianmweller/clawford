"""Tests for Sergeant Murphy's Phase C producer tools."""
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
    workspace = tmp_path / "meetings-coach-workspace"
    workspace.mkdir()
    cache = workspace / "cache"
    cache.mkdir()
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    for mod in list(sys.modules):
        if mod in ("tools",):
            del sys.modules[mod]
    import tools
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "CACHE", str(cache))
    return tools


def _write_debrief(cache_dir, event_id, action_items):
    data = {
        "event_id": event_id,
        "meeting_title": f"Meeting {event_id}",
        "meeting_start": "2026-04-15T10:00:00-07:00",
        "krisp_action_items": action_items,
        "status": "pending_review",
    }
    path = cache_dir / f"pending-debrief-{event_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_list_pending_action_items_empty(tools_mod):
    result = tools_mod.list_pending_action_items()
    assert result["count"] == 0
    assert result["items"] == []


def test_list_pending_action_items(tools_mod, tmp_path):
    cache = Path(tools_mod.CACHE)
    _write_debrief(cache, "ev1", ["Follow up with Yesol", "Draft roadmap"])
    _write_debrief(cache, "ev2", ["Send meeting notes"])

    result = tools_mod.list_pending_action_items()
    assert result["count"] == 3
    ids = [i["item_id"] for i in result["items"]]
    assert "ev1:0" in ids
    assert "ev1:1" in ids
    assert "ev2:0" in ids


def test_confirm_action_item(tools_mod, tmp_path):
    cache = Path(tools_mod.CACHE)
    _write_debrief(cache, "ev1", ["Follow up with Yesol", "Draft roadmap"])

    result = tools_mod.confirm_action_item("ev1:0")
    assert result["status"] == "ok"
    assert result["action_item"] == "Follow up with Yesol"

    # Verify persisted
    with open(cache / "pending-debrief-ev1.json", encoding="utf-8") as f:
        data = json.load(f)
    assert 0 in data["accepted_items"]


def test_confirm_action_item_idempotent(tools_mod, tmp_path):
    cache = Path(tools_mod.CACHE)
    _write_debrief(cache, "ev1", ["Task"])

    tools_mod.confirm_action_item("ev1:0")
    tools_mod.confirm_action_item("ev1:0")

    with open(cache / "pending-debrief-ev1.json", encoding="utf-8") as f:
        data = json.load(f)
    assert data["accepted_items"].count(0) == 1


def test_dismiss_action_item(tools_mod, tmp_path):
    cache = Path(tools_mod.CACHE)
    _write_debrief(cache, "ev1", ["Not relevant"])

    result = tools_mod.dismiss_action_item("ev1:0")
    assert result["status"] == "ok"
    assert result["dismissed"] is True

    with open(cache / "pending-debrief-ev1.json", encoding="utf-8") as f:
        data = json.load(f)
    assert 0 in data["dismissed_items"]


def test_confirm_invalid_item_id(tools_mod):
    result = tools_mod.confirm_action_item("bad_id")
    assert result["status"] == "error"


def test_confirm_missing_debrief(tools_mod):
    result = tools_mod.confirm_action_item("nonexistent:0")
    assert result["status"] == "error"
