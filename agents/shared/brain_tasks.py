"""agents/shared/brain_tasks.py — task queue parser + in-place editors.

Owns the schema of ``tasks/queue.md`` in the Dropbox-synced brain. Format
mirrors the existing ``commitments/active.md`` convention:

    ## {id}
    - description: {str}
    - assignee: {str}
    - status: {str}
    - due_at: {iso}         (optional; date-only = all-day, datetime = timed)
    - source_agent: {str}
    - created_at: {iso}
    - completed_at: {iso}   (optional; set when status flips to done)

Status values:
    open, done, cancelled, ignored, deleted.
    ``deleted`` is a tombstone (user deleted the task from their phone's
    Google Tasks app); filtered from reads by default. ``ignored`` means
    stop surfacing locally; archived on GCal with a ``[IGNORED]`` title
    prefix.

``due_at`` is ISO 8601. Presence of ``T`` distinguishes timed (``2026-04-
18T17:00:00Z``) from all-day (``2026-04-18``). Snooze behaviour diverges:
timed tasks advance by 1h, all-day by 1 day.

In-place edits are the sole exception to the brain's append-only rule
and are permitted only for agent-authored status transitions, ``due_at``
updates on open tasks (for snooze), and setting ``completed_at``.
"""
from __future__ import annotations

import re
import sys as _sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

# Self-locate ``brain`` so this module works whether imported as
# ``brain_tasks`` (shared/ on sys.path) or as ``agents.shared.brain_tasks``
# (repo root on sys.path).
_SHARED_DIR = Path(__file__).resolve().parent
if str(_SHARED_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SHARED_DIR))

from brain import dropbox_brain_root  # noqa: E402


QUEUE_RELPATH = "tasks/queue.md"

VALID_STATUSES = {"open", "done", "cancelled", "ignored", "deleted"}
TERMINAL_STATUSES = {"done", "cancelled", "ignored", "deleted"}


@dataclass
class Task:
    id: str
    description: str
    assignee: str
    status: str
    source_agent: str
    created_at: str
    due_at: Optional[str] = None
    completed_at: Optional[str] = None

    def is_timed(self) -> bool:
        """True if ``due_at`` has an explicit time-of-day component."""
        return self.due_at is not None and "T" in self.due_at

    def is_all_day(self) -> bool:
        """True if ``due_at`` is date-only (``YYYY-MM-DD`` with no ``T``)."""
        return self.due_at is not None and "T" not in self.due_at

    def due_date(self) -> Optional[date]:
        """Return the calendar date portion of ``due_at``, or ``None`` if
        the task has no due date at all."""
        if not self.due_at:
            return None
        return date.fromisoformat(self.due_at.split("T", 1)[0])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^##\s+(\S+)\s*$")
_FIELD_RE = re.compile(r"^-\s+([a-z_]+):\s?(.*)$")


def _parse_block(task_id: str, field_lines: list[str]) -> Optional[Task]:
    """Turn the body of a section into a Task. Returns None if required
    fields are missing (malformed block — caller skips it)."""
    fields: dict[str, str] = {}
    for line in field_lines:
        m = _FIELD_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        fields[key] = value

    required = ("description", "assignee", "status", "source_agent", "created_at")
    if not all(k in fields for k in required):
        return None

    return Task(
        id=task_id,
        description=fields["description"],
        assignee=fields["assignee"],
        status=fields["status"],
        source_agent=fields["source_agent"],
        created_at=fields["created_at"],
        due_at=fields.get("due_at"),
        completed_at=fields.get("completed_at"),
    )


def parse_queue(content: str, *, include_deleted: bool = False) -> list[Task]:
    """Parse queue.md content into a list of Task objects.

    Tombstones (``status=deleted``) are filtered out by default so every
    consumer — morning brief, reminder-check, sync push — sees a clean
    view. Pass ``include_deleted=True`` when you genuinely need the full
    history (e.g., the GCal sync's reconciliation pass).
    """
    tasks: list[Task] = []
    current_id: Optional[str] = None
    current_lines: list[str] = []

    def _flush():
        nonlocal current_id, current_lines
        if current_id is None:
            return
        t = _parse_block(current_id, current_lines)
        if t is not None:
            tasks.append(t)
        current_id = None
        current_lines = []

    for raw in content.splitlines():
        m = _HEADING_RE.match(raw)
        if m:
            _flush()
            current_id = m.group(1)
            current_lines = []
            continue
        if current_id is not None:
            current_lines.append(raw)
    _flush()

    if not include_deleted:
        tasks = [t for t in tasks if t.status != "deleted"]
    return tasks


