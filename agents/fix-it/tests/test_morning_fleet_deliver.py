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
    monkeypatch.setenv("CLAWFORD_WORKSPACE_BASE", str(base))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "TESTCHATID")
    for t in (
        "FAMILYCAL_BOT_TOKEN",
        "MEETINGS_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "SHOPPING_BOT_TOKEN",
        "NEWSDIGEST_BOT_TOKEN",
        "CONNECTOR_BOT_TOKEN",
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


def test_fleet_roster_includes_connector(fake_fleet):
    """Regression guard (2026-04-15): Huckle Cat's gather cron writes
    connector-workspace/cache/morning-brief-ready.txt daily, but for
    several days it was missing from FLEET, so fleet-deliver silently
    skipped it and no morning relationship nudge went out."""
    fleet = fake_fleet["mod"].FLEET
    ids = [row[0] for row in fleet]
    assert "connector" in ids, f"connector missing from FLEET: {ids}"
    connector_row = next(row for row in fleet if row[0] == "connector")
    assert connector_row[1] == "CONNECTOR_BOT_TOKEN"
    assert connector_row[2] == "Huckle Cat"


def test_main_delivers_all_6_when_all_present(fake_fleet):
    base = fake_fleet["base"]
    for agent in (
        "family-calendar",
        "meetings-coach",
        "fix-it",
        "shopping",
        "news-digest",
        "connector",
    ):
        _write_brief(base, agent, f"brief from {agent}")

    with patch.object(fake_fleet["mod"], "send_telegram", return_value=True) as send:
        rc = fake_fleet["mod"].main()

    assert rc == 0
    assert send.call_count == 6
    # Verify each call used a DIFFERENT bot token (not all the same)
    tokens_used = [call.args[0] for call in send.call_args_list]
    assert len(set(tokens_used)) == 6, f"expected 6 distinct bot tokens, got {tokens_used}"
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
    assert len(report["skipped"]) == 6
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


# ────────────────────────────────────────────────────────────────────────
# Per-item inline keyboard delivery (Option B)
#
# news-digest writes cache/morning-items.json alongside the plain text
# brief. When present, morning-fleet-deliver reads it and sends each
# item as its own Telegram message with a [👍 like][👎 dislike][📖 more]
# inline keyboard row. callback_data = "like:N" / "dislike:N" / "more:N"
# — engagement-poller.py parses those out of the session transcript
# after the user taps.
# ────────────────────────────────────────────────────────────────────────


def _write_items(base: Path, agent_id: str, items: list, age_s: float = 0) -> Path:
    ws = base / f"{agent_id}-workspace" / "cache"
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "morning-items.json"
    f.write_text(json.dumps(items), encoding="utf-8")
    if age_s:
        mt = time.time() - age_s
        os.utime(str(f), (mt, mt))
    return f


SAMPLE_ITEMS = [
    {
        "num": 1,
        "category": "🤖 AI & Tech",
        "extended_headline": "Hospitals roll out chatbots to reclaim the first patient interaction.",
        "url": "https://statnews.com/hospitals",
        "source_label": "STAT",
    },
    {
        "num": 2,
        "category": "🤖 AI & Tech",
        "extended_headline": "TSMC headed for another record quarter on AI demand.",
        "url": "https://reuters.com/tsmc",
        "source_label": "Reuters",
    },
    {
        "num": 3,
        "category": "💰 Economics",
        "extended_headline": "Oil climbs above $100 as Hormuz blockade looms.",
        "url": "https://wsj.com/oil",
        "source_label": "WSJ",
    },
]


def test_read_items_returns_ok_for_fresh_file(fake_fleet):
    _write_items(fake_fleet["base"], "news-digest", SAMPLE_ITEMS)
    items, status = fake_fleet["mod"].read_items("news-digest")
    assert status == "ok"
    assert items == SAMPLE_ITEMS


def test_read_items_missing_file(fake_fleet):
    items, status = fake_fleet["mod"].read_items("news-digest")
    assert status == "missing"
    assert items is None


def test_read_items_stale_file(fake_fleet):
    _write_items(fake_fleet["base"], "news-digest", SAMPLE_ITEMS, age_s=3 * 60 * 60)
    items, status = fake_fleet["mod"].read_items("news-digest")
    assert status == "stale"


def test_read_items_empty_list(fake_fleet):
    _write_items(fake_fleet["base"], "news-digest", [])
    items, status = fake_fleet["mod"].read_items("news-digest")
    assert status == "empty"


def test_read_items_malformed_json(fake_fleet):
    ws = fake_fleet["base"] / "news-digest-workspace" / "cache"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "morning-items.json").write_text("not json at all", encoding="utf-8")
    items, status = fake_fleet["mod"].read_items("news-digest")
    assert status == "error"


