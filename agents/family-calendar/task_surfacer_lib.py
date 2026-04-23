"""agents/family-calendar/task_surfacer_lib.py — tier classifier for Mouse.

Pure functions. No I/O, no Telegram, no GCal. Given a task list and a
``now``, return which tasks belong in which surface.

Tiers:
    morning_brief   — open + assignee=me + due_at date == today (Pacific).
                      All-day and timed tasks both qualify. Rendered into
                      the 5 AM PT brief's "Today's tasks" section.

    unscheduled_rollup — open + assignee=me + no due_at at all. Rendered
                      Monday-only as a "don't forget about these" section.

    t_30min         — timed tasks with (due_at − now) in (0, 30 min].
                      All-day tasks are excluded — there's no T-30 for
                      a task with no time component. Pinged by reminder-
                      check.py with the three-button inline keyboard.

    t_24h_overdue   — any open task overdue by more than 24 h. For all-
                      day tasks, effective due is UTC midnight at the
                      end of the due date; the 24-h clock starts there.

Dedup is the caller's responsibility: ``pending_reminders`` takes an
``already_sent`` set and skips any dedup key it contains, but persistence
lives in ``sent-reminders.json`` one layer up.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from brain_tasks import Task  # type: ignore


TERMINAL = {"done", "cancelled", "ignored", "deleted"}
T_30MIN = "t_30min"
T_24H_OVERDUE = "t_24h_overdue"


def _is_mine_and_open(t: Task) -> bool:
    return t.assignee == "me" and t.status == "open"


def tasks_for_morning_brief(tasks: Iterable[Task], now_pacific: datetime) -> list[Task]:
    """Tasks due today, for the 5 AM brief section."""
    today = now_pacific.date()
    pacific_tz = now_pacific.tzinfo
    out: list[Task] = []
    for t in tasks:
        if not _is_mine_and_open(t):
            continue
        if t.due_at is None:
            continue
        due_date = _task_local_date(t, pacific_tz)
        if due_date == today:
            out.append(t)
    return out


def tasks_for_unscheduled_rollup(tasks: Iterable[Task]) -> list[Task]:
    """Open mine-assigned tasks with no due_at — for the Monday rollup."""
    return [t for t in tasks if _is_mine_and_open(t) and t.due_at is None]


def pending_reminders(
    tasks: Iterable[Task],
    now: datetime,
    already_sent: set[str],
) -> list[dict]:
    """Return reminder payloads for tasks needing a ping right now.

    Each payload:
        {
          "task_id": str,
          "tier": "t_30min" | "t_24h_overdue",
          "dedup_key": f"{task_id}_{tier}",
          "description": str,
          "due_at": str | None,
        }

    ``already_sent`` holds dedup_keys of reminders we've already fired —
    typically loaded from sent-reminders.json. Matching keys are skipped.
    """
    now_utc = _as_utc(now)
    out: list[dict] = []
    for t in tasks:
        if not _is_mine_and_open(t):
            continue
        if t.due_at is None:
            continue

        # T-30min — timed tasks only
        if t.is_timed():
            due_utc = _parse_iso_utc(t.due_at)
            delta = (due_utc - now_utc).total_seconds()
            if 0 < delta <= 30 * 60:
                key = f"{t.id}_{T_30MIN}"
                if key not in already_sent:
                    out.append(_payload(t, T_30MIN, key))

        # T+24h overdue — both timed and all-day
        effective_due_utc = _effective_due_utc(t)
        if effective_due_utc is not None:
            overdue = (now_utc - effective_due_utc).total_seconds()
            if overdue > 24 * 3600:
                key = f"{t.id}_{T_24H_OVERDUE}"
                if key not in already_sent:
                    out.append(_payload(t, T_24H_OVERDUE, key))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _payload(t: Task, tier: str, dedup_key: str) -> dict:
    return {
        "task_id": t.id,
        "tier": tier,
        "dedup_key": dedup_key,
        "description": t.description,
        "due_at": t.due_at,
    }


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso_utc(iso: str) -> datetime:
    """Parse an ISO 8601 datetime (must contain ``T``) and return UTC."""
    s = iso.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _task_local_date(t: Task, tz) -> date:
    """Return the calendar date for display — all-day uses its literal
    date, timed is converted to the display timezone first."""
    if t.is_all_day():
        return t.due_date()  # type: ignore[return-value]
    dt_utc = _parse_iso_utc(t.due_at)  # type: ignore[arg-type]
    return dt_utc.astimezone(tz).date()


def _effective_due_utc(t: Task) -> datetime | None:
    """For the 24-h overdue clock: timed uses its exact instant, all-day
    uses UTC midnight at the *end* of its due date (i.e., midnight UTC of
    the following calendar day).
    """
    if t.due_at is None:
        return None
    if t.is_timed():
        return _parse_iso_utc(t.due_at)
    # all-day
    d = t.due_date()
    if d is None:
        return None
    next_day = d + timedelta(days=1)
    return datetime.combine(next_day, time(0, 0), tzinfo=timezone.utc)
