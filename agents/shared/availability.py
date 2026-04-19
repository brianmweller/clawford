"""Scheduling-availability calculator — pure function with no GCal dependency.

Given a search window, a proposed meeting length, the operator's scheduling
rules, and the GCal busy blocks for that window, return up to 3 free slots
in chronological order.

Kept pure (no Google auth, no network) so it's testable with frozen fixtures.
The Google-Calendar fetch is a separate thin wrapper in the agent scripts
that call free_slots() with the busy_blocks they just pulled.

Rule semantics:
  - timezone:               IANA name; all local comparisons happen here.
  - working_hours:          { "mon": ["09:00","18:00"], ..., "sat": None }
  - blackout_windows:       [{days: [...], start, end, label}, ...]
  - weekend_policy:         "strict" (never) | "family_only" | "open"
  - min_buffer_minutes:     spacing between candidate slots within a range.
  - preferred_meeting_days: ["tue","wed","thu"] — non-preferred days are
                            skipped unless recipient is family-inner or
                            urgent=True.
  - hard_no_days:           ["2026-04-20", ...] — absolute override, skipped
                            even for family-inner + urgent.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def free_slots(
    search_start: datetime,
    search_end: datetime,
    meeting_length_minutes: int,
    rules: dict,
    busy_blocks: list[tuple[datetime, datetime]],
    recipient_circles: list[str] | None = None,
    urgent: bool = False,
) -> list[tuple[datetime, datetime]]:
    tz = ZoneInfo(rules.get("timezone", "UTC"))
    recipient_circles = recipient_circles or []
    is_family_inner = "family-inner" in recipient_circles

    s_local = search_start.astimezone(tz)
    e_local = search_end.astimezone(tz)

    meeting_dur = timedelta(minutes=meeting_length_minutes)
    min_buffer = timedelta(minutes=rules.get("min_buffer_minutes", 0))
    weekend_policy = rules.get("weekend_policy", "strict")
    preferred_days = set(rules.get("preferred_meeting_days", []))
    hard_no_days = set(rules.get("hard_no_days", []))

    busy_local = [(bs.astimezone(tz), be.astimezone(tz)) for bs, be in busy_blocks]

    slots: list[tuple[datetime, datetime]] = []
    current = s_local.date()
    last = e_local.date()

    while current <= last:
        day_slots = _slots_for_day(
            day=current, tz=tz,
            search_start=s_local, search_end=e_local,
            meeting_dur=meeting_dur, min_buffer=min_buffer,
            rules=rules,
            busy_local=busy_local,
            hard_no_days=hard_no_days,
            preferred_days=preferred_days,
            weekend_policy=weekend_policy,
            is_family_inner=is_family_inner,
            urgent=urgent,
        )
        slots.extend(day_slots)
        if len(slots) >= 3:
            return slots[:3]
        current += timedelta(days=1)

    return slots[:3]


def _slots_for_day(
    *,
    day: date,
    tz: ZoneInfo,
    search_start: datetime,
    search_end: datetime,
    meeting_dur: timedelta,
    min_buffer: timedelta,
    rules: dict,
    busy_local: list[tuple[datetime, datetime]],
    hard_no_days: set[str],
    preferred_days: set[str],
    weekend_policy: str,
    is_family_inner: bool,
    urgent: bool,
) -> list[tuple[datetime, datetime]]:
    if day.isoformat() in hard_no_days:
        return []

    day_abbr = _WEEKDAYS[day.weekday()]
    is_weekend = day.weekday() >= 5
    if is_weekend and weekend_policy == "family_only" and not is_family_inner:
        return []
    if is_weekend and weekend_policy == "strict":
        return []

    if day_abbr not in preferred_days and not (is_family_inner or urgent):
        return []

    wh = rules.get("working_hours", {}).get(day_abbr)
    if not wh:
        return []

    wh_start = _time_on_day(day, wh[0], tz)
    wh_end = _time_on_day(day, wh[1], tz)

    range_start = max(wh_start, search_start)
    range_end = min(wh_end, search_end)
    if range_start >= range_end:
        return []

    unavailable = list(busy_local)
    for bw in rules.get("blackout_windows", []):
        if day_abbr in bw.get("days", []):
            unavailable.append((
                _time_on_day(day, bw["start"], tz),
                _time_on_day(day, bw["end"], tz),
            ))

    free_ranges = _subtract([(range_start, range_end)], unavailable)

    slots: list[tuple[datetime, datetime]] = []
    for fr_start, fr_end in free_ranges:
        slot_start = fr_start
        while slot_start + meeting_dur <= fr_end:
            slots.append((slot_start, slot_start + meeting_dur))
            slot_start = slot_start + meeting_dur + min_buffer
    return slots


def _time_on_day(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=tz)


def _subtract(
    base: list[tuple[datetime, datetime]],
    cuts: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    result = list(base)
    for c_start, c_end in cuts:
        new_result: list[tuple[datetime, datetime]] = []
        for r_start, r_end in result:
            if c_end <= r_start or c_start >= r_end:
                new_result.append((r_start, r_end))
                continue
            if c_start > r_start:
                new_result.append((r_start, c_start))
            if c_end < r_end:
                new_result.append((c_end, r_end))
        result = new_result
    return result