def test_format_item_message_drops_url_from_text(fake_fleet):
    """The URL now rides on the More button (as a native Telegram URL
    link button), not in the message text. Inline URLs clutter the
    digest and duplicate what the button provides."""
    text = fake_fleet["mod"]._format_item_message(SAMPLE_ITEMS[0])
    assert "1." in text
    assert "Hospitals roll out chatbots" in text
    assert "https://statnews.com/hospitals" not in text
    assert "🤖 AI & Tech" in text
    assert "STAT" in text


def test_format_item_message_suppresses_category_when_show_category_false(fake_fleet):
    """deliver_items_with_buttons passes show_category=False for items
    whose category matches the previous item — avoids repeating the
    '🤖 AI & Tech' header on every consecutive AI-category message."""
    text = fake_fleet["mod"]._format_item_message(SAMPLE_ITEMS[1], show_category=False)
    assert "🤖 AI & Tech" not in text
    # Headline + num + source_label still render; URL lives on the button.
    assert "2." in text
    assert "TSMC" in text
    assert "https://" not in text


NUDGE_ITEMS = [
    {"type": "overview", "text": "🐱🤝 Relationship Check\n7 overdue · 179 tracked"},
    {"type": "group_header", "group": "family", "text": "👪 FAMILY (2)"},
    {"type": "person", "slug": "kai-rivera", "group": "family",
     "text": "Kai Rivera — 64 days · WhatsApp"},
    {"type": "person", "slug": "robin-rivera", "group": "family",
     "text": "Robin Rivera — 64 days · email"},
    {"type": "group_header", "group": "friends", "text": "🤝 FRIENDS (1)"},
    {"type": "person", "slug": "alice-hyun", "group": "friends",
     "text": "Alice Hyun — 50 days · iMessage"},
]


def test_deliver_nudge_sends_one_message_per_item(fake_fleet):
    sent_args = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None, **_):
        sent_args.append({"text": text, "reply_markup": reply_markup})
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        sent, failed = fake_fleet["mod"].deliver_nudge_items_with_buttons(
            "FAKE_TOKEN", "CHAT", NUDGE_ITEMS, footer_text=None,
        )
    assert failed == 0
    assert sent == len(NUDGE_ITEMS)
    # Overview and group headers have NO buttons
    assert sent_args[0]["reply_markup"] is None  # overview
    assert sent_args[1]["reply_markup"] is None  # family header
    # Person messages have nudge buttons
    assert sent_args[2]["reply_markup"] is not None  # family person 1
    assert sent_args[3]["reply_markup"] is not None  # family person 2
    assert sent_args[4]["reply_markup"] is None  # friends header
    assert sent_args[5]["reply_markup"] is not None  # Alice


def test_deliver_nudge_person_items_sent_with_html_parse_mode(fake_fleet):
    """2026-04-21: morning-relationship-nudge emits clickable mailto:/
    tel: anchors embedded in each person item's text. Telegram only
    renders them as links when the sendMessage call carries
    parse_mode=HTML — without it the raw tags arrive as unrendered
    markup. Overview + group_header items should NOT set parse_mode
    (they carry no HTML; keeping it unset avoids accidentally
    forcing HTML parsing on stray '<' in future copy).

    This is the contract deliver_nudge_items_with_buttons must uphold
    for the Huckle clickable-contacts feature to work end-to-end."""
    calls = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None,
                  parse_mode=None):
        calls.append({"text": text, "parse_mode": parse_mode})
        return True

    items = [
        {"type": "overview", "text": "\U0001f431\U0001f91d overview"},
        {"type": "group_header", "text": "\U0001f46a FAMILY (1)"},
        {"type": "person", "slug": "bob",
         "text": 'Bob — 30 days · <a href="tel:+14155551234">+14155551234</a>'},
    ]
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        fake_fleet["mod"].deliver_nudge_items_with_buttons(
            "FAKE_TOKEN", "CHAT", items, footer_text=None,
        )
    assert calls[0]["parse_mode"] is None, "overview should not force HTML"
    assert calls[1]["parse_mode"] is None, "group header should not force HTML"
    assert calls[2]["parse_mode"] == "HTML", (
        "person item with anchor must be sent parse_mode=HTML"
    )


