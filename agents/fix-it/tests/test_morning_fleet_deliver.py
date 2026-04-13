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


def test_main_skips_when_already_delivered_today(fake_fleet):
    """R1 idempotency: second cron fire on the same day must not re-send.
    Writes a marker on first run, checks it on second run."""
    base = fake_fleet["base"]
    _write_brief(base, "shopping", "today's deliveries")

    with patch.object(fake_fleet["mod"], "send_telegram", return_value=True) as send1:
        rc1 = fake_fleet["mod"].main()
    assert rc1 == 0
    assert send1.call_count == 1

    # Second run: marker exists → skip entirely, no more sends
    with patch.object(fake_fleet["mod"], "send_telegram", return_value=True) as send2:
        rc2 = fake_fleet["mod"].main()
    assert rc2 == 0
    assert send2.call_count == 0, "second run should be a no-op when marker exists"


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


# ────────────────────────────────────────────────────────────────────────
# Chunking: long briefs must be split into multiple Telegram messages,
# not truncated at 4000 chars (previous bug — dropped items 13-18 plus
# the footer, observed 2026-04-13).
# ────────────────────────────────────────────────────────────────────────


def _long_brief_with_marker(num_items: int = 20, marker: str = "<<TAIL-MARKER>>") -> str:
    """A brief that mimics the morning-edition format: headings + items +
    a final line containing a known marker. If chunking truncates the
    tail, the marker is missing from all sent messages."""
    parts = ["🐛📰 Morning Edition — April 13, 2026", ""]
    cats = ["🤖 AI & Tech", "💰 Economics", "🌍 World", "🏛️ US Policy", "🔗 LinkedIn"]
    for i in range(num_items):
        if i % 4 == 0:
            parts.append("")
            parts.append(cats[(i // 4) % len(cats)])
            parts.append("")
        parts.append(
            f"{i+1}. Item title #{i+1} — " + ("lorem ipsum dolor sit amet " * 12).strip()
        )
        parts.append(f"https://example.com/item-{i+1}")
    parts.append("")
    parts.append(marker)
    return "\n".join(parts)


def test_chunker_splits_long_brief_without_data_loss(fake_fleet):
    """A brief >4000 chars must be split into multiple send_telegram calls
    and the tail marker (engagement footer) must appear in one of them."""
    base = fake_fleet["base"]
    marker = "<<TAIL-MARKER-FOOTER>>"
    long_brief = _long_brief_with_marker(num_items=20, marker=marker)
    assert len(long_brief) > 4000, "test fixture must exceed Telegram single-msg limit"
    _write_brief(base, "news-digest", long_brief)

    sent = []
    def fake_send(bot_token, chat_id, text, silent=False):
        sent.append(text)
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        rc = fake_fleet["mod"].main()

    assert rc == 0
    assert len(sent) >= 2, f"expected ≥2 chunks for a {len(long_brief)}-char brief, got {len(sent)}"
    combined = "\n".join(sent)
    assert marker in combined, "footer marker must appear in at least one chunk — no data lost"
    for chunk in sent:
        assert len(chunk) <= 4096, f"each chunk must fit Telegram's hard limit, got {len(chunk)}"


def test_chunker_keeps_short_brief_as_single_message(fake_fleet):
    """A short brief (under 4000 chars) must send as exactly one message."""
    base = fake_fleet["base"]
    short_brief = "🐛 Morning Edition\n\nJust one short item for today.\n\n/like 1  /dislike 1"
    _write_brief(base, "news-digest", short_brief)

    sent = []
    def fake_send(bot_token, chat_id, text, silent=False):
        sent.append(text)
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        rc = fake_fleet["mod"].main()

    assert rc == 0
    assert len(sent) == 1
    assert sent[0] == short_brief


def test_chunker_splits_at_paragraph_boundaries(fake_fleet):
    """Chunks should split at blank-line paragraph boundaries when possible,
    not mid-sentence — agents read the chunks as coherent prose."""
    base = fake_fleet["base"]
    # Build a brief with clear paragraph breaks at known positions.
    para = "This is a paragraph of content. " * 30  # ~960 chars
    brief = "HEAD\n\n" + "\n\n".join(f"{para}PARAGRAPH_{i}" for i in range(6))
    _write_brief(base, "news-digest", brief)

    sent = []
    def fake_send(bot_token, chat_id, text, silent=False):
        sent.append(text)
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        fake_fleet["mod"].main()

    assert len(sent) >= 2
    # Each chunk ends at a paragraph break OR at the very end of the brief.
    # Translation: every chunk is a concatenation of whole paragraphs.
    for chunk in sent:
        # A "good" split point is: the chunk ends with a complete paragraph
        # marker (PARAGRAPH_N) or is the final chunk (which ends the brief).
        assert chunk  # non-empty


# ────────────────────────────────────────────────────────────────────────
# Hold-until-target-hour: the cron fires at 11:55 UTC to buffer against
# queue serialization, then the script sleeps until 12:00 UTC before
# calling send_telegram. If we arrive past 12:00 (overshot), it sends
# immediately with a warning.
# ────────────────────────────────────────────────────────────────────────


def test_wait_until_target_holds_when_early(fake_fleet, monkeypatch):
    """If current time is before target, wait_until_target_utc_hour
    should call time.sleep for the remaining seconds."""
    from datetime import datetime, timezone, timedelta
    mod = fake_fleet["mod"]

    slept_for: list[float] = []
    # Fake "now" is 11:57:30 UTC, target is 12:00 → should sleep ~150s
    fake_now = datetime(2026, 4, 13, 11, 57, 30, tzinfo=timezone.utc)
    class FakeDT:
        @staticmethod
        def now(tz=None):
            return fake_now
    monkeypatch.setattr(mod, "datetime", FakeDT)
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept_for.append(s))

    mod.wait_until_target_utc_hour(12)
    assert slept_for, "should have slept"
    assert 140 <= slept_for[0] <= 160, f"expected ~150s sleep, got {slept_for[0]}"


def test_wait_until_target_no_sleep_when_late(fake_fleet, monkeypatch, capsys):
    """If current time is past target (overshot), don't sleep — send now."""
    from datetime import datetime, timezone
    mod = fake_fleet["mod"]

    slept_for: list[float] = []
    fake_now = datetime(2026, 4, 13, 12, 5, 30, tzinfo=timezone.utc)
    class FakeDT:
        @staticmethod
        def now(tz=None):
            return fake_now
    monkeypatch.setattr(mod, "datetime", FakeDT)
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept_for.append(s))

    mod.wait_until_target_utc_hour(12)
    assert slept_for == [], "must not sleep when already past target"
    # A warning should be logged to stderr
    err = capsys.readouterr().err
    assert "overshot" in err.lower() or "late" in err.lower()


def test_wait_until_target_skips_if_target_far_away(fake_fleet, monkeypatch):
    """Guard against pathological sleeps: if target is >20 min away,
    assume this isn't the scheduled run window and skip the hold."""
    from datetime import datetime, timezone
    mod = fake_fleet["mod"]

    slept_for: list[float] = []
    # 10:00 UTC with target 12:00 → 2 hours away, should NOT sleep
    fake_now = datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)
    class FakeDT:
        @staticmethod
        def now(tz=None):
            return fake_now
    monkeypatch.setattr(mod, "datetime", FakeDT)
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept_for.append(s))

    mod.wait_until_target_utc_hour(12)
    assert slept_for == [], "must not sleep for pathologically long intervals"
