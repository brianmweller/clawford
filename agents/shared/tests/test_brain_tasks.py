"""Tests for agents/shared/brain_tasks.py — task queue parser + in-place edits.

Task queue format mirrors the brain's existing ``commitments/active.md`` section
convention: ``## {id}`` heading followed by ``- key: value`` dash-key-value
lines, one field per line. Values run to end-of-line — no quote-based
delimiters — so descriptions containing ``"``, ``:`` and other punctuation are
preserved byte-for-byte.

The module is small on purpose:
    parse_queue(content)            -> list[Task]
    read_tasks()                    -> list[Task]  (Dropbox brain wrapper)
    edit_task_status(id, new, ...)  -> None        (in-place allowed exception)
    edit_task_due_at(id, new)       -> None        (in-place allowed exception)
    append_task(task)               -> None

Status values: open, done, cancelled, ignored (terminal — stop surfacing,
archived on GCal with [IGNORED] prefix), deleted (tombstone from phone-delete,
filtered from all reads unless include_deleted=True).

``due_at`` is ISO 8601 — date-only (``2026-04-18``) means all-day task, datetime
(``2026-04-18T17:00:00Z``) means specific-time task. Snooze behaviour diverges
based on which shape it is.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload_brain_tasks():
    for mod in list(sys.modules):
        if mod in {"brain", "brain_tasks"} or mod.startswith(("brain.", "brain_tasks.")):
            del sys.modules[mod]
    import brain_tasks  # type: ignore
    return brain_tasks


@pytest.fixture
def sandboxed_brain(tmp_path, monkeypatch):
    dropbox_root = tmp_path / "dropbox-brain"
    git_root = tmp_path / "git-brain"
    dropbox_root.mkdir()
    git_root.mkdir()
    (dropbox_root / "tasks").mkdir()
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(dropbox_root))
    monkeypatch.setenv("CLAWFORD_BRAIN_GIT_ROOT", str(git_root))
    return dropbox_root


HEADER_ONLY = """# Tasks — Queue

> **Schema:** Each entry is an action item.
>
> (header content omitted)

---

