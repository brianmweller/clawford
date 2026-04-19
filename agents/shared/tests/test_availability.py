"""availability.free_slots — pure function that returns up to 3 proposable
meeting windows within a search range, respecting:

- scheduling.rules.json (working hours, blackouts, weekend policy, preferred days)
- GCal busy blocks (passed in, not fetched)
- Recipient circles and urgency flag (which bypass preferred_meeting_days)
- A minimum buffer between proposed slots

The GCal fetch is a separate concern, kept out of this function so it stays
testable without Google auth.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agents.shared.availability import free_slots


PT = ZoneInfo("America/Los_Angeles")


def _rules(**overrides):
    """Default rules resembling agents/connector/scheduling.rules.json."""
    base = {
        "timezone": "America/Los_Angeles",
        "working_hours": {
            "mon": ["09:00", "18:00"],
            "tue": ["09:00", "18:00"],
            "wed": ["09:00", "18:00"],
            "thu": ["09:00", "18:00"],
            "fri": ["09:00", "18:00"],
            "sat": None,
            "sun": None,
        },
        "blackout_windows": [],
        "weekend_policy": "family_only",
        "min_buffer_minutes": 15,
        "preferred_meeting_days": ["mon", "tue", "wed", "thu", "fri"],
        "hard_no_days": [],
    }
    base.update(overrides)
    return base


def _pt(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=PT)


# --- working hours baseline ---

def test_empty_calendar_returns_slots_in_working_hours():
    # Mon 2026-04-20, no busy blocks
    start = _pt(2026, 4, 20, 0)
    end = _pt(2026, 4, 20, 23, 59)
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30,
        rules=_rules(),
        busy_blocks=[],
    )
    assert len(slots) >= 1
    for s, e in slots:
        assert s.astimezone(PT).hour >= 9
        assert e.astimezone(PT).hour <= 18


def test_no_slots_before_working_hours_begin():
    start = _pt(2026, 4, 20, 0)
    end = _pt(2026, 4, 20, 9, 0)   # search ends at start of working day
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=[],
    )
    assert slots == []


def test_no_slots_after_working_hours_end():
    start = _pt(2026, 4, 20, 18, 0)
    end = _pt(2026, 4, 20, 23, 59)
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=[],
    )
    assert slots == []


# --- busy blocks subtract ---

def test_busy_block_removes_overlap():
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    busy = [(_pt(2026, 4, 20, 10), _pt(2026, 4, 20, 12))]
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=busy,
    )
    # No slot may overlap the busy window
    for s, e in slots:
        assert not (s < _pt(2026, 4, 20, 12) and e > _pt(2026, 4, 20, 10))


def test_fully_booked_day_returns_no_slots():
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    busy = [(start, end)]
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=busy,
    )
    assert slots == []


# --- blackout windows ---

def test_blackout_window_is_subtracted():
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    rules = _rules(blackout_windows=[{
        "label": "morning focus",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "start": "09:00", "end": "10:30",
    }])
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=rules, busy_blocks=[],
    )
    for s, e in slots:
        # no slot may start before 10:30 PT
        assert s.astimezone(PT) >= _pt(2026, 4, 20, 10, 30)


# --- weekend policy ---

def test_weekend_excluded_by_default():
    # Saturday 2026-04-25
    start = _pt(2026, 4, 25, 0)
    end = _pt(2026, 4, 25, 23, 59)
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=[],
    )
    assert slots == []


def test_weekend_allowed_for_family_inner_recipient():
    # weekend_policy="family_only" means family-inner can book weekends
    start = _pt(2026, 4, 25, 9)
    end = _pt(2026, 4, 25, 18)
    rules = _rules(working_hours={
        **_rules()["working_hours"],
        "sat": ["10:00", "16:00"],  # family weekend window
    })
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=rules, busy_blocks=[],
        recipient_circles=["family-inner"],
    )
    assert len(slots) >= 1


# --- preferred_meeting_days filter ---

def test_preferred_days_filter_excludes_other_days():
    # Monday 2026-04-20, rules restrict to tue/wed/thu only
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    rules = _rules(preferred_meeting_days=["tue", "wed", "thu"])
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=rules, busy_blocks=[],
    )
    assert slots == []


def test_family_inner_bypasses_preferred_days_filter():
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    rules = _rules(preferred_meeting_days=["tue", "wed", "thu"])
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=rules, busy_blocks=[],
        recipient_circles=["family-inner"],
    )
    assert len(slots) >= 1


def test_urgent_flag_bypasses_preferred_days_filter():
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    rules = _rules(preferred_meeting_days=["tue", "wed", "thu"])
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=rules, busy_blocks=[],
        urgent=True,
    )
    assert len(slots) >= 1


# --- hard_no_days ---

def test_hard_no_day_returns_no_slots_even_for_family_inner():
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    rules = _rules(hard_no_days=["2026-04-20"])
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=rules, busy_blocks=[],
        recipient_circles=["family-inner"], urgent=True,
    )
    assert slots == []


# --- meeting length + buffer ---

def test_meeting_length_must_fit_available_slot():
    # Only a 20-min window between busy blocks; 30-min meeting can't fit
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    busy = [
        (_pt(2026, 4, 20, 9),   _pt(2026, 4, 20, 11)),
        (_pt(2026, 4, 20, 11, 20), _pt(2026, 4, 20, 18)),
    ]
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=busy,
    )
    assert slots == []


def test_min_buffer_between_proposed_slots():
    # A 9-hour empty day; with 30-min meeting and 15-min buffer,
    # proposed slots must be spaced ≥ 45 min apart start-to-start.
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 20, 18)
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30,
        rules=_rules(min_buffer_minutes=15),
        busy_blocks=[],
    )
    assert len(slots) >= 2
    for i in range(1, len(slots)):
        gap = (slots[i][0] - slots[i-1][0]).total_seconds() / 60
        assert gap >= 45


# --- top-3 shortlist ---

def test_returns_at_most_three_slots():
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 23, 18)   # Mon-Thu, four full days
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=[],
    )
    assert len(slots) <= 3


def test_returned_slots_are_in_chronological_order():
    start = _pt(2026, 4, 20, 9)
    end = _pt(2026, 4, 23, 18)
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=[],
    )
    for i in range(1, len(slots)):
        assert slots[i][0] >= slots[i-1][1]


# --- timezone handling ---

def test_rules_in_local_timezone_respected_for_utc_inputs():
    # 17:00 UTC on 2026-04-20 = 10:00 PT — should be inside working hours
    start = datetime(2026, 4, 20, 16, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc)
    slots = free_slots(
        search_start=start, search_end=end,
        meeting_length_minutes=30, rules=_rules(), busy_blocks=[],
    )
    assert len(slots) >= 1
