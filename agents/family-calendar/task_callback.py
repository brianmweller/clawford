"""agents/family-calendar/task_callback.py — inline-keyboard executor.

Called by ``agents/shared/dispatcher.py`` when the user taps a button on
a task-reminder Telegram message. The executor mutates ``queue.md`` in
place and clears any stale ``sent-reminders.json`` entries so a snoozed
task can fire a fresh T-30min ping against its new ``due_at``.

Actions:
    done    — flip status to done, stamp completed_at.
    snooze  — contextual. Timed tasks (due_at has ``T``) advance 1 hour;
              all-day tasks (date-only due_at) advance 1 calendar day.
              Sent-reminders entries for the task are cleared so the
              T-30min tier re-arms against the updated due_at.
    ignore  — flip status to ignored. Next sync tick will prefix the
              GCal task title with ``[IGNORED] `` and mark it completed.

Actual propagation to Google Tasks happens at the next
``family-calendar-tasks-sync`` cron tick (≤5 min). We don't fire an
inline push because the callback runs inside the dispatcher thread and
we want it to stay snappy.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import brain_tasks  # type: ignore  # noqa: E402


WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")
SENT_REMINDERS_PATH = os.path.join(WORKSPACE, "sent-reminders.json")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt_utc(iso: str) -> datetime:
    s = iso.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _format_dt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clear_sent_reminders_for_task(task_id: str) -> None:
    """Drop ``{task_id}_{tier}`` entries from sent-reminders.json so a
    snoozed task can re-fire its T-30min ping. Silently tolerates a
    missing or malformed cache file — reminder-check.py treats a missing
    file as empty."""
    if not os.path.exists(SENT_REMINDERS_PATH):
        return
    try:
        with open(SENT_REMINDERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    reminders = data.get("reminders", {}) or {}
    prefix = task_id + "_"
    reminders = {k: v for k, v in reminders.items() if not k.startswith(prefix)}
    data["reminders"] = reminders
    tmp = SENT_REMINDERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SENT_REMINDERS_PATH)


def _find_task(task_id: str) -> Optional[brain_tasks.Task]:
    for t in brain_tasks.read_tasks(include_deleted=True):
        if t.id == task_id:
            return t
    return None


def _advance_due_at(task: brain_tasks.Task) -> str:
    """Return the new ``due_at`` for a snooze. Preserves the shape of
    the original (date-only stays date-only, datetime stays datetime)."""
    if task.is_timed():
        dt = _parse_dt_utc(task.due_at)  # type: ignore[arg-type]
        return _format_dt_utc(dt + timedelta(hours=1))
    # all-day
    d = task.due_date()
    if d is None:
        # No due_at at all — fall back to "tomorrow in UTC"
        d = datetime.now(timezone.utc).date()
    return (d + timedelta(days=1)).isoformat()


def handle_task_callback(*, action: str, task_id: str) -> dict:
    """Apply the button action to the task. Returns a small status dict
    (``{status: ok|error, ...}``) the dispatcher can surface via toast.
    """
    task = _find_task(task_id)
    if task is None:
        return {"status": "error", "error": f"task not found: {task_id!r}"}

    try:
        if action == "done":
            brain_tasks.edit_task_status(task_id, "done", completed_at=_iso_now())
        elif action == "snooze":
            if task.status != "open":
                return {"status": "error", "error": "can only snooze open tasks"}
            new_due = _advance_due_at(task)
            brain_tasks.edit_task_due_at(task_id, new_due)
            _clear_sent_reminders_for_task(task_id)
        elif action == "ignore":
            brain_tasks.edit_task_status(task_id, "ignored")
        else:
            return {"status": "error", "error": f"unknown action: {action!r}"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    return {"status": "ok", "action": action, "task_id": task_id}