# ---------------------------------------------------------------------------
# Dropbox-brain wrappers
# ---------------------------------------------------------------------------


def _queue_path() -> Path:
    return dropbox_brain_root() / QUEUE_RELPATH


def read_tasks(*, include_deleted: bool = False) -> list[Task]:
    """Read ``tasks/queue.md`` from the Dropbox brain. Returns an empty
    list if the file doesn't exist yet — callers should not have to guard
    the first-run case."""
    path = _queue_path()
    if not path.exists():
        return []
    return parse_queue(path.read_text(encoding="utf-8"), include_deleted=include_deleted)


# ---------------------------------------------------------------------------
# In-place edits  (the sole exception to the brain's append-only rule)
# ---------------------------------------------------------------------------


def _write_queue(content: str) -> None:
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _replace_field_in_block(content: str, task_id: str, key: str, value: str) -> str:
    """Return ``content`` with the ``- {key}: {value}`` line inside the
    ``## {task_id}`` section replaced. If the field is missing, insert
    it before the next heading or EOF."""
    lines = content.splitlines(keepends=False)
    n = len(lines)

    # Find the section heading line
    heading_idx = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(1) == task_id:
            heading_idx = i
            break
    if heading_idx is None:
        raise KeyError(f"task id not found: {task_id!r}")

    # Scan forward until the next heading or EOF; replace or insert
    section_end = n
    for j in range(heading_idx + 1, n):
        if _HEADING_RE.match(lines[j]):
            section_end = j
            break

    field_prefix = f"- {key}:"
    new_line = f"- {key}: {value}"
    replaced = False
    for j in range(heading_idx + 1, section_end):
        if lines[j].startswith(field_prefix):
            lines[j] = new_line
            replaced = True
            break
    if not replaced:
        # Insert just before section_end (skipping trailing blank lines of the block)
        insert_at = section_end
        while insert_at > heading_idx + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, new_line)

    # Preserve trailing newline if the original had one
    out = "\n".join(lines)
    if content.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def edit_task_status(
    task_id: str,
    new_status: str,
    *,
    completed_at: Optional[str] = None,
) -> None:
    """Flip a task's ``status`` line in place. When flipping to ``done``,
    ``completed_at`` should be provided (ISO 8601 timestamp) and is
    written to a ``- completed_at: ...`` line in the same section.

    Raises KeyError if the id is unknown, ValueError if the status isn't
    one of VALID_STATUSES.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(VALID_STATUSES)!r}, got {new_status!r}"
        )
    path = _queue_path()
    if not path.exists():
        raise KeyError(f"task id not found: {task_id!r}")
    content = path.read_text(encoding="utf-8")
    content = _replace_field_in_block(content, task_id, "status", new_status)
    if completed_at is not None:
        content = _replace_field_in_block(content, task_id, "completed_at", completed_at)
    _write_queue(content)


def edit_task_due_at(task_id: str, new_due_at: str) -> None:
    """Replace a task's ``due_at`` line in place. Used by the snooze
    callback: timed tasks are advanced by 1h, all-day by 1 day. The
    caller is responsible for formatting — this function just writes
    what it's given."""
    path = _queue_path()
    if not path.exists():
        raise KeyError(f"task id not found: {task_id!r}")
    content = path.read_text(encoding="utf-8")
    content = _replace_field_in_block(content, task_id, "due_at", new_due_at)
    _write_queue(content)


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def _render_block(task: Task) -> str:
    lines = [
        f"## {task.id}",
        f"- description: {task.description}",
        f"- assignee: {task.assignee}",
        f"- status: {task.status}",
    ]
    if task.due_at is not None:
        lines.append(f"- due_at: {task.due_at}")
    lines.extend(
        [
            f"- source_agent: {task.source_agent}",
            f"- created_at: {task.created_at}",
        ]
    )
    if task.completed_at is not None:
        lines.append(f"- completed_at: {task.completed_at}")
    return "\n".join(lines) + "\n"


def append_task(task: Task) -> None:
    """Append a new task section to ``tasks/queue.md``. Creates the file
    (with the standard header) if it doesn't exist yet."""
    if task.status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {task.status!r}")
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    block = _render_block(task)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        sep = "" if existing.endswith("\n") else "\n"
        path.write_text(existing + sep + block + "\n", encoding="utf-8")
    else:
        header = (
            "# Tasks — Queue\n\n"
            "> Append-only. ``deleted`` is a tombstone — filtered from reads.\n"
            "> In-place edits: ``status``, ``due_at`` (open tasks only), ``completed_at``.\n\n"
            "---\n\n"
        )
        path.write_text(header + block + "\n", encoding="utf-8")
