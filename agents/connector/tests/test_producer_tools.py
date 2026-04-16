"""Tests for Huckle Cat's Phase C producer tools."""
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
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir()
    for mod in list(sys.modules):
        if mod in ("tools",):
            del sys.modules[mod]
    import tools
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "CHECKIN_LOG_PATH", str(workspace / "checkin-log.json"))
    monkeypatch.setattr(tools, "CONFIG_PATH", str(workspace / "connector-config.json"))
    return tools


def test_mark_checkin_logs_contact(tools_mod):
    result = tools_mod.mark_checkin("John Smith")
    assert result["status"] == "ok"
    assert result["person"] == "John Smith"

    with open(tools_mod.CHECKIN_LOG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["checkins"]) == 1
    assert data["checkins"][0]["person"] == "John Smith"
    assert data["checkins"][0]["source"] == "manual"


def test_mark_checkin_appends_multiple(tools_mod):
    tools_mod.mark_checkin("Alice")
    tools_mod.mark_checkin("Bob")
    tools_mod.mark_checkin("Carol")

    with open(tools_mod.CHECKIN_LOG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["checkins"]) == 3


def test_snooze_reminder(tools_mod):
    result = tools_mod.snooze_reminder("Sarah", days=14)
    assert result["status"] == "ok"
    assert result["person"] == "Sarah"
    assert result["snoozed_for_days"] == 14

    with open(tools_mod.CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    assert "sarah" in config["snoozes"]
    assert "until" in config["snoozes"]["sarah"]


def test_snooze_default_7_days(tools_mod):
    result = tools_mod.snooze_reminder("Tom")
    assert result["snoozed_for_days"] == 7


def test_mark_checkin_and_snooze_in_executors(tools_mod):
    assert "mark_checkin" in tools_mod.EXECUTORS
    assert "snooze_reminder" in tools_mod.EXECUTORS
