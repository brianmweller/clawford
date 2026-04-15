"""Tests for the pure helpers in gmessages-mine.py.

gmessages-mine.py is a headless Playwright scraper against
messages.google.com/web. The scrape itself depends on a live
persistent profile, so tests cover only the deterministic
helpers — DOM row → contact dict, and relative-date → yyyy-mm-dd
resolution. The run() entry point is dead-on-arrival without a
paired browser profile, which is the correct behavior (clean
error JSON, exit 0).

Run: cd agents/connector && python3 -m pytest tests/test_gmessages_mine.py -v
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"conn_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def gm():
    return _load_script("gmessages-mine.py")


# ── resolve_relative_date ────────────────────────────────────────────


def test_resolve_today(gm):
    today = date(2026, 4, 14)
    assert gm._resolve_relative_date("2:35 PM", today) == "2026-04-14"
    assert gm._resolve_relative_date("9:02 AM", today) == "2026-04-14"
    # Google Messages sometimes localizes to 24h
    assert gm._resolve_relative_date("14:35", today) == "2026-04-14"


def test_resolve_yesterday(gm):
    today = date(2026, 4, 14)
    assert gm._resolve_relative_date("Yesterday", today) == "2026-04-13"
    assert gm._resolve_relative_date("yesterday", today) == "2026-04-13"


def test_resolve_weekday_names_go_to_prior_week(gm):
    """A bare weekday name refers to the most recent past occurrence.
    Monday April 14 → 'Sunday' = April 13, 'Monday' = April 7 (same day
    means last week, not today)."""
    today = date(2026, 4, 14)  # Tuesday
    assert gm._resolve_relative_date("Monday", today) == "2026-04-13"
    assert gm._resolve_relative_date("Sunday", today) == "2026-04-12"
    assert gm._resolve_relative_date("Saturday", today) == "2026-04-11"
    # Tuesday (today is Tuesday) → must roll back a full week
    assert gm._resolve_relative_date("Tuesday", today) == "2026-04-07"


def test_resolve_month_day_current_year(gm):
    today = date(2026, 4, 14)
    assert gm._resolve_relative_date("Mar 28", today) == "2026-03-28"
    assert gm._resolve_relative_date("Apr 1", today) == "2026-04-01"


def test_resolve_month_day_rolls_back_year(gm):
    """If 'Nov 5' comes up on April 14, 2026, it's from 2025 — not the
    future. Any month > today's month belongs to last year."""
    today = date(2026, 4, 14)
    assert gm._resolve_relative_date("Nov 5", today) == "2025-11-05"
    assert gm._resolve_relative_date("Dec 22", today) == "2025-12-22"


def test_resolve_explicit_mdy_slashes(gm):
    today = date(2026, 4, 14)
    assert gm._resolve_relative_date("3/28/2026", today) == "2026-03-28"
    assert gm._resolve_relative_date("12/22/2025", today) == "2025-12-22"


def test_resolve_mdy_two_digit_year(gm):
    """Google Messages renders older conversations as '1/26/25' (2-digit
    year). 25 → 2025, 99 → 1999 (the standard 1970-pivot heuristic)."""
    today = date(2026, 4, 14)
    assert gm._resolve_relative_date("1/26/25", today) == "2025-01-26"
    assert gm._resolve_relative_date("12/3/24", today) == "2024-12-03"


def test_resolve_relative_recent(gm):
    """'23 min', '5 h', '2 hr', '1 d' all round to today (or today-N for
    days). gmessages-mine only stamps yyyy-mm-dd anyway."""
    today = date(2026, 4, 14)
    assert gm._resolve_relative_date("23 min", today) == today.isoformat()
    assert gm._resolve_relative_date("5 h", today) == today.isoformat()
    assert gm._resolve_relative_date("2 hr", today) == today.isoformat()
    assert gm._resolve_relative_date("11 hrs", today) == today.isoformat()
    assert gm._resolve_relative_date("1 d", today) == "2026-04-13"
    assert gm._resolve_relative_date("3 d", today) == "2026-04-11"


def test_resolve_unparseable_returns_none(gm):
    today = date(2026, 4, 14)
    assert gm._resolve_relative_date("", today) is None
    assert gm._resolve_relative_date("just now", today) == today.isoformat()
    assert gm._resolve_relative_date("some gibberish", today) is None


# ── row dict normalization ──────────────────────────────────────────


def test_normalize_row_keeps_name_and_date(gm):
    row = {"name": "Dan Zylberglejd", "time": "Yesterday"}
    today = date(2026, 4, 14)
    out = gm._row_to_contact(row, today)
    assert out == {
        "name": "Dan Zylberglejd",
        "phone": "",
        "last_message_date": "2026-04-13",
    }


def test_normalize_row_extracts_phone_when_name_is_digits(gm):
    row = {"name": "+1 650-555-1111", "time": "2:35 PM"}
    today = date(2026, 4, 14)
    out = gm._row_to_contact(row, today)
    assert out["phone"] == "+1 650-555-1111"
    assert out["name"] == "+1 650-555-1111"
    assert out["last_message_date"] == "2026-04-14"


def test_normalize_row_skips_unresolvable_time(gm):
    row = {"name": "Whoever", "time": "blargh"}
    today = date(2026, 4, 14)
    assert gm._row_to_contact(row, today) is None


def test_normalize_row_skips_empty_name(gm):
    row = {"name": "", "time": "Yesterday"}
    today = date(2026, 4, 14)
    assert gm._row_to_contact(row, today) is None


# ── main() SCRIPT_CONTRACT — graceful error when profile missing ───


def test_main_returns_error_json_when_profile_missing(gm, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(gm, "PROFILE_DIR", tmp_path / "nonexistent-profile")
    rc = gm.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] in ("error", "degraded")
    assert "profile" in payload.get("alert", payload.get("error", "")).lower()