def test_deliver_nudge_person_buttons_use_nudge_callback_data(fake_fleet):
    sent_args = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None, **_):
        sent_args.append(reply_markup)
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        fake_fleet["mod"].deliver_nudge_items_with_buttons(
            "FAKE_TOKEN", "CHAT",
            [{"type": "person", "slug": "kai-rivera", "text": "..."}],
            footer_text=None,
        )
    markup = sent_args[0]
    assert markup is not None
    row = markup["inline_keyboard"][0]
    cbs = {b["callback_data"] for b in row}
    assert cbs == {
        "nudge_done:kai-rivera",
        "nudge_snooze:kai-rivera",
        "nudge_ignore:kai-rivera",
    }


def test_deliver_nudge_skips_items_with_empty_text(fake_fleet):
    sent_args = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None, **_):
        sent_args.append(text)
        return True
    items = [
        {"type": "overview", "text": "real overview"},
        {"type": "group_header", "text": ""},  # empty — skipped
        {"type": "person", "slug": "x", "text": "Xavier — 30 days"},
    ]
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        sent, failed = fake_fleet["mod"].deliver_nudge_items_with_buttons(
            "FAKE_TOKEN", "CHAT", items, footer_text=None,
        )
    assert sent == 2  # overview + person, header skipped
    assert sent_args == ["real overview", "Xavier — 30 days"]


def test_deliver_items_shows_category_only_on_section_transition(fake_fleet):
    """Items 1 and 2 share category '🤖 AI & Tech'; item 3 switches
    to '💰 Economics'. The delivery loop must print the category
    header on items 1 and 3 only — NOT on item 2."""
    sent_texts = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None):
        sent_texts.append(text)
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        fake_fleet["mod"].deliver_items_with_buttons(
            "FAKE_TOKEN", "CHAT", SAMPLE_ITEMS, footer_text=None,
        )
    # Item 1 (first AI) — shows category
    assert "🤖 AI & Tech" in sent_texts[0]
    # Item 2 (continues AI) — does NOT show category
    assert "🤖 AI & Tech" not in sent_texts[1]
    # Item 3 (switches to Economics) — shows category
    assert "💰 Economics" in sent_texts[2]


def test_buttons_for_item_more_is_url_link(fake_fleet):
    """The 'more' button now navigates to the article URL via Telegram's
    native URL-button (no callback round-trip). like/dislike stay as
    callback_data buttons routed to record_engagement as before."""
    buttons = fake_fleet["mod"]._buttons_for_item(
        5, url="https://example.com/article"
    )
    row = buttons["inline_keyboard"][0]
    by_label = {b["text"]: b for b in row}

    like_btn = next(b for b in row if "like" in b["text"].lower() and "dislike" not in b["text"].lower())
    assert like_btn.get("callback_data") == "like:5"
    assert "url" not in like_btn

    dislike_btn = next(b for b in row if "dislike" in b["text"].lower())
    assert dislike_btn.get("callback_data") == "dislike:5"
    assert "url" not in dislike_btn

    more_btn = next(b for b in row if "more" in b["text"].lower())
    assert more_btn.get("url") == "https://example.com/article"
    # The 'more' button must NOT carry a callback_data anymore —
    # Telegram rejects buttons that have both url and callback_data.
    assert "callback_data" not in more_btn


def test_buttons_for_item_omits_more_when_no_url(fake_fleet):
    """Without a URL there's nothing for 'more' to open, so omit it
    rather than emitting a broken button."""
    buttons = fake_fleet["mod"]._buttons_for_item(5, url="")
    row = buttons["inline_keyboard"][0]
    labels = [b["text"].lower() for b in row]
    assert any("like" in l and "dislike" not in l for l in labels)
    assert any("dislike" in l for l in labels)
    assert not any("more" in l for l in labels)
    assert len(row) == 2


