"""Tests for ops/scripts/fleet_alert_gate.py.

The gate replaces "send every 15 min while degraded" with a
state-change detector + sparse reminder ladder (1h / 4h / 24h).

Run: cd agents/shared && python3 -m pytest tests/test_fleet_alert_gate.py -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE_PATH = REPO_ROOT / "ops" / "scripts" / "fleet_alert_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("fleet_alert_gate", GATE_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["fleet_alert_gate"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def gate():
    return _load_gate()


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "fleet-alert-state.json"


def _ts(offset_seconds: int = 0) -> str:
    base = datetime(2026, 4, 19, 20, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state), encoding="utf-8")


def _read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_missing_state_and_ok_suppresses(gate, state_file):
    summary = {"status": "ok", "agents_checked": 6, "agents_ok": 6}
    decision = gate.decide(summary, state_file, now_iso=_ts(0))
    assert decision.send is False
    assert decision.message == ""
    # No state churn needed when nothing is wrong.
    assert not state_file.exists()


def test_first_degraded_sends(gate, state_file):
    summary = {"status": "degraded", "alert": "🦞 fleet health: costco JWT expired"}
    d = gate.decide(summary, state_file, now_iso=_ts(0))
    assert d.send is True
    assert "costco JWT expired" in d.message
    st = _read_state(state_file)
    assert st["status"] == "degraded"
    assert st["alert"] == summary["alert"]
    assert st["first_seen_utc"] == _ts(0)
    assert st["last_notified_utc"] == _ts(0)
    assert st["reminder_index"] == 0


def test_repeat_same_alert_within_1h_suppresses(gate, state_file):
    _write_state(state_file, {
        "status": "degraded",
        "alert": "🦞 fleet health: costco JWT expired",
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(0),
        "reminder_index": 0,
    })
    summary = {"status": "degraded", "alert": "🦞 fleet health: costco JWT expired"}
    d = gate.decide(summary, state_file, now_iso=_ts(30 * 60))  # 30 min later
    assert d.send is False
    # State should not have advanced the reminder_index.
    st = _read_state(state_file)
    assert st["reminder_index"] == 0


def test_reminder_at_1h_sends(gate, state_file):
    _write_state(state_file, {
        "status": "degraded",
        "alert": "🦞 fleet health: costco JWT expired",
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(0),
        "reminder_index": 0,
    })
    summary = {"status": "degraded", "alert": "🦞 fleet health: costco JWT expired"}
    d = gate.decide(summary, state_file, now_iso=_ts(3600))  # exactly 1h
    assert d.send is True
    assert "still degraded" in d.message.lower()
    st = _read_state(state_file)
    assert st["reminder_index"] == 1
    assert st["last_notified_utc"] == _ts(3600)


def test_between_1h_and_4h_suppresses(gate, state_file):
    _write_state(state_file, {
        "status": "degraded",
        "alert": "🦞 fleet health: costco JWT expired",
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(3600),
        "reminder_index": 1,
    })
    summary = {"status": "degraded", "alert": "🦞 fleet health: costco JWT expired"}
    d = gate.decide(summary, state_file, now_iso=_ts(2 * 3600))
    assert d.send is False


def test_reminder_at_4h_sends(gate, state_file):
    _write_state(state_file, {
        "status": "degraded",
        "alert": "🦞 fleet health: costco JWT expired",
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(3600),
        "reminder_index": 1,
    })
    summary = {"status": "degraded", "alert": "🦞 fleet health: costco JWT expired"}
    d = gate.decide(summary, state_file, now_iso=_ts(4 * 3600))
    assert d.send is True
    st = _read_state(state_file)
    assert st["reminder_index"] == 2


def test_reminder_at_24h_sends(gate, state_file):
    _write_state(state_file, {
        "status": "degraded",
        "alert": "🦞 fleet health: costco JWT expired",
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(4 * 3600),
        "reminder_index": 2,
    })
    summary = {"status": "degraded", "alert": "🦞 fleet health: costco JWT expired"}
    d = gate.decide(summary, state_file, now_iso=_ts(24 * 3600))
    assert d.send is True
    st = _read_state(state_file)
    assert st["reminder_index"] == 3


def test_no_alerts_after_24h_reminder(gate, state_file):
    _write_state(state_file, {
        "status": "degraded",
        "alert": "🦞 fleet health: costco JWT expired",
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(24 * 3600),
        "reminder_index": 3,
    })
    summary = {"status": "degraded", "alert": "🦞 fleet health: costco JWT expired"}
    d = gate.decide(summary, state_file, now_iso=_ts(48 * 3600))
    assert d.send is False


def test_alert_text_change_sends_and_resets_ladder(gate, state_file):
    _write_state(state_file, {
        "status": "degraded",
        "alert": "🦞 fleet health: costco JWT expired",
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(0),
        "reminder_index": 2,
    })
    summary = {"status": "degraded", "alert": "🦞 fleet health: connector auth missing"}
    d = gate.decide(summary, state_file, now_iso=_ts(600))
    assert d.send is True
    st = _read_state(state_file)
    assert st["alert"] == summary["alert"]
    assert st["first_seen_utc"] == _ts(600)
    assert st["reminder_index"] == 0


def test_recovery_from_degraded_sends(gate, state_file):
    _write_state(state_file, {
        "status": "degraded",
        "alert": "🦞 fleet health: costco JWT expired",
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(3600),
        "reminder_index": 1,
    })
    summary = {"status": "ok", "agents_checked": 6, "agents_ok": 6}
    d = gate.decide(summary, state_file, now_iso=_ts(90 * 60))  # 1h30m later
    assert d.send is True
    assert "recovered" in d.message.lower()
    # Duration should be reflected so user can see how long the outage lasted.
    assert "1h" in d.message or "90m" in d.message or "90 min" in d.message
    st = _read_state(state_file)
    assert st["status"] == "ok"
    assert st["alert"] is None


def test_ok_when_already_ok_suppresses(gate, state_file):
    _write_state(state_file, {
        "status": "ok",
        "alert": None,
        "first_seen_utc": _ts(0),
        "last_notified_utc": _ts(0),
        "reminder_index": 0,
    })
    summary = {"status": "ok", "agents_checked": 6, "agents_ok": 6}
    d = gate.decide(summary, state_file, now_iso=_ts(3600))
    assert d.send is False


def test_missing_alert_field_uses_error_fallback(gate, state_file):
    # fleet-health.py crashed: status=error, error set, alert optional.
    summary = {"status": "error", "error": "orchestrator crashed", "alert": "🦞 fleet-health crashed: x"}
    d = gate.decide(summary, state_file, now_iso=_ts(0))
    assert d.send is True
    assert "crashed" in d.message
