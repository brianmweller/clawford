"""Tests for role_timeline_lib — parses self/role-timeline.md and maps
ISO dates to the role the operator was in on that date. Used by the per-role
archive synthesizer to join Workflowy meeting records (role=meetings
with a chronological_date) to the actual role-context they belong to.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from role_timeline_lib import (  # type: ignore
    parse_role_timeline,
    role_for_date,
)


# Canonical fixture matching the self/role-timeline.md draft
_SAMPLE_TIMELINE = """\
# the operator's Role Timeline

Used by the synthesizer blah blah.

## Roles

- **pre-twitch** | before | 2019-05-01 | earlier roles
- **twitch**     | 2019-05-01 | 2020-09-01 | Director, Central Science
- **amazon**     | 2020-09-01 | 2022-04-01 | YETI / Prime Video / WMS
- **netflix**    | 2022-04-01 | 2022-11-01 | Targeted Experience
- **linkedin**   | 2022-11-01 | 2024-01-01 | Flagship Data / FLEX DS
- **between-linkedin-airbnb** | 2024-01-01 | 2024-05-01 | Layoff + job search
- **airbnb**     | 2024-05-01 | 2026-02-01 | Marketplace Data Science
- **post-airbnb** | 2026-02-01 | current  | Clawford / founder mode

## Notes

Irrelevant prose.
"""


# --- parse_role_timeline ---

def test_parse_role_timeline_returns_ordered_ranges(tmp_path: Path):
    path = tmp_path / "role-timeline.md"
    path.write_text(_SAMPLE_TIMELINE, encoding="utf-8")
    ranges = parse_role_timeline(path)
    assert len(ranges) == 8
    assert [r["role"] for r in ranges] == [
        "pre-twitch", "twitch", "amazon", "netflix", "linkedin",
        "between-linkedin-airbnb", "airbnb", "post-airbnb",
    ]


def test_parse_role_timeline_captures_dates_and_label(tmp_path: Path):
    path = tmp_path / "role-timeline.md"
    path.write_text(_SAMPLE_TIMELINE, encoding="utf-8")
    ranges = parse_role_timeline(path)
    twitch = ranges[1]
    assert twitch["role"] == "twitch"
    assert twitch["start"] == "2019-05-01"
    assert twitch["end"] == "2020-09-01"
    assert "Central Science" in twitch["label"]


def test_parse_role_timeline_before_sentinel(tmp_path: Path):
    path = tmp_path / "role-timeline.md"
    path.write_text(_SAMPLE_TIMELINE, encoding="utf-8")
    ranges = parse_role_timeline(path)
    pre = ranges[0]
    assert pre["role"] == "pre-twitch"
    assert pre["start"] == ""   # sentinel for "before time"
    assert pre["end"] == "2019-05-01"


def test_parse_role_timeline_current_sentinel(tmp_path: Path):
    path = tmp_path / "role-timeline.md"
    path.write_text(_SAMPLE_TIMELINE, encoding="utf-8")
    ranges = parse_role_timeline(path)
    post = ranges[-1]
    assert post["role"] == "post-airbnb"
    assert post["start"] == "2026-02-01"
    assert post["end"] == "current"   # resolved at lookup time


def test_parse_role_timeline_ignores_prose(tmp_path: Path):
    """Only lines matching the role-entry format should be parsed.
    Headers, notes, and blank lines should be skipped."""
    path = tmp_path / "role-timeline.md"
    path.write_text(_SAMPLE_TIMELINE + "\n\n## Extra notes\n\nJust some prose.\n", encoding="utf-8")
    ranges = parse_role_timeline(path)
    assert len(ranges) == 8   # no false positives


def test_parse_role_timeline_missing_file_raises(tmp_path: Path):
    import pytest
    with pytest.raises(FileNotFoundError):
        parse_role_timeline(tmp_path / "nope.md")


# --- role_for_date ---

def _ranges():
    return [
        {"role": "pre-twitch", "start": "", "end": "2019-05-01", "label": "x"},
        {"role": "twitch", "start": "2019-05-01", "end": "2020-09-01", "label": "x"},
        {"role": "amazon", "start": "2020-09-01", "end": "2022-04-01", "label": "x"},
        {"role": "netflix", "start": "2022-04-01", "end": "2022-11-01", "label": "x"},
        {"role": "linkedin", "start": "2022-11-01", "end": "2024-01-01", "label": "x"},
        {"role": "between-linkedin-airbnb", "start": "2024-01-01", "end": "2024-05-01", "label": "x"},
        {"role": "airbnb", "start": "2024-05-01", "end": "2026-02-01", "label": "x"},
        {"role": "post-airbnb", "start": "2026-02-01", "end": "current", "label": "x"},
    ]


def test_role_for_date_maps_standard_dates():
    rs = _ranges()
    assert role_for_date("2019-06-15", rs) == "twitch"
    assert role_for_date("2021-03-30", rs) == "amazon"
    assert role_for_date("2022-07-01", rs) == "netflix"
    assert role_for_date("2022-12-01", rs) == "linkedin"
    assert role_for_date("2024-03-15", rs) == "between-linkedin-airbnb"
    assert role_for_date("2024-11-20", rs) == "airbnb"


def test_role_for_date_boundary_start_inclusive_end_exclusive():
    """2019-05-01 is the start of twitch (inclusive), so maps to twitch
    even though it's also the end of pre-twitch (exclusive)."""
    rs = _ranges()
    assert role_for_date("2019-05-01", rs) == "twitch"
    assert role_for_date("2020-09-01", rs) == "amazon"
    assert role_for_date("2024-01-01", rs) == "between-linkedin-airbnb"
    assert role_for_date("2024-05-01", rs) == "airbnb"
    assert role_for_date("2026-02-01", rs) == "post-airbnb"


def test_role_for_date_pre_twitch_sentinel_start():
    rs = _ranges()
    assert role_for_date("2010-01-01", rs) == "pre-twitch"
    assert role_for_date("2018-12-31", rs) == "pre-twitch"


def test_role_for_date_post_airbnb_current_sentinel():
    rs = _ranges()
    # "current" means open-ended to today — any date after 2026-02-01 matches
    assert role_for_date("2026-03-15", rs) == "post-airbnb"
    assert role_for_date("2030-01-01", rs) == "post-airbnb"


def test_role_for_date_partial_iso_date():
    """chronological_date may be YYYY-MM or YYYY (classifier could return
    these). The lookup should still work — treat as YYYY-MM-01 / YYYY-01-01."""
    rs = _ranges()
    assert role_for_date("2021-03", rs) == "amazon"
    assert role_for_date("2022", rs) == "amazon"
    # 2022-01-01 falls in amazon (2020-09-01 to 2022-04-01 exclusive)


def test_role_for_date_empty_returns_unknown():
    rs = _ranges()
    assert role_for_date("", rs) == "unknown"
    assert role_for_date(None, rs) == "unknown"


def test_role_for_date_malformed_returns_unknown():
    rs = _ranges()
    assert role_for_date("not a date", rs) == "unknown"
    assert role_for_date("2026-13-45", rs) == "unknown"


def test_role_for_date_empty_ranges_returns_unknown():
    assert role_for_date("2020-01-01", []) == "unknown"
