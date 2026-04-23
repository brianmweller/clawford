"""Tests for agents/family-calendar/task_surfacer_lib.py — tier classifier.

The surfacer is Mouse's policy layer on top of the brain's task primitive.
It takes the raw task list and a ``now`` and returns:

    tasks_for_morning_brief(tasks, now_pacific)
        Open + assignee=me + due_at date == today (Pacific).

    tasks_for_unscheduled_rollup(tasks)
        Open + assignee=me + no due_at. Used on Mondays only.

    pending_reminders(tasks, now_utc, already_sent)
        Ping payloads for T-30min (timed only) and T+24h overdue tiers.
        Each payload carries a dedup key the caller persists in
        sent-reminders.json.

Tier policy:
    - T-30min fires for timed tasks when (due_at - now) is in (0, 30 min].
    - T+24h fires for any task overdue by more than 24 h. For all-day
      tasks, "overdue by 24 h" means the calendar day *after* the due
      date has fully passed.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(AGENT_DIR))


def _reload_surfacer():
    for mod in list(sys.modules):
        if mod in {"task_surfacer_lib", "brain_tasks", "brain"}:
            del sys.modules[mod]
    import task_surfacer_lib  # type: ignore
    return task_surfacer_lib


PACIFIC = ZoneInfo("America/Los_Angeles")


def _task(
    task_id,
    *,
    description="thing",
    assignee="me",
    status="open",
    due_at=None,
    completed_at=None,
):
    import brain_tasks  # type: ignore
    return brain_tasks.Task(
        id=task_id,
        description=description,
        assignee=assignee,
        status=status,
        source_agent="family-calendar",
        created_at="2026-04-18T00:00:00Z",
        due_at=due_at,
        completed_at=completed_at,
    )


# ---------------------------------------------------------------------------
# Morning-brief section
# ---------------------------------------------------------------------------


def test_morning_brief_includes_today_all_day_task():
    s = _reload_surfacer()
    now = datetime(2026, 4, 18, 8, 0, tzinfo=PACIFIC)
    tasks = [_task("a-1", due_at="2026-04-18")]
    assert [t.id for t in s.tasks_for_morning_brief(tasks, now)] == ["a-1"]


def test_morning_brief_includes_today_timed_task():
    s = _reload_surfacer()
    now = datetime(2026, 4, 18, 5, 0, tzinfo=PACIFIC)
    # 5 PM Pacific == 00:00 UTC next day — make sure the date comparison uses Pacific
    tasks = [_task("a-1", due_at="2026-04-19T00:00:00Z")]  # 5 PM Pacific on 2026-04-18
    assert [t.id for t in s.tasks_for_morning_brief(tasks, now)] == ["a-1"]


def test_morning_brief_excludes_tomorrow():
    s = _reload_surfacer()
    now = datetime(2026, 4, 18, 8, 0, tzinfo=PACIFIC)
    tasks = [_task("a-1", due_at="2026-04-19")]
    assert s.tasks_for_morning_brief(tasks, now) == []


def test_morning_brief_excludes_yesterday():
    s = _reload_surfacer()
    now = datetime(2026, 4, 18, 8, 0, tzinfo=PACIFIC)
    tasks = [_task("a-1", due_at="2026-04-17")]
    assert s.tasks_for_morning_brief(tasks, now) == []


def test_morning_brief_excludes_done_and_other_terminal():
    s = _reload_surfacer()
    now = datetime(2026, 4, 18, 8, 0, tzinfo=PACIFIC)
    tasks = [
        _task("a-1", due_at="2026-04-18", status="done"),
        _task("a-2", due_at="2026-04-18", status="ignored"),
        _task("a-3", due_at="2026-04-18", status="cancelled"),
        _task("a-4", due_at="2026-04-18", status="open"),
    ]
    assert [t.id for t in s.tasks_for_morning_brief(tasks, now)] == ["a-4"]


def test_morning_brief_excludes_not_assigned_to_me():
    s = _reload_surfacer()
    now = datetime(2026, 4, 18, 8, 0, tzinfo=PACIFIC)
    tasks = [
        _task("a-1", due_at="2026-04-18", assignee="wife"),
        _task("a-2", due_at="2026-04-18", assignee="me"),
    ]
    assert [t.id for t in s.tasks_for_morning_brief(tasks, now)] == ["a-2"]


def test_morning_brief_excludes_tasks_with_no_due_at():
    s = _reload_surfacer()
    now = datetime(2026, 4, 18, 8, 0, tzinfo=PACIFIC)
    tasks = [_task("a-1", due_at=None)]
    assert s.tasks_for_morning_brief(tasks, now) == []


# ---------------------------------------------------------------------------
# Unscheduled rollup (Monday-only)
# ---------------------------------------------------------------------------


def test_unscheduled_rollup_lists_tasks_with_no_due_at():
    s = _reload_surfacer()
    tasks = [
        _task("a-1", due_at=None),
        _task("a-2", due_at="2026-04-18"),
        _task("a-3", due_at=None, status="done"),  # terminal — skipped
        _task("a-4", due_at=None, assignee="wife"),  # not me — skipped
    ]
    assert [t.id for t in s.tasks_for_unscheduled_rollup(tasks)] == ["a-1"]


# ---------------------------------------------------------------------------
# T-30min tier
# ---------------------------------------------------------------------------


def test_t_30min_fires_on_timed_task_within_window():
    s = _reload_surfacer()
    due = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    now = due - timedelta(minutes=20)  # 20 min before due, within the 0-30 min window
    tasks = [_task("a-1", due_at=due.isoformat().replace("+00:00", "Z"))]
    reminders = s.pending_reminders(tasks, now, already_sent=set())
    assert len(reminders) == 1
    assert reminders[0]["task_id"] == "a-1"
    assert reminders[0]["tier"] == "t_30min"
    assert reminders[0]["dedup_key"] == "a-1_t_30min"


def test_t_30min_does_not_fire_too_early():
    s = _reload_surfacer()
    due = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    now = due - timedelta(minutes=45)  # too early
    tasks = [_task("a-1", due_at=due.isoformat().replace("+00:00", "Z"))]
    assert s.pending_reminders(tasks, now, already_sent=set()) == []


def test_t_30min_does_not_fire_on_all_day_task():
    s = _reload_surfacer()
    now = datetime(2026, 4, 18, 16, 30, tzinfo=timezone.utc)
    tasks = [_task("a-1", due_at="2026-04-18")]
    reminders = s.pending_reminders(tasks, now, already_sent=set())
    # All-day doesn't get T-30min (no time to measure against)
    assert [r["tier"] for r in reminders] == []


def test_t_30min_respects_dedup():
    s = _reload_surfacer()
    due = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    now = due - timedelta(minutes=20)
    tasks = [_task("a-1", due_at=due.isoformat().replace("+00:00", "Z"))]
    reminders = s.pending_reminders(tasks, now, already_sent={"a-1_t_30min"})
    assert reminders == []


def test_t_30min_skips_terminal_statuses():
    s = _reload_surfacer()
    due = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    now = due - timedelta(minutes=20)
    tasks = [
        _task("a-1", due_at=due.isoformat().replace("+00:00", "Z"), status="done"),
        _task("a-2", due_at=due.isoformat().replace("+00:00", "Z"), status="ignored"),
        _task("a-3", due_at=due.isoformat().replace("+00:00", "Z"), status="open"),
    ]
    reminders = s.pending_reminders(tasks, now, already_sent=set())
    assert [r["task_id"] for r in reminders] == ["a-3"]


def test_t_30min_skips_not_me():
    s = _reload_surfacer()
    due = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    now = due - timedelta(minutes=20)
    tasks = [_task("a-1", due_at=due.isoformat().replace("+00:00", "Z"), assignee="wife")]
    assert s.pending_reminders(tasks, now, already_sent=set()) == []


# ---------------------------------------------------------------------------
# T+24h overdue tier
# ---------------------------------------------------------------------------


def test_t_24h_fires_on_timed_task_overdue_by_more_than_24h():
    s = _reload_surfacer()
    due = datetime(2026, 4, 17, 17, 0, tzinfo=timezone.utc)
    now = due + timedelta(hours=25)
    tasks = [_task("a-1", due_at=due.isoformat().replace("+00:00", "Z"))]
    reminders = s.pending_reminders(tasks, now, already_sent=set())
    assert any(r["tier"] == "t_24h_overdue" for r in reminders)


def test_t_24h_does_not_fire_if_overdue_less_than_24h():
    s = _reload_surfacer()
    due = datetime(2026, 4, 17, 17, 0, tzinfo=timezone.utc)
    now = due + timedelta(hours=12)
    tasks = [_task("a-1", due_at=due.isoformat().replace("+00:00", "Z"))]
    reminders = s.pending_reminders(tasks, now, already_sent=set())
    assert [r for r in reminders if r["tier"] == "t_24h_overdue"] == []


def test_t_24h_fires_on_all_day_task_after_next_day_passes():
    s = _reload_surfacer()
    # All-day task due 2026-04-17 → overdue by 24h starts 2026-04-19T00:00 UTC
    # (treat end-of-due-date as midnight start of next calendar day)
    now = datetime(2026, 4, 19, 1, 0, tzinfo=timezone.utc)
    tasks = [_task("a-1", due_at="2026-04-17")]
    reminders = s.pending_reminders(tasks, now, already_sent=set())
    assert any(r["tier"] == "t_24h_overdue" for r in reminders)


def test_t_24h_does_not_fire_same_day_for_all_day():
    s = _reload_surfacer()
    now = datetime(2026, 4, 17, 23, 0, tzinfo=timezone.utc)
    tasks = [_task("a-1", due_at="2026-04-17")]
    reminders = s.pending_reminders(tasks, now, already_sent=set())
    assert [r for r in reminders if r["tier"] == "t_24h_overdue"] == []


def test_t_24h_respects_dedup():
    s = _reload_surfacer()
    due = datetime(2026, 4, 17, 17, 0, tzinfo=timezone.utc)
    now = due + timedelta(hours=25)
    tasks = [_task("a-1", due_at=due.isoformat().replace("+00:00", "Z"))]
    reminders = s.pending_reminders(tasks, now, already_sent={"a-1_t_24h_overdue"})
    assert reminders == []


def test_t_24h_skips_terminal_statuses():
    s = _reload_surfacer()
    due = datetime(2026, 4, 17, 17, 0, tzinfo=timezone.utc)
    now = due + timedelta(hours=30)
    tasks = [
        _task("a-1", due_at=due.isoformat().replace("+00:00", "Z"), status="done"),
        _task("a-2", due_at=due.isoformat().replace("+00:00", "Z"), status="ignored"),
    ]
    assert s.pending_reminders(tasks, now, already_sent=set()) == []


# ---------------------------------------------------------------------------
# Reminder payload shape — carries everything the caller needs to format a ping
# ---------------------------------------------------------------------------


def test_reminder_payload_includes_description_and_due_at():
    s = _reload_surfacer()
    due = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    now = due - timedelta(minutes=20)
    tasks = [_task("a-1", description="Pay taxes", due_at=due.isoformat().replace("+00:00", "Z"))]
    reminders = s.pending_reminders(tasks, now, already_sent=set())
    r = reminders[0]
    assert r["description"] == "Pay taxes"
    assert r["due_at"] == due.isoformat().replace("+00:00", "Z")
