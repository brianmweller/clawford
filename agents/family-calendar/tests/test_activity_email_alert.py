"""Tests for agents/family-calendar/scripts/activity-email-alert.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`family-calendar:activity-email-check`. Runs the existing I/O script
activity-email-check.py, calls agents.shared.llm.infer to classify
each email into {closure | cancellation | action | event | fyi | none},
and sends one Telegram alert per non-"none" item.

LLM classification (per the operator's logic-gate rule) is the right tier here
because the raw email bodies are natural-language marketing copy from
preschool/swim/ballet providers — keyword matching is brittle against
"Spring Recital schedule" vs "Spring Recital cancelled" vs "Sign your
child up for Spring Recital".
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "family-calendar" / "scripts" / "activity-email-alert.py"
FIXTURES = Path(__file__).parent / "fixtures" / "morning-briefing"


def _load():
    spec = importlib.util.spec_from_file_location("activity_email_alert", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def activity_emails():
    with open(FIXTURES / "activity-emails.json", encoding="utf-8") as f:
        return json.load(f)


def _fake_infer_result(urgency: str, summary: str) -> SimpleNamespace:
    """Build a fake llm.infer result matching the real InferResult shape."""
    return SimpleNamespace(
        ok=True,
        text=json.dumps({"urgency": urgency, "summary": summary}),
        error=None,
        input_tokens=0,
        output_tokens=0,
        model="fake",
    )


def test_format_message_closure_has_warning_prefix(mod):
    email = {"source": "Example Preschool", "subject": "x", "body": "y"}
    classification = {"urgency": "closure", "summary": "School closed Apr 17"}
    msg = mod.format_message(email, classification)
    assert msg is not None
    assert msg.startswith("\U0001f42d \u26a0\ufe0f")  # 🐭 ⚠️
    assert "Example Preschool" in msg
    assert "School closed Apr 17" in msg


def test_format_message_action_has_clipboard_prefix(mod):
    email = {"source": "Example Ballet Studio"}
    classification = {"urgency": "action", "summary": "Order costume by Apr 22"}
    msg = mod.format_message(email, classification)
    assert msg.startswith("\U0001f42d \U0001f4cb")  # 🐭 📋


def test_format_message_event_has_pin_prefix(mod):
    email = {"source": "Example Preschool"}
    classification = {"urgency": "event", "summary": "Parent night Apr 25"}
    msg = mod.format_message(email, classification)
    assert msg.startswith("\U0001f42d \U0001f4cc")  # 🐭 📌


def test_format_message_none_suppressed(mod):
    email = {"source": "Example Swim School"}
    classification = {"urgency": "none", "summary": ""}
    msg = mod.format_message(email, classification)
    assert msg is None


def test_run_classifies_and_sends_non_none(
    mod, activity_emails, tmp_path, monkeypatch
):
    """Integration: stub llm.infer to return pre-classified results and
    verify exactly the non-none items get sent."""
    workspace = tmp_path / "family-calendar-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-activity-email.json"
    )
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: activity_emails)

    # Stub the LLM to return a deterministic classification per subject.
    def fake_infer(prompt, *, json_mode=True, timeout=60, model=None, backend=None):
        if "Early Dismissal" in prompt:
            return _fake_infer_result("closure", "Thu Apr 17 dismissal at noon")
        if "costume order" in prompt.lower():
            return _fake_infer_result("action", "Order costume by Apr 22")
        return _fake_infer_result("none", "")

    monkeypatch.setattr(mod, "llm_infer", fake_infer)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 14, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _FrozenDt)

    result = mod.run()

    assert result["status"] == "ok"
    assert result["classified"] == 3
    assert result["sent"] == 2
    assert any("Example Preschool" in t for t in sent)
    assert any("Example Ballet Studio" in t for t in sent)
    assert not any("Example Swim School" in t for t in sent)


def test_run_empty_input_silent(mod, tmp_path, monkeypatch):
    workspace = tmp_path / "family-calendar-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-activity-email.json"
    )
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: [])

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 14, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _FrozenDt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["sent"] == 0
    assert sent == []


def test_run_llm_failure_defaults_to_none_classification(
    mod, activity_emails, tmp_path, monkeypatch
):
    """When llm.infer returns ok=False, each email should quietly fall
    through as 'none' (no crash, no misfiled alerts)."""
    workspace = tmp_path / "family-calendar-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-activity-email.json"
    )
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: activity_emails)

    def failing_infer(prompt, **kw):
        return SimpleNamespace(
            ok=False, text=None, error="LLM down", input_tokens=0, output_tokens=0, model=None
        )

    monkeypatch.setattr(mod, "llm_infer", failing_infer)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 14, 15, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _FrozenDt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["sent"] == 0
    assert result["classified"] == 3
    assert result.get("llm_failures") == 3


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
