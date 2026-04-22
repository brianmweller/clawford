"""Tests for auto-compose.py — pure helpers only.

The subprocess-spawning run_draft_compose is exercised indirectly through
build_draft_compose_cmd (extracted for testability) plus the scheduling
path helpers (default_scheduling_rules_path, compute_default_search_window).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

# auto-compose.py has a dash in the name — import it under an alias.
_spec = importlib.util.spec_from_file_location(
    "auto_compose", _SCRIPTS_DIR / "auto-compose.py"
)
auto_compose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auto_compose)


# --- _build_recruiter_markup ---

def test_recruiter_markup_returns_none_for_non_cold_compose():
    # No fit_assessment means this is a known-sender draft, not cold recruiter.
    parsed = {
        "reply_needed": True,
        "gmail_thread_id": "t-known",
        "person_slug": "jamie-fitzgerald",
    }
    assert auto_compose._build_recruiter_markup(parsed) is None


def test_recruiter_markup_returns_keyboard_for_a_tier():
    parsed = {
        "reply_needed": True,
        "gmail_thread_id": "t-cold-abc",
        "fit_assessment": {"tier": "A", "rationale": "strong"},
    }
    markup = auto_compose._build_recruiter_markup(parsed)
    assert markup is not None
    row = markup["inline_keyboard"][0]
    assert row[0]["text"].startswith("✅")  # ✅
    assert row[0]["callback_data"] == "recruiter:keep:t-cold-abc"
    assert row[1]["callback_data"] == "recruiter:reject:t-cold-abc"


def test_recruiter_markup_returns_none_for_not_a_target():
    parsed = {
        "reply_needed": True,
        "gmail_thread_id": "t-notarget",
        "fit_assessment": {"tier": "not_a_target"},
    }
    assert auto_compose._build_recruiter_markup(parsed) is None


def test_recruiter_markup_returns_none_without_thread_id():
    parsed = {
        "reply_needed": True,
        "fit_assessment": {"tier": "B"},
        # no gmail_thread_id
    }
    assert auto_compose._build_recruiter_markup(parsed) is None


# --- operator_hint threading ---

def test_build_cmd_includes_operator_hint_when_provided():
    cmd = auto_compose.build_draft_compose_cmd(
        thread_id="tid-1",
        slug="jamie-fitzgerald",
        llm_backend="codex",
        json_out=Path("/tmp/out.json"),
        operator_hint="make it warmer",
    )
    assert "--operator-hint" in cmd
    idx = cmd.index("--operator-hint")
    assert cmd[idx + 1] == "make it warmer"


def test_build_cmd_omits_operator_hint_flag_when_none():
    cmd = auto_compose.build_draft_compose_cmd(
        thread_id="tid-1",
        slug="jamie-fitzgerald",
        llm_backend="codex",
        json_out=Path("/tmp/out.json"),
    )
    assert "--operator-hint" not in cmd


def test_build_cmd_omits_operator_hint_flag_when_empty():
    cmd = auto_compose.build_draft_compose_cmd(
        thread_id="tid-1",
        slug="jamie-fitzgerald",
        llm_backend="codex",
        json_out=Path("/tmp/out.json"),
        operator_hint="   ",
    )
    assert "--operator-hint" not in cmd


# --- build_draft_compose_cmd ---

def test_build_cmd_includes_required_args():
    cmd = auto_compose.build_draft_compose_cmd(
        thread_id="tid-1",
        slug="jamie-fitzgerald",
        llm_backend="codex",
        json_out=Path("/tmp/out.json"),
    )
    assert "--person-slug" in cmd
    assert "jamie-fitzgerald" in cmd
    assert "--gmail-thread-id" in cmd
    assert "tid-1" in cmd
    assert "--llm-backend" in cmd
    assert "codex" in cmd
    assert "--json-out" in cmd


def test_build_cmd_no_create_draft_toggle():
    cmd_on = auto_compose.build_draft_compose_cmd(
        thread_id="t", slug="s", llm_backend="codex",
        json_out=Path("/tmp/x.json"), no_create_draft=True,
    )
    cmd_off = auto_compose.build_draft_compose_cmd(
        thread_id="t", slug="s", llm_backend="codex",
        json_out=Path("/tmp/x.json"), no_create_draft=False,
    )
    assert "--no-create-draft" in cmd_on
    assert "--no-create-draft" not in cmd_off


def test_build_cmd_includes_scheduling_rules_when_provided():
    cmd = auto_compose.build_draft_compose_cmd(
        thread_id="t", slug="s", llm_backend="codex",
        json_out=Path("/tmp/x.json"),
        scheduling_rules=Path("/home/openclaw/.clawford/connector-workspace/scheduling.rules.json"),
        search_window="2026-04-22T09:00/2026-05-05T18:00/America/Los_Angeles",
    )
    assert "--scheduling-rules" in cmd
    assert any("scheduling.rules.json" in str(a) for a in cmd)
    assert "--search-window" in cmd
    assert "2026-04-22T09:00/2026-05-05T18:00/America/Los_Angeles" in cmd


def test_build_cmd_cold_inbound_uses_cold_flag_not_slug():
    """Cold-recruiter dispatch: pass --cold-inbound, skip --person-slug."""
    cmd = auto_compose.build_draft_compose_cmd(
        thread_id="t1", slug=None, llm_backend="codex",
        json_out=Path("/tmp/x.json"), cold_inbound=True,
    )
    assert "--cold-inbound" in cmd
    assert "--person-slug" not in cmd


def test_build_cmd_requires_slug_when_not_cold_inbound():
    import pytest
    with pytest.raises(ValueError):
        auto_compose.build_draft_compose_cmd(
            thread_id="t1", slug=None, llm_backend="codex",
            json_out=Path("/tmp/x.json"), cold_inbound=False,
        )


def test_build_cmd_includes_busy_blocks_when_provided():
    cmd = auto_compose.build_draft_compose_cmd(
        thread_id="t", slug="s", llm_backend="codex",
        json_out=Path("/tmp/x.json"),
        busy_blocks=Path("/tmp/busy.json"),
    )
    assert "--busy-blocks" in cmd
    assert any(str(a).endswith("busy.json") for a in cmd)


def test_build_cmd_omits_scheduling_when_not_provided():
    cmd = auto_compose.build_draft_compose_cmd(
        thread_id="t", slug="s", llm_backend="codex",
        json_out=Path("/tmp/x.json"),
    )
    assert "--scheduling-rules" not in cmd
    assert "--search-window" not in cmd
    assert "--busy-blocks" not in cmd


# --- compute_default_search_window ---

def test_default_search_window_shape():
    win = auto_compose.compute_default_search_window(
        "America/Los_Angeles", days_ahead=10,
        now=datetime(2026, 4, 22, 10, 30),
    )
    # Format: ISO-start/ISO-end/tz. Use maxsplit=2 because tz contains '/'.
    parts = win.split("/", 2)
    assert len(parts) == 3
    assert parts[2] == "America/Los_Angeles"
    # Start is tomorrow 09:00
    assert parts[0] == "2026-04-23T09:00"
    # End is now + days_ahead at 18:00 (2026-04-22 + 10 days = 2026-05-02)
    assert parts[1] == "2026-05-02T18:00"


def test_default_search_window_tz_passthrough():
    win = auto_compose.compute_default_search_window(
        "America/New_York", days_ahead=7,
        now=datetime(2026, 4, 22, 10, 30),
    )
    assert win.endswith("/America/New_York")


# --- default_scheduling_rules_path ---

def test_default_scheduling_rules_path_none_when_missing(tmp_path):
    missing = tmp_path / "nope.json"
    assert auto_compose.default_scheduling_rules_path(missing) is None


def test_default_scheduling_rules_path_returns_when_present(tmp_path):
    present = tmp_path / "scheduling.rules.json"
    present.write_text('{"timezone": "America/Los_Angeles"}', encoding="utf-8")
    assert auto_compose.default_scheduling_rules_path(present) == present


def test_load_timezone_from_rules(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text('{"timezone": "America/New_York"}', encoding="utf-8")
    assert auto_compose.load_timezone_from_rules(p) == "America/New_York"


def test_load_timezone_from_rules_defaults_to_pt_when_missing(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text('{}', encoding="utf-8")
    assert auto_compose.load_timezone_from_rules(p) == "America/Los_Angeles"


# --- busy-blocks materialization ---

def test_materialize_busy_blocks_writes_temp_json(tmp_path):
    blocks = [
        {"start": "2026-04-22T16:00:00Z", "end": "2026-04-22T17:00:00Z"},
        {"start": "2026-04-23T18:00:00Z", "end": "2026-04-23T19:30:00Z"},
    ]
    path = auto_compose.materialize_busy_blocks(blocks, dest_dir=tmp_path)
    assert path.exists()
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == blocks


def test_materialize_busy_blocks_returns_none_when_empty(tmp_path):
    assert auto_compose.materialize_busy_blocks([], dest_dir=tmp_path) is None


def test_parse_search_window_roundtrip():
    # Helper to convert the 'ISO-start/ISO-end/tz' window back to
    # tz-aware datetimes, used before calling gcal_freebusy.
    start, end = auto_compose.parse_search_window(
        "2026-04-22T09:00/2026-05-02T18:00/America/Los_Angeles"
    )
    assert start.tzinfo is not None
    assert end.tzinfo is not None
    assert start.year == 2026 and start.month == 4 and start.day == 22
    assert start.hour == 9
    assert end.month == 5 and end.day == 2
    assert end.hour == 18