"""


def _task_block(
    task_id: str,
    *,
    description: str,
    assignee: str = "me",
    status: str = "open",
    due_at: str | None = None,
    source_agent: str = "family-calendar",
    created_at: str = "2026-04-18T09:00:00Z",
    completed_at: str | None = None,
) -> str:
    lines = [f"## {task_id}", f"- description: {description}", f"- assignee: {assignee}", f"- status: {status}"]
    if due_at is not None:
        lines.append(f"- due_at: {due_at}")
    lines.extend([f"- source_agent: {source_agent}", f"- created_at: {created_at}"])
    if completed_at is not None:
        lines.append(f"- completed_at: {completed_at}")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Parser: basics
# ---------------------------------------------------------------------------


def test_parse_header_only_returns_empty_list(sandboxed_brain):
    bt = _reload_brain_tasks()
    assert bt.parse_queue(HEADER_ONLY) == []


def test_parse_single_task(sandboxed_brain):
    bt = _reload_brain_tasks()
    content = HEADER_ONLY + _task_block(
        "family-calendar-2026-04-18-001",
        description="Pay quarterly tax estimate",
        due_at="2026-04-18T17:00:00Z",
    )
    tasks = bt.parse_queue(content)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.id == "family-calendar-2026-04-18-001"
    assert t.description == "Pay quarterly tax estimate"
    assert t.assignee == "me"
    assert t.status == "open"
    assert t.due_at == "2026-04-18T17:00:00Z"
    assert t.source_agent == "family-calendar"
    assert t.created_at == "2026-04-18T09:00:00Z"
    assert t.completed_at is None


def test_parse_multiple_tasks(sandboxed_brain):
    bt = _reload_brain_tasks()
    content = (
        HEADER_ONLY
        + _task_block("a-2026-04-18-001", description="First")
        + _task_block("a-2026-04-18-002", description="Second", status="done", completed_at="2026-04-18T12:00:00Z")
    )
    tasks = bt.parse_queue(content)
    assert [t.id for t in tasks] == ["a-2026-04-18-001", "a-2026-04-18-002"]
    assert tasks[1].status == "done"
    assert tasks[1].completed_at == "2026-04-18T12:00:00Z"


# ---------------------------------------------------------------------------
# Parser: robustness to punctuation in description
# ---------------------------------------------------------------------------


def test_parse_description_with_quotes_and_colons(sandboxed_brain):
    bt = _reload_brain_tasks()
    # Description contains double quotes, single quotes, colons, and a pipe
    tricky = 'Email Jay: "quarterly update" — reply by EOD | ping if silent'
    content = HEADER_ONLY + _task_block("a-2026-04-18-001", description=tricky)
    tasks = bt.parse_queue(content)
    assert tasks[0].description == tricky


def test_parse_description_with_backslash_and_unicode(sandboxed_brain):
    bt = _reload_brain_tasks()
    tricky = "Send Priya the recipe (flour 🌾 + honey) — backslash \\ preserved"
    content = HEADER_ONLY + _task_block("a-2026-04-18-001", description=tricky)
    tasks = bt.parse_queue(content)
    assert tasks[0].description == tricky


# ---------------------------------------------------------------------------
# Task model: timed vs all-day
# ---------------------------------------------------------------------------


def test_timed_task_detection(sandboxed_brain):
    bt = _reload_brain_tasks()
    content = HEADER_ONLY + _task_block("a-1", description="Timed", due_at="2026-04-18T17:00:00Z")
    t = bt.parse_queue(content)[0]
    assert t.is_timed() is True
    assert t.is_all_day() is False


def test_all_day_task_detection(sandboxed_brain):
    bt = _reload_brain_tasks()
    content = HEADER_ONLY + _task_block("a-1", description="All day", due_at="2026-04-18")
    t = bt.parse_queue(content)[0]
    assert t.is_timed() is False
    assert t.is_all_day() is True


def test_no_due_at_is_neither_timed_nor_all_day(sandboxed_brain):
    bt = _reload_brain_tasks()
    content = HEADER_ONLY + _task_block("a-1", description="Someday")
    t = bt.parse_queue(content)[0]
    assert t.due_at is None
    assert t.is_timed() is False
    assert t.is_all_day() is False


def test_due_date_extraction(sandboxed_brain):
    from datetime import date
    bt = _reload_brain_tasks()
    content = (
        HEADER_ONLY
        + _task_block("a-1", description="Timed", due_at="2026-04-18T17:00:00Z")
        + _task_block("a-2", description="All day", due_at="2026-04-19")
        + _task_block("a-3", description="Someday")
    )
    tasks = bt.parse_queue(content)
    assert tasks[0].due_date() == date(2026, 4, 18)
    assert tasks[1].due_date() == date(2026, 4, 19)
    assert tasks[2].due_date() is None


# ---------------------------------------------------------------------------
# Parser: deleted tombstones filtered by default
# ---------------------------------------------------------------------------


def test_deleted_tasks_filtered_by_default(sandboxed_brain):
    bt = _reload_brain_tasks()
    content = (
        HEADER_ONLY
        + _task_block("a-1", description="Live", status="open")
        + _task_block("a-2", description="Tombstone", status="deleted")
    )
    tasks = bt.parse_queue(content)
    assert [t.id for t in tasks] == ["a-1"]


def test_deleted_tasks_included_when_requested(sandboxed_brain):
    bt = _reload_brain_tasks()
    content = (
        HEADER_ONLY
        + _task_block("a-1", description="Live", status="open")
        + _task_block("a-2", description="Tombstone", status="deleted")
    )
    tasks = bt.parse_queue(content, include_deleted=True)
    assert [t.id for t in tasks] == ["a-1", "a-2"]


# ---------------------------------------------------------------------------
# read_tasks: Dropbox-brain wrapper
# ---------------------------------------------------------------------------


def test_read_tasks_reads_from_dropbox_brain(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(
        HEADER_ONLY + _task_block("a-1", description="Hello", due_at="2026-04-18"),
        encoding="utf-8",
    )
    tasks = bt.read_tasks()
    assert len(tasks) == 1
    assert tasks[0].description == "Hello"


def test_read_tasks_missing_file_returns_empty(sandboxed_brain):
    bt = _reload_brain_tasks()
    # No queue.md exists — agents should tolerate this gracefully
    tasks = bt.read_tasks()
    assert tasks == []


# ---------------------------------------------------------------------------
# In-place status edit
# ---------------------------------------------------------------------------


def test_edit_task_status_flips_open_to_done_with_completed_at(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(
        HEADER_ONLY + _task_block("a-1", description="Do thing", due_at="2026-04-18T17:00:00Z"),
        encoding="utf-8",
    )
    bt.edit_task_status("a-1", "done", completed_at="2026-04-18T17:05:00Z")
    tasks = bt.read_tasks()
    assert tasks[0].status == "done"
    assert tasks[0].completed_at == "2026-04-18T17:05:00Z"


def test_edit_task_status_to_ignored_does_not_set_completed_at(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(
        HEADER_ONLY + _task_block("a-1", description="Nope", due_at="2026-04-18"),
        encoding="utf-8",
    )
    bt.edit_task_status("a-1", "ignored")
    tasks = bt.read_tasks()
    assert tasks[0].status == "ignored"
    assert tasks[0].completed_at is None


def test_edit_task_status_preserves_other_tasks(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(
        HEADER_ONLY
        + _task_block("a-1", description="First", due_at="2026-04-18")
        + _task_block("a-2", description="Second", due_at="2026-04-19"),
        encoding="utf-8",
    )
    bt.edit_task_status("a-2", "done", completed_at="2026-04-19T12:00:00Z")
    tasks = bt.read_tasks()
    assert tasks[0].id == "a-1" and tasks[0].status == "open"
    assert tasks[1].id == "a-2" and tasks[1].status == "done"


def test_edit_task_status_unknown_id_raises(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(HEADER_ONLY, encoding="utf-8")
    with pytest.raises(KeyError):
        bt.edit_task_status("missing-id", "done", completed_at="2026-04-18T17:05:00Z")


def test_edit_task_status_rejects_invalid_status(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(
        HEADER_ONLY + _task_block("a-1", description="x", due_at="2026-04-18"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        bt.edit_task_status("a-1", "bogus")


# ---------------------------------------------------------------------------
# In-place due_at edit (used for snooze)
# ---------------------------------------------------------------------------


def test_edit_task_due_at_replaces_existing(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(
        HEADER_ONLY + _task_block("a-1", description="x", due_at="2026-04-18T17:00:00Z"),
        encoding="utf-8",
    )
    bt.edit_task_due_at("a-1", "2026-04-18T18:00:00Z")
    assert bt.read_tasks()[0].due_at == "2026-04-18T18:00:00Z"


def test_edit_task_due_at_preserves_all_day_format(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(
        HEADER_ONLY + _task_block("a-1", description="x", due_at="2026-04-18"),
        encoding="utf-8",
    )
    bt.edit_task_due_at("a-1", "2026-04-19")
    t = bt.read_tasks()[0]
    assert t.due_at == "2026-04-19"
    assert t.is_all_day() is True


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def test_append_task_adds_to_queue(sandboxed_brain):
    bt = _reload_brain_tasks()
    queue_path = sandboxed_brain / "tasks" / "queue.md"
    queue_path.write_text(HEADER_ONLY, encoding="utf-8")
    task = bt.Task(
        id="family-calendar-2026-04-18-001",
        description="Brand new task",
        assignee="me",
        status="open",
        due_at="2026-04-18T17:00:00Z",
        source_agent="family-calendar",
        created_at="2026-04-18T09:00:00Z",
    )
    bt.append_task(task)
    tasks = bt.read_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "family-calendar-2026-04-18-001"
    assert tasks[0].description == "Brand new task"


def test_append_task_creates_file_if_missing(sandboxed_brain):
    bt = _reload_brain_tasks()
    task = bt.Task(
        id="a-1",
        description="First ever task in file",
        assignee="me",
        status="open",
        due_at=None,
        source_agent="family-calendar",
        created_at="2026-04-18T09:00:00Z",
    )
    bt.append_task(task)
    tasks = bt.read_tasks()
    assert len(tasks) == 1
    assert tasks[0].description == "First ever task in file"


def test_append_task_roundtrip_preserves_tricky_description(sandboxed_brain):
    bt = _reload_brain_tasks()
    tricky = 'Reply: "see you Tuesday" — note the | pipe and \\ backslash'
    task = bt.Task(
        id="a-1",
        description=tricky,
        assignee="me",
        status="open",
        due_at=None,
        source_agent="family-calendar",
        created_at="2026-04-18T09:00:00Z",
    )
    bt.append_task(task)
    assert bt.read_tasks()[0].description == tricky