def test_deliver_items_sends_one_call_per_item_with_buttons(fake_fleet):
    sent_args = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None):
        sent_args.append({
            "text": text,
            "silent": silent,
            "reply_markup": reply_markup,
        })
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        sent, failed = fake_fleet["mod"].deliver_items_with_buttons(
            "FAKE_TOKEN", "CHAT", SAMPLE_ITEMS, footer_text="🐛 done"
        )
    assert failed == 0
    assert sent == len(SAMPLE_ITEMS) + 1  # 3 items + 1 footer
    # Every non-footer call has a reply_markup
    item_calls = sent_args[:-1]
    assert all(c["reply_markup"] is not None for c in item_calls), (
        "every per-item send must carry an inline keyboard"
    )
    # Footer call has NO reply_markup (it's just a summary message)
    assert sent_args[-1]["reply_markup"] is None


def test_deliver_items_silences_all_but_footer(fake_fleet):
    sent_args = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None):
        sent_args.append({"silent": silent})
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        fake_fleet["mod"].deliver_items_with_buttons(
            "FAKE_TOKEN", "CHAT", SAMPLE_ITEMS, footer_text="🐛 done"
        )
    # All item sends silent, footer audible
    for i, c in enumerate(sent_args[:-1]):
        assert c["silent"] is True, f"item {i} should be silent"
    assert sent_args[-1]["silent"] is False, "footer should be audible"


def test_deliver_items_no_footer_last_item_audible(fake_fleet):
    """When no footer, the last item itself is the one audible ding."""
    sent_args = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None):
        sent_args.append({"silent": silent})
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        fake_fleet["mod"].deliver_items_with_buttons(
            "FAKE_TOKEN", "CHAT", SAMPLE_ITEMS,  # no footer
        )
    assert sent_args[-1]["silent"] is False
    for c in sent_args[:-1]:
        assert c["silent"] is True


def test_main_dispatches_news_digest_to_items_path(fake_fleet):
    """When morning-items.json exists, news-digest is delivered per-item
    with buttons, NOT via plain text chunks."""
    base = fake_fleet["base"]
    _write_items(base, "news-digest", SAMPLE_ITEMS)
    # Still write a plain text brief so the path NOT taken can be
    # distinguished: if send_telegram is called with the text brief body,
    # the items path was bypassed.
    _write_brief(base, "news-digest", "PLAIN TEXT BRIEF BODY")

    call_kwargs: list[dict] = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None):
        call_kwargs.append({
            "text": text,
            "reply_markup": reply_markup,
        })
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        rc = fake_fleet["mod"].main()

    assert rc == 0
    # News-digest sent 3 items + 1 footer = 4 messages
    nd_calls = [c for c in call_kwargs if "PLAIN TEXT BRIEF BODY" not in c["text"]]
    assert len(nd_calls) == 4
    # First 3 have inline keyboards
    assert all(
        c["reply_markup"] is not None
        for c in nd_calls[:3]
    )


def test_main_falls_back_to_text_when_items_missing(fake_fleet):
    """If morning-items.json is missing, news-digest uses the plain
    text brief path (backwards compat)."""
    base = fake_fleet["base"]
    _write_brief(base, "news-digest", "JUST PLAIN TEXT, NO ITEMS FILE")

    call_kwargs: list[dict] = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None):
        call_kwargs.append({"text": text, "reply_markup": reply_markup})
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        rc = fake_fleet["mod"].main()

    assert rc == 0
    # Only plain-text news-digest message, no buttons
    nd_calls = [c for c in call_kwargs if "JUST PLAIN TEXT" in c["text"]]
    assert len(nd_calls) == 1
    assert nd_calls[0]["reply_markup"] is None


def test_main_other_agents_still_use_plain_text(fake_fleet):
    """Option B is news-digest-only. Mistress Mouse, Sergeant Murphy,
    Mr Fixit, Hilda Hippo still deliver plain text briefs with no
    inline keyboards."""
    base = fake_fleet["base"]
    for agent in ("family-calendar", "meetings-coach", "fix-it", "shopping"):
        _write_brief(base, agent, f"plain text from {agent}")
    # news-digest has NO items file and NO text file → skipped

    call_kwargs: list[dict] = []
    def fake_send(bot_token, chat_id, text, silent=False, reply_markup=None):
        call_kwargs.append({"reply_markup": reply_markup})
        return True
    with patch.object(fake_fleet["mod"], "send_telegram", side_effect=fake_send):
        fake_fleet["mod"].main()

    assert len(call_kwargs) == 4
    # None of the 4 other agents attach inline keyboards
    assert all(c["reply_markup"] is None for c in call_kwargs)
