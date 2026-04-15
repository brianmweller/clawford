"""Tests for agents/fix-it/scripts/probation-end-reminder.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:probation-end-reminder`. One-shot: reads probation.md, counts
failure log entries, sends a Telegram message with the count and the
verdict prompt. Does not propose a verdict — that's the operator's call.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "fix-it" / "scripts" / "probation-end-reminder.py"
FIXTURES = Path(__file__).parent / "fixtures" / "probation-end-reminder"


def _load():
    spec = importlib.util.spec_from_file_location("probation_end_reminder", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


# ─── count_failures ──────────────────────────────────────────────────


def test_count_failures_with_three_entries(mod):
    text = (FIXTURES / "probation-with-failures.md").read_text(encoding="utf-8")
    assert mod.count_failures(text) == 3


def test_count_failures_empty_log(mod):
    text = (FIXTURES / "probation-clean.md").read_text(encoding="utf-8")
    assert mod.count_failures(text) == 0


def test_count_failures_ignores_lines_outside_failure_log(mod):
    """A `- YYYY-MM-DD ... |` style line elsewhere in the file (e.g. in
    the criteria table) must NOT be counted as a failure."""
    text = (
        "## Probation criteria\n"
        "- 2026-04-01 00:00 | P1 | not a failure, just criteria text\n"
        "## Failure log\n"
        "- 2026-04-13 09:42 | P4 | a real failure\n"
        "## Verdict\n"
    )
    assert mod.count_failures(text) == 1


# ─── format_message ──────────────────────────────────────────────────


def test_format_message_includes_count_and_verdict_prompt(mod):
    msg = mod.format_message(3)
    assert "3" in msg
    assert "verdict" in msg.lower() or "keep" in msg.lower()


# ─── run() ────────────────────────────────────────────────────────────


def test_run_sends_message_with_count(mod, tmp_path, monkeypatch):
    probation = tmp_path / "probation.md"
    probation.write_text(
        (FIXTURES / "probation-with-failures.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    workspace = tmp_path / "fix-it-workspace"
    (workspace / "cache").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-probation-end.json"
    )
    monkeypatch.setattr(mod, "PROBATION_FILE", probation)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 25, 16, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["failures"] == 3
    assert result["sent"] == 1
    assert "3" in sent[0]


def test_run_handles_missing_probation_file(mod, tmp_path, monkeypatch):
    workspace = tmp_path / "fix-it-workspace"
    (workspace / "cache").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-probation-end.json"
    )
    monkeypatch.setattr(mod, "PROBATION_FILE", tmp_path / "missing.md")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["status"] == "degraded"
    assert sent == []


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
