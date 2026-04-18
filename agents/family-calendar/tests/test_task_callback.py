"""Tests for task-callback executor (done / snooze / ignore).

Wired from ``agents/shared/dispatcher.py``'s ``_handle_task_callback``
when the user taps a button on a task reminder Telegram message. This
module's executor mutates queue.md in place and clears the relevant
sent-reminders entry so snoozed tasks can fire a fresh T-30min ping.

Contextual snooze:
    Timed task (``due_at`` has a ``T``) → advance by 1 hour.
    All-day task (date-only)            → advance by 1 calendar day.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SHARED_DIR = REPO_ROOT / "agents" / "shared"
AGENT_DIR = REPO_ROOT / "agents" / "family-calendar"


@pytest.fixture
def tcb(monkeypatch, tmp_path):
    for p in (str(SHARED_DIR), str(AGENT_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))
    (tmp_path / "brain" / "tasks").mkdir(parents=True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("FAMILYCAL_WORKSPACE", str(ws))
    for mod in ("brain", "brain_tasks", "task_callback"):
        sys.modules.pop(mod, None)
    import task_callback  # type: ignore
    task_callback.WORKSPACE = str(ws)
    task_callback.SENT_REMINDERS_PATH = str(ws / "sent-reminders.json")
    return task_callback


def _seed(tid, *, description="thing", status="open", due_at=None):
    import brain_tasks  # type: ignore
    brain_tasks.append_task(
        brain_tasks.Task(
            id=tid,
            description=description,
            assignee="me",
            status=status,
            source_agent="family-calendar",
            created_at="2026-04-18T09:00:00Z",
            due_at=due_at,
        )
    )


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------


def test_done_flips_status_and_sets_completed_at(tcb):
    import brain_tasks  # type: ignore
    _seed("a-1", description="Do thing", due_at="2026-04-18T17:00:00Z")
    result = tcb.handle_task_callback(action="done", task_id="a-1")
    assert result["status"] == "ok"
    t = brain_tasks.read_tasks()[0]
    assert t.status == "done"
    assert t.completed_at is not None


# ---------------------------------------------------------------------------
# snooze (contextual)
# ---------------------------------------------------------------------------


def test_snooze_timed_task_advances_one_hour(tcb):
    import brain_tasks  # type: ignore
    _seed("a-1", due_at="2026-04-18T17:00:00Z")
    tcb.handle_task_callback(action="snooze", task_id="a-1")
    t = brain_tasks.read_tasks()[0]
    assert t.due_at == "2026-04-18T18:00:00Z"
    assert t.is_timed()


def test_snooze_all_day_task_advances_one_day(tcb):
    import brain_tasks  # type: ignore
    _seed("a-1", due_at="2026-04-18")
    tcb.handle_task_callback(action="snooze", task_id="a-1")
    t = brain_tasks.read_tasks()[0]
    assert t.due_at == "2026-04-19"
    assert t.is_all_day()


def test_snooze_clears_sent_reminders_dedup(tcb):
    import brain_tasks  # type: ignore
    _seed("a-1", due_at="2026-04-18T17:00:00Z")
    # Pre-populate dedup entries for this task
    with open(tcb.SENT_REMINDERS_PATH, "w") as f:
        json.dump({"reminders": {
            "a-1_t_30min": "2026-04-18T16:30:00Z",
            "a-1_t_24h_overdue": "2026-04-18T17:30:00Z",
            "other-event_30min": "2026-04-18T10:00:00Z",
        }}, f)
    tcb.handle_task_callback(action="snooze", task_id="a-1")
    with open(tcb.SENT_REMINDERS_PATH) as f:
        after = json.load(f)
    # Only a-1's entries are cleared, unrelated reminders preserved
    assert "a-1_t_30min" not in after["reminders"]
    assert "a-1_t_24h_overdue" not in after["reminders"]
    assert "other-event_30min" in after["reminders"]


# ---------------------------------------------------------------------------
# ignore
# ---------------------------------------------------------------------------


def test_ignore_flips_status_to_ignored(tcb):
    import brain_tasks  # type: ignore
    _seed("a-1", due_at="2026-04-18T17:00:00Z")
    result = tcb.handle_task_callback(action="ignore", task_id="a-1")
    assert result["status"] == "ok"
    t = brain_tasks.read_tasks()[0]
    assert t.status == "ignored"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unknown_task_id_returns_error_result(tcb):
    result = tcb.handle_task_callback(action="done", task_id="missing")
    assert result["status"] == "error"


def test_unknown_action_returns_error_result(tcb):
    _seed("a-1", due_at="2026-04-18T17:00:00Z")
    result = tcb.handle_task_callback(action="bogus", task_id="a-1")
    assert result["status"] == "error"
