"""Tests for agents/meetings-coach/scripts/commitment-follow-up.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`meetings-coach:commitment-follow-up`. Runs commitment-tracker.py,
formats an alert when any commitment is overdue or approaching, and
stays silent otherwise.

Pure Python (logic-gate rule): tracker output is already structured
overdue/approaching classification, so no LLM in the compose tier.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "commitment-follow-up.py"
FIXTURES = Path(__file__).parent / "fixtures" / "commitment-follow-up"


def _load():
    spec = importlib.util.spec_from_file_location("commitment_follow_up", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def tracker_mixed():
    with open(FIXTURES / "tracker-mixed.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def tracker_empty():
    with open(FIXTURES / "tracker-empty.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def tracker_all_clear():
    with open(FIXTURES / "tracker-all-clear.json", encoding="utf-8") as f:
        return json.load(f)


# ─── format_message ──────────────────────────────────────────────────


def test_format_message_includes_overdue_section(mod, tracker_mixed):
    msg = mod.format_message(tracker_mixed)
    assert msg is not None
    assert "OVERDUE" in msg
    assert "Alexis" in msg
    assert "Q2 roadmap draft" in msg
    assert "5 days overdue" in msg
    assert "Jay" in msg


def test_format_message_includes_approaching_section(mod, tracker_mixed):
    msg = mod.format_message(tracker_mixed)
    assert "APPROACHING" in msg
    assert "Pat" in msg
    assert "vendor comparison" in msg
    assert "1 days left" in msg


def test_format_message_has_footer_counts(mod, tracker_mixed):
    msg = mod.format_message(tracker_mixed)
    assert "4 open" in msg
    assert "2 overdue" in msg
    assert "1 approaching" in msg


def test_format_message_returns_none_when_no_actionable(mod, tracker_all_clear):
    """All items on-track → no alert. 'Silent if all clear.'"""
    msg = mod.format_message(tracker_all_clear)
    assert msg is None


def test_format_message_returns_none_when_empty_commitments(mod, tracker_empty):
    msg = mod.format_message(tracker_empty)
    assert msg is None


def test_format_message_overdue_only_no_approaching_section(mod):
    """If approaching list is empty, that section header should not
    appear in the output."""
    tracker = {
        "status": "ok",
        "commitments": [
            {
                "id": "c-1",
                "who": "the operator",
                "to_whom": "Kai",
                "what": "Share the draft",
                "by_when": "2026-04-09",
                "status": "overdue",
                "days_info": "6 days overdue",
                "is_overdue": True,
                "is_approaching": False,
            }
        ],
        "summary": {"total": 1, "open": 0, "overdue": 1, "approaching": 0},
    }
    msg = mod.format_message(tracker)
    assert msg is not None
    assert "OVERDUE" in msg
    assert "APPROACHING" not in msg


# ─── run() orchestration ─────────────────────────────────────────────


def test_run_sends_when_items_actionable(mod, tracker_mixed, tmp_path, monkeypatch):
    workspace = tmp_path / "meetings-coach-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-commitment-follow-up.json"
    )
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: tracker_mixed)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 16, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["sent"] == 1
    assert len(sent) == 1
    assert "OVERDUE" in sent[0]


def test_run_silent_when_no_actionable(mod, tracker_all_clear, tmp_path, monkeypatch):
    workspace = tmp_path / "meetings-coach-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-commitment-follow-up.json"
    )
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: tracker_all_clear)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 16, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["sent"] == 0
    assert sent == []


def test_run_handles_tracker_failure(mod, tmp_path, monkeypatch):
    """If commitment-tracker.py fails, return degraded and stay silent
    (don't spam the operator with a corrupted commitments section)."""
    workspace = tmp_path / "meetings-coach-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-commitment-follow-up.json"
    )
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: None)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 16, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "degraded"
    assert result["sent"] == 0
    assert sent == []


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
