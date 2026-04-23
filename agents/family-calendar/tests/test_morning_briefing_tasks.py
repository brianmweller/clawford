"""Tests for morning-briefing.py task-section integration.

The brief now renders a ``✅ TODAY'S TASKS`` section between the
time-blocked event bucket and the tomorrow preview, sourcing from the
shared brain's ``tasks/queue.md``. On Mondays, a ``📝 UNSCHEDULED`` block
rolls up open tasks with no due_at.

format_brief keeps its existing positional args for back-compat and
takes ``today_tasks`` and ``unscheduled_tasks`` as keyword-only params
(both default to empty lists — silent when there's nothing to render).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "family-calendar" / "scripts" / "morning-briefing.py"
SHARED_DIR = REPO_ROOT / "agents" / "shared"
AGENT_DIR = REPO_ROOT / "agents" / "family-calendar"


@pytest.fixture
def mb():
    # Make brain_tasks and task_surfacer_lib importable for any direct calls
    for p in (str(SHARED_DIR), str(AGENT_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    for mod in ("brain", "brain_tasks", "task_surfacer_lib", "morning_briefing"):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tuesday_pacific():
    return datetime(2026, 4, 14, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


@pytest.fixture
def monday_pacific():
    return datetime(2026, 4, 20, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def _task(
    tid,
    *,
    description="thing",
    assignee="me",
    status="open",
    due_at=None,
):
    import brain_tasks  # type: ignore
    return brain_tasks.Task(
        id=tid,
        description=description,
        assignee=assignee,
        status=status,
        source_agent="family-calendar",
        created_at="2026-04-14T00:00:00Z",
        due_at=due_at,
    )


# ---------------------------------------------------------------------------
# Section appears / doesn't appear
# ---------------------------------------------------------------------------


def test_no_tasks_section_when_empty(mb, tuesday_pacific):
    brief = mb.format_brief([], None, tuesday_pacific)
    assert "TODAY'S TASKS" not in brief
    assert "UNSCHEDULED" not in brief


def test_today_tasks_section_rendered(mb, tuesday_pacific):
    tasks = [
        _task("a-1", description="Pay quarterly taxes", due_at="2026-04-14T17:00:00Z"),
        _task("a-2", description="Water plants", due_at="2026-04-14"),
    ]
    brief = mb.format_brief(
        [], None, tuesday_pacific, today_tasks=tasks
    )
    assert "TODAY'S TASKS" in brief
    assert "Pay quarterly taxes" in brief
    assert "Water plants" in brief


def test_today_task_with_time_shows_pacific_time(mb, tuesday_pacific):
    # 17:00 UTC on 2026-04-14 == 10:00 AM Pacific (PDT, UTC-7)
    tasks = [_task("a-1", description="Call plumber", due_at="2026-04-14T17:00:00Z")]
    brief = mb.format_brief([], None, tuesday_pacific, today_tasks=tasks)
    # Rendered Pacific-local time should appear
    assert "10:00 AM" in brief or "10:00AM" in brief


def test_today_all_day_task_shows_all_day_label(mb, tuesday_pacific):
    tasks = [_task("a-1", description="Renew library card", due_at="2026-04-14")]
    brief = mb.format_brief([], None, tuesday_pacific, today_tasks=tasks)
    assert "Renew library card" in brief
    # Shouldn't include a phantom time
    assert "??:??" not in brief


# ---------------------------------------------------------------------------
# Monday unscheduled rollup
# ---------------------------------------------------------------------------


def test_unscheduled_tasks_rollup_on_monday(mb, monday_pacific):
    unscheduled = [
        _task("u-1", description="Fix the attic light"),
        _task("u-2", description="Plan summer trip"),
    ]
    brief = mb.format_brief(
        [], None, monday_pacific, unscheduled_tasks=unscheduled
    )
    assert "UNSCHEDULED" in brief
    assert "Fix the attic light" in brief
    assert "Plan summer trip" in brief


def test_no_unscheduled_section_on_non_monday(mb, tuesday_pacific):
    unscheduled = [_task("u-1", description="Fix the attic light")]
    brief = mb.format_brief(
        [], None, tuesday_pacific, unscheduled_tasks=unscheduled
    )
    # Section header omitted even though tasks were passed
    assert "UNSCHEDULED" not in brief
    assert "Fix the attic light" not in brief


def test_no_unscheduled_section_on_monday_when_empty(mb, monday_pacific):
    brief = mb.format_brief(
        [], None, monday_pacific, unscheduled_tasks=[]
    )
    assert "UNSCHEDULED" not in brief


# ---------------------------------------------------------------------------
# Back-compat — existing callers pass only positional args
# ---------------------------------------------------------------------------


def test_format_brief_back_compat_without_task_kwargs(mb, tuesday_pacific):
    # The existing run() call site uses 3 positional args. Must still work.
    brief = mb.format_brief([], None, tuesday_pacific)
    assert "TOMORROW PREVIEW" in brief  # existing section still renders
    assert "TODAY'S TASKS" not in brief
