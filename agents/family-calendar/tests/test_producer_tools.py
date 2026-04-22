"""Tests for Mistress Mouse's Phase C producer tools."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(AGENT_DIR))


@pytest.fixture
def tools_mod(tmp_path, monkeypatch):
    workspace = tmp_path / "family-calendar-workspace"
    workspace.mkdir()
    (workspace / "scripts").mkdir()
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    for mod in list(sys.modules):
        if mod in ("tools", "pending_actions"):
            del sys.modules[mod]
    import tools
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "GCAL_WRITE_SCRIPT", str(workspace / "scripts" / "gcal-write.py"))
    return tools


def test_propose_event_add_stages_action(tools_mod):
    result = tools_mod.propose_event_add(
        calendar_id="family", summary="Dentist",
        start="2026-04-20T14:00", end="2026-04-20T15:00",
    )
    assert "__pending_action__" in result
    assert "Dentist" in result["summary"]

    import pending_actions
    actions = pending_actions.load("family-calendar")
    assert len(actions) == 1
    assert actions[0]["kind"] == "calendar_add"
    assert actions[0]["payload"]["summary"] == "Dentist"


def test_propose_event_move_stages_action(tools_mod, monkeypatch):
    """Fuzzy resolver: an event_id-shaped descriptor passes through
    id_pattern. The candidate must live in the pool so the tool can
    extract source_calendar_id from matched.extras."""
    from fuzzy_resolver import Candidate
    fake_pool = [Candidate(
        id="evlongenough01",
        display="Dentist",
        subject="Dentist",
        extras={"id": "evlongenough01",
                "summary": "Dentist",
                "source_calendar_id": "family"},
    )]
    monkeypatch.setattr(tools_mod, "_load_event_candidates", lambda: fake_pool)

    result = tools_mod.propose_event_move(
        event="evlongenough01", new_start="2026-04-21T15:00",
    )
    assert "__pending_action__" in result
    import pending_actions
    action = pending_actions.load_by_id("family-calendar", result["action_id"])
    assert action["kind"] == "calendar_move"
    assert action["payload"]["calendar_id"] == "family"
    assert action["payload"]["event_id"] == "evlongenough01"


def test_propose_event_cancel_stages_action(tools_mod, monkeypatch):
    from fuzzy_resolver import Candidate
    fake_pool = [Candidate(
        id="evcancellable2",
        display="Standup",
        subject="Standup",
        extras={"id": "evcancellable2",
                "summary": "Standup",
                "source_calendar_id": "family"},
    )]
    monkeypatch.setattr(tools_mod, "_load_event_candidates", lambda: fake_pool)

    result = tools_mod.propose_event_cancel(event="evcancellable2")
    assert "__pending_action__" in result
    import pending_actions
    action = pending_actions.load_by_id("family-calendar", result["action_id"])
    assert action["kind"] == "calendar_cancel"
    assert action["payload"]["calendar_id"] == "family"
    assert action["payload"]["event_id"] == "evcancellable2"


def test_propose_event_move_by_fuzzy_title(tools_mod, monkeypatch):
    """A title-substring descriptor ('dentist') resolves via the fuzzy
    resolver to the matching event + its calendar."""
    from fuzzy_resolver import Candidate
    fake_pool = [Candidate(
        id="evdentist01234",
        display="Dentist",
        subject="Dentist appointment",
        extras={"id": "evdentist01234",
                "summary": "Dentist appointment",
                "source_calendar_id": "family"},
    )]
    monkeypatch.setattr(tools_mod, "_load_event_candidates", lambda: fake_pool)

    result = tools_mod.propose_event_move(
        event="dentist", new_start="2026-04-21T16:00",
    )
    import pending_actions
    action = pending_actions.load_by_id("family-calendar", result["action_id"])
    assert action["payload"]["event_id"] == "evdentist01234"


def test_propose_event_move_returns_not_found_on_miss(tools_mod, monkeypatch):
    """Fuzzy that matches nothing returns status=not_found without
    staging a pending action. Use a short descriptor that won't trip
    the id_pattern passthrough (< 10 alphanumeric chars)."""
    monkeypatch.setattr(tools_mod, "_load_event_candidates", lambda: [])
    result = tools_mod.propose_event_move(
        event="nonesuch", new_start="2026-04-21T16:00",
    )
    assert result["status"] == "not_found"
    assert "__pending_action__" not in result


def test_confirm_calendar_add_calls_subprocess(tools_mod, monkeypatch):
    mock_run = MagicMock(return_value={"status": "ok", "action": "created"})
    monkeypatch.setattr(tools_mod, "run_json_script", mock_run)

    result = tools_mod.confirm_calendar_add(
        calendar_id="family", summary="Dentist",
        start="2026-04-20T14:00",
    )
    mock_run.assert_called_once()
    args = mock_run.call_args[0]
    assert "create" in args
    assert "--confirm" in args
    assert result["status"] == "ok"


def test_confirm_calendar_move_calls_subprocess(tools_mod, monkeypatch):
    mock_run = MagicMock(return_value={"status": "ok", "action": "moved"})
    monkeypatch.setattr(tools_mod, "run_json_script", mock_run)

    result = tools_mod.confirm_calendar_move(
        calendar_id="family", event_id="ev1",
        new_start="2026-04-21T15:00",
    )
    args = mock_run.call_args[0]
    assert "move" in args
    assert "--confirm" in args


def test_confirm_calendar_cancel_calls_subprocess(tools_mod, monkeypatch):
    mock_run = MagicMock(return_value={"status": "ok", "action": "removed"})
    monkeypatch.setattr(tools_mod, "run_json_script", mock_run)

    result = tools_mod.confirm_calendar_cancel(
        calendar_id="family", event_id="ev2",
    )
    args = mock_run.call_args[0]
    assert "remove" in args
    assert "--confirm" in args


def test_confirm_executors_not_in_manifest(tools_mod):
    tool_names = {t["name"] for t in tools_mod.TOOLS}
    assert "confirm_calendar_add" not in tool_names
    assert "confirm_calendar_move" not in tool_names
    assert "confirm_calendar_cancel" not in tool_names
    assert "confirm_calendar_add" in tools_mod.EXECUTORS
    assert "confirm_calendar_move" in tools_mod.EXECUTORS
    assert "confirm_calendar_cancel" in tools_mod.EXECUTORS
