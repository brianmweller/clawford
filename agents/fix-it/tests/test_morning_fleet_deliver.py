"""Tests for morning-fleet-deliver.py — Option D P3 fleet delivery orchestrator.

The script reads each agent's cache/morning-brief-ready.txt, sends via
the agent's dedicated bot token, and reports delivery status.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_deliver():
    path = SCRIPTS_DIR / "morning-fleet-deliver.py"
    spec = importlib.util.spec_from_file_location("morning_fleet_deliver", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_fleet(tmp_path, monkeypatch):
    """Set up a tmp WORKSPACE_BASE with cache files for each fleet agent."""
    base = tmp_path / "openclaw"
    base.mkdir()
    # Set env before loading the module (module reads env at import time)
    monkeypatch.setenv("OPENCLAW_WORKSPACE_BASE", str(base))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "TESTCHATID")
    for t in (
        "FAMILYCAL_BOT_TOKEN",
        "MEETINGS_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "SHOPPING_BOT_TOKEN",
        "NEWSDIGEST_BOT_TOKEN",
    ):
        monkeypatch.setenv(t, "FAKE_" + t)
    mod = _load_deliver()
    # Ensure the module picked up the tmp base
    mod.WORKSPACE_BASE = str(base)
    return {"mod": mod, "base": base}


def _write_brief(base: Path, agent_id: str, text: str, age_s: float = 0):
    ws = base / f"{agent_id}-workspace" / "cache"
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "morning-brief-ready.txt"
    f.write_text(text, encoding="utf-8")
    if age_s:
        mt = time.time() - age_s
        os.utime(str(f), (mt, mt))
    return f


def test_read_brief_returns_ok_for_fresh_file(fake_fleet):
    _write_brief(fake_fleet["base"], "family-calendar", "Good morning!")
    text, status = fake_fleet["mod"].read_brief("family-calendar")
    assert status == "ok"
    assert text == "Good morning!"


def test_read_brief_missing_file(fake_fleet):
    text, status = fake_fleet["mod"].read_brief("shopping")
    assert status == "missing"
    assert text is None


def test_read_brief_stale_file(fake_fleet):
    _write_brief(fake_fleet["base"], "news-digest", "yesterday's news", age_s=3 * 60 * 60)
    text, status = fake_fleet["mod"].read_brief("news-digest")
    assert status == "stale"
    assert text is None


def test_read_brief_empty_file(fake_fleet):
    _write_brief(fake_fleet["base"], "meetings-coach", "   \n  ")
    text, status = fake_fleet["mod"].read_brief("meetings-coach")
    assert status == "empty"


def test_main_delivers_all_5_when_all_present(fake_fleet):
    base = fake_fleet["base"]
    for agent in ("family-calendar", "meetings-coach", "fix-it", "shopping", "news-digest"):
        _write_brief(base, agent, f"brief from {agent}")

    with patch.object(fake_fleet["mod"], "send_telegram", return_value=True) as send:
        rc = fake_fleet["mod"].main()

    assert rc == 0
    assert send.call_count == 5
    # Verify each call used a DIFFERENT bot token (not all the same)
    tokens_used = [call.args[0] for call in send.call_args_list]
    assert len(set(tokens_used)) == 5, f"expected 5 distinct bot tokens, got {tokens_used}"
    # Every token should start with FAKE_ (from the fixture)
    assert all(t.startswith("FAKE_") for t in tokens_used)


def test_main_returns_zero_when_no_cache_files(fake_fleet, capsys):
    """No briefs to send → exit 0 silently. No agents gathered today."""
    rc = fake_fleet["mod"].main()
    assert rc == 0
    # Report should be emitted
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["delivered"] == []
    assert len(report["skipped"]) == 5
    assert all(reason == "missing" for _, reason in report["skipped"])


def test_main_partial_failure_returns_one(fake_fleet):
    """One bot token missing → partial failure, exit 1."""
    base = fake_fleet["base"]
    for agent in ("family-calendar", "meetings-coach"):
        _write_brief(base, agent, f"brief from {agent}")

    # Remove one bot token from env
    os.environ.pop("MEETINGS_BOT_TOKEN", None)

    with patch.object(fake_fleet["mod"], "send_telegram", return_value=True):
        rc = fake_fleet["mod"].main()

    assert rc == 1  # partial failure


def test_main_writes_consumed_marker_on_success(fake_fleet):
    """After successful send, a .consumed sibling file appears so the
    message isn't accidentally resent."""
    base = fake_fleet["base"]
    _write_brief(base, "shopping", "today's deliveries")

    with patch.object(fake_fleet["mod"], "send_telegram", return_value=True):
        fake_fleet["mod"].main()

    consumed = base / "shopping-workspace" / "cache" / "morning-brief-ready.consumed"
    assert consumed.exists()


def test_main_does_not_send_to_wrong_bot_if_token_missing(fake_fleet):
    """If a bot token isn't in env, skip that agent and don't substitute
    another token."""
    base = fake_fleet["base"]
    _write_brief(base, "family-calendar", "brief")
    os.environ.pop("FAMILYCAL_BOT_TOKEN", None)

    sent_tokens = []
    def fake_send(bot_token, chat_id, text, silent=False):
        sent_tokens.append(bot_token)
        return True

    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        rc = fake_fleet["mod"].main()

    assert rc == 2  # no deliveries, 1 failure
    assert sent_tokens == []  # never called because token missing
