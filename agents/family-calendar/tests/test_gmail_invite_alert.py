"""Tests for agents/family-calendar/scripts/gmail-invite-alert.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`family-calendar:gmail-invite-check`. Runs gmail-invite-check.py (the
existing I/O script that fetches new invites), formats each invite as
a Telegram alert, sends via agents.shared.telegram_api. Pure Python —
no LLM needed; the input already has structured fields.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "family-calendar" / "scripts" / "gmail-invite-alert.py"
FIXTURES = Path(__file__).parent / "fixtures" / "morning-briefing"


def _load():
    spec = importlib.util.spec_from_file_location("gmail_invite_alert", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def invites():
    with open(FIXTURES / "gmail-invites.json", encoding="utf-8") as f:
        return json.load(f)


def test_format_invite_includes_subject_date_organizer(mod, invites):
    msg = mod.format_invite(invites[0])
    assert "Team Offsite" in msg
    assert "alice@example.com" in msg
    # Date should be a human-readable day, not raw ISO
    assert "Apr 18" in msg
    assert msg.startswith("\U0001f42d")  # 🐭 prefix


def test_format_invite_without_location(mod, invites):
    """The preschool conference has no location — format shouldn't
    choke or leave '()' empty-parens in the message."""
    msg = mod.format_invite(invites[1])
    assert "parent-teacher conference" in msg
    assert "()" not in msg


def test_run_sends_one_telegram_per_invite(mod, invites, tmp_path, monkeypatch):
    workspace = tmp_path / "family-calendar-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-gmail-invite.json"
    )

    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: invites)

    sent: list[tuple] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append((tok, chat, text)) or True
    )

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 14, 13, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _FrozenDt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["invites_sent"] == 2
    assert len(sent) == 2


def test_run_empty_invites_sends_nothing(mod, tmp_path, monkeypatch):
    workspace = tmp_path / "family-calendar-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-gmail-invite.json"
    )

    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: [])

    sent: list[tuple] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 14, 13, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _FrozenDt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["invites_sent"] == 0
    assert sent == []


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    monkeypatch.setattr(mod, "run", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
    assert payload["alert"].startswith("\U0001f42d")
