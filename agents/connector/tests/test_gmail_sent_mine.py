"""Tests for gmail-sent-mine.py and gmail_sent_mine_lib.py.

Walks the operator's Gmail Sent folder and stamps last_interaction on
matching person files. Tests fixture a fake Gmail service so no
network hits fire — mirrors the pattern in test_daily_refresh.py.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
AGENT_DIR = Path(__file__).resolve().parent.parent
SHARED_DIR = AGENT_DIR.parent / "shared"


def _load_module(name: str, path: Path):
    # Ensure shared/ is on sys.path so agents.shared.* imports resolve.
    for p in (SHARED_DIR, AGENT_DIR, SCRIPTS_DIR):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lib():
    return _load_module(
        "gmail_sent_mine_lib", SCRIPTS_DIR / "gmail_sent_mine_lib.py",
    )


@pytest.fixture
def sent_mine():
    return _load_module(
        "gmail_sent_mine_script", SCRIPTS_DIR / "gmail-sent-mine.py",
    )


# ---------------------------------------------------------------------------
# Pure-helper tests
# ---------------------------------------------------------------------------


def test_is_skippable_recipient_flags_noreply(lib):
    assert lib.is_skippable_recipient("noreply@github.com") is True
    assert lib.is_skippable_recipient("no-reply@substack.com") is True
    assert lib.is_skippable_recipient("donotreply@linkedin.com") is True


def test_is_skippable_recipient_flags_bounce_domains(lib):
    assert lib.is_skippable_recipient("abc123@xyz.bounces.google.com") is True


def test_is_skippable_recipient_passes_normal_addresses(lib):
    assert lib.is_skippable_recipient("thomas@example.com") is False
    assert lib.is_skippable_recipient("yendrick.zieleniak@example.com") is False
    assert lib.is_skippable_recipient("") is True  # empty is skippable


def test_extract_recipient_emails_single_to(lib):
    msg = {"payload": {"headers": [
        {"name": "To", "value": "Alice <alice@example.com>"},
    ]}}
    out = lib.extract_recipient_emails(msg, operator_emails=set())
    assert out == {"alice@example.com"}


def test_extract_recipient_emails_merges_to_and_cc(lib):
    msg = {"payload": {"headers": [
        {"name": "To", "value": "alice@example.com"},
        {"name": "Cc", "value": "bob@example.com, carol@example.com"},
    ]}}
    out = lib.extract_recipient_emails(msg, operator_emails=set())
    assert out == {"alice@example.com", "bob@example.com", "carol@example.com"}


def test_extract_recipient_emails_drops_brian_self(lib):
    msg = {"payload": {"headers": [
        {"name": "To", "value": "the operator <operator@example.com>, alice@example.com"},
    ]}}
    out = lib.extract_recipient_emails(
        msg, operator_emails={"operator@example.com"},
    )
    assert out == {"alice@example.com"}


def test_extract_recipient_emails_drops_noreply(lib):
    msg = {"payload": {"headers": [
        {"name": "To", "value": "noreply@github.com, thomas@example.com"},
    ]}}
    out = lib.extract_recipient_emails(msg, operator_emails=set())
    assert out == {"thomas@example.com"}


def test_extract_recipient_pairs_returns_email_with_raw_header(lib):
    """extract_recipient_pairs preserves the raw `Display Name <email>`
    form so callers can pass it to promote_to_people_brain for richer
    slug derivation. Bare emails return empty raw_header."""
    msg = {"payload": {"headers": [
        {"name": "To", "value": "Alice Smith <alice@example.com>, bob@example.com"},
    ]}}
    out = lib.extract_recipient_pairs(msg, operator_emails=set())
    out_dict = {email: raw for email, raw in out}
    assert "alice@example.com" in out_dict
    assert out_dict["alice@example.com"] == "Alice Smith <alice@example.com>"
    assert "bob@example.com" in out_dict
    assert out_dict["bob@example.com"] == "bob@example.com"


def test_extract_recipient_pairs_filters_brian_and_noreply(lib):
    msg = {"payload": {"headers": [
        {"name": "To", "value": "the operator <operator@example.com>, noreply@x.com, Alice <alice@example.com>"},
    ]}}
    out = lib.extract_recipient_pairs(
        msg, operator_emails={"operator@example.com"},
    )
    emails = [e for e, _ in out]
    assert emails == ["alice@example.com"]


def test_internal_date_to_iso_date(lib):
    # Compute the expected epoch from the datetime itself rather than
    # hard-coding — avoids off-by-N drift when computing by hand.
    ms = int(datetime(2026, 4, 21, tzinfo=timezone.utc).timestamp() * 1000)
    assert lib.internal_date_to_iso_date(str(ms)) == "2026-04-21"


def test_internal_date_to_iso_date_invalid_returns_none(lib):
    assert lib.internal_date_to_iso_date(None) is None
    assert lib.internal_date_to_iso_date("") is None
    assert lib.internal_date_to_iso_date("not-a-number") is None


def test_build_gmail_query_uses_fallback_when_no_cursor(lib):
    q = lib.build_gmail_query(
        cursor={}, fallback_days=90, now_iso="2026-04-21T10:00:00Z",
    )
    assert q == "in:sent -in:chats newer_than:90d"


def test_build_gmail_query_uses_after_when_cursor_fresh(lib):
    cursor = {
        "last_internalDate": "1776988800000",
        "last_run_at": "2026-04-21T08:00:00Z",
    }
    q = lib.build_gmail_query(
        cursor=cursor, fallback_days=90, now_iso="2026-04-21T10:00:00Z",
    )
    assert q.startswith("in:sent -in:chats after:")
    assert "1776988800" in q  # seconds, not ms


def test_build_gmail_query_stale_cursor_falls_back(lib):
    cursor = {
        "last_internalDate": "1776988800000",
        "last_run_at": "2026-04-01T08:00:00Z",  # > 48h ago
    }
    q = lib.build_gmail_query(
        cursor=cursor, fallback_days=90, now_iso="2026-04-21T10:00:00Z",
    )
    assert q == "in:sent -in:chats newer_than:90d"


# ---------------------------------------------------------------------------
# run() integration tests — fake Gmail service + tmp people dir
# ---------------------------------------------------------------------------


class _FakeGmailService:
    """Minimal stub mirroring Gmail API's fluent interface. Returns
    canned message list + get responses."""

    def __init__(self, messages: list[dict], *, raises: Exception | None = None):
        self._messages = messages
        self._raises = raises

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, *, userId, q, maxResults):
        if self._raises is not None:
            raise self._raises
        self._last_query = q
        # Gmail API's users().messages().list().execute() returns
        # {"messages": [...], "resultSizeEstimate": N}. Mirror that.
        return _Exec({
            "messages": [{"id": m["id"]} for m in self._messages],
        })

    def get(self, *, userId, id, format, metadataHeaders=None):
        if self._raises is not None:
            raise self._raises
        for m in self._messages:
            if m["id"] == id:
                return _Exec(m)
        return _Exec({})


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def _sent_msg(
    *, mid: str, to: str, cc: str = "", internal_date_ms: int,
) -> dict:
    headers = [{"name": "To", "value": to}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {
        "id": mid,
        "internalDate": str(internal_date_ms),
        "payload": {"headers": headers},
    }


def _write_person(
    people_dir: Path, slug: str, *, email: str,
    last_interaction: str = "2026-01-01",
):
    (people_dir / f"{slug}.md").write_text(
        f"# {slug.replace('-', ' ').title()}\n"
        f"- **slug:** {slug}\n"
        f"- **circles:** professional-outer\n"
        f"- **email:** {email}\n"
        f"- **last_interaction:** {last_interaction}\n",
        encoding="utf-8",
    )


def _apr21_ms(hours_ago: int = 0) -> int:
    """Epoch ms for a recent point in time."""
    # 2026-04-21T10:00:00Z
    base = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
    dt = base - timedelta(hours=hours_ago)
    return int(dt.timestamp() * 1000)


@pytest.fixture
def sandbox(tmp_path):
    people = tmp_path / "people"
    people.mkdir()
    cursor = tmp_path / "cursor.json"
    summary = tmp_path / "summary.json"
    return type("Sandbox", (), {
        "people": people, "cursor": cursor, "summary": summary,
    })()


def test_run_stamps_last_interaction_for_matched_recipient(
    sent_mine, sandbox,
):
    _write_person(sandbox.people, "thomas-example",
                  email="thomas@example.com",
                  last_interaction="2026-01-01")
    svc = _FakeGmailService([
        _sent_msg(mid="m1", to="thomas@example.com",
                  internal_date_ms=_apr21_ms(2)),
    ])
    stats = sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90,
        max_messages=100,
        commit=True,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    assert stats["stamps_written"] == 1
    assert stats["recipients_matched"] == 1
    text = (sandbox.people / "thomas-example.md").read_text(encoding="utf-8")
    assert "- **last_interaction:** 2026-04-21" in text


def test_run_skips_noreply_recipients(sent_mine, sandbox):
    _write_person(sandbox.people, "thomas-example",
                  email="thomas@example.com")
    svc = _FakeGmailService([
        _sent_msg(mid="m1", to="noreply@linkedin.com",
                  internal_date_ms=_apr21_ms(2)),
    ])
    stats = sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90,
        max_messages=100,
        commit=True,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    # Noreply filtered at extract step — doesn't count as unmatched.
    assert stats["recipients_total"] == 0
    assert stats["recipients_matched"] == 0
    assert stats["stamps_written"] == 0


def test_run_cursor_advances_on_success(sent_mine, sandbox):
    _write_person(sandbox.people, "alice-ex", email="alice@ex.com")
    msg_ms = _apr21_ms(1)
    svc = _FakeGmailService([
        _sent_msg(mid="m1", to="alice@ex.com", internal_date_ms=msg_ms),
    ])
    sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90, max_messages=100, commit=True,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    cursor = json.loads(sandbox.cursor.read_text(encoding="utf-8"))
    assert cursor["last_internalDate"] == str(msg_ms)
    assert cursor["last_run_at"] == "2026-04-21T10:00:00Z"


def test_run_cursor_unchanged_on_api_failure(sent_mine, sandbox):
    _write_person(sandbox.people, "alice-ex", email="alice@ex.com")
    # Pre-seed a cursor so we can assert it's unchanged.
    sandbox.cursor.write_text(json.dumps({
        "last_internalDate": "111", "last_run_at": "2026-04-20T10:00:00Z",
    }), encoding="utf-8")
    svc = _FakeGmailService([], raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        sent_mine.run(
            service=svc,
            people_dir=sandbox.people,
            cursor_path=sandbox.cursor,
            summary_path=sandbox.summary,
            window_days=90, max_messages=100, commit=True,
            now_iso="2026-04-21T10:00:00Z",
            operator_emails={"operator@example.com"},
        )
    cursor = json.loads(sandbox.cursor.read_text(encoding="utf-8"))
    assert cursor["last_internalDate"] == "111"
    assert cursor["last_run_at"] == "2026-04-20T10:00:00Z"


def test_run_multi_recipient_stamps_both_matched_slugs(sent_mine, sandbox):
    _write_person(sandbox.people, "alice-ex", email="alice@ex.com",
                  last_interaction="2026-01-01")
    _write_person(sandbox.people, "bob-ex", email="bob@ex.com",
                  last_interaction="2026-01-01")
    svc = _FakeGmailService([
        _sent_msg(mid="m1", to="alice@ex.com", cc="bob@ex.com",
                  internal_date_ms=_apr21_ms(1)),
    ])
    stats = sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90, max_messages=100, commit=True,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    assert stats["stamps_written"] == 2
    for slug in ("alice-ex", "bob-ex"):
        text = (sandbox.people / f"{slug}.md").read_text(encoding="utf-8")
        assert "- **last_interaction:** 2026-04-21" in text


def test_run_unmatched_recipient_no_exception(sent_mine, sandbox, monkeypatch):
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(sandbox.people.parent))
    # No person file for unknown@x.com
    svc = _FakeGmailService([
        _sent_msg(mid="m1", to="unknown@x.com",
                  internal_date_ms=_apr21_ms(1)),
    ])
    stats = sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90, max_messages=100, commit=True,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    assert stats["status"] == "ok"
    # After Phase 2.3, unmatched recipients are auto-promoted instead
    # of silently lost. A stub now exists for unknown@x.com and counts
    # as auto_promoted (recipients_unmatched still tracks the
    # pre-promote count for audit purposes).
    assert stats["recipients_unmatched"] == 1
    assert stats["auto_promoted"] == 1
    assert stats["stamps_written"] == 0
    assert (sandbox.people / "unknown-x-com.md").exists()


def test_run_unmatched_recipient_creates_stub_with_display_name(
    sent_mine, sandbox, monkeypatch,
):
    """Auto-promotion uses the To header's display name to build a
    cleaner slug than the local-domain fallback."""
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(sandbox.people.parent))
    svc = _FakeGmailService([
        _sent_msg(
            mid="m1",
            to="Alyssa Statile <alyssa.statile@coinbase.com>",
            internal_date_ms=_apr21_ms(1),
        ),
    ])
    stats = sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90, max_messages=100, commit=True,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    assert stats["auto_promoted"] == 1
    fp = sandbox.people / "alyssa-statile.md"
    assert fp.exists()
    text = fp.read_text(encoding="utf-8")
    assert "auto_created" in text
    assert "sent_recipient" in text
    assert "alyssa.statile@coinbase.com" in text


def test_run_dry_run_does_not_promote_unmatched(
    sent_mine, sandbox, monkeypatch,
):
    """Dry-run must not write stubs (matches existing dry-run cursor
    behavior)."""
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(sandbox.people.parent))
    svc = _FakeGmailService([
        _sent_msg(mid="m1", to="newperson@example.com",
                  internal_date_ms=_apr21_ms(1)),
    ])
    sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90, max_messages=100, commit=False,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    assert not (sandbox.people / "newperson-example-com.md").exists()


def test_run_dry_run_does_not_stamp_or_persist_cursor(sent_mine, sandbox):
    _write_person(sandbox.people, "alice-ex", email="alice@ex.com",
                  last_interaction="2026-01-01")
    svc = _FakeGmailService([
        _sent_msg(mid="m1", to="alice@ex.com",
                  internal_date_ms=_apr21_ms(1)),
    ])
    sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90, max_messages=100, commit=False,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    text = (sandbox.people / "alice-ex.md").read_text(encoding="utf-8")
    assert "- **last_interaction:** 2026-01-01" in text  # unchanged
    assert not sandbox.cursor.exists()


def test_run_max_merge_declines_older_stamp(sent_mine, sandbox):
    """update_last_interaction is max-merged. If the sent message is
    OLDER than the existing last_interaction (e.g., daily-refresh
    already stamped a fresher date from Krisp), decline the write."""
    _write_person(sandbox.people, "alice-ex", email="alice@ex.com",
                  last_interaction="2026-04-20")  # fresh
    svc = _FakeGmailService([
        # Sent message dated 2026-04-15 — older than the existing stamp.
        _sent_msg(
            mid="m1", to="alice@ex.com",
            internal_date_ms=int(datetime(
                2026, 4, 15, tzinfo=timezone.utc,
            ).timestamp() * 1000),
        ),
    ])
    stats = sent_mine.run(
        service=svc,
        people_dir=sandbox.people,
        cursor_path=sandbox.cursor,
        summary_path=sandbox.summary,
        window_days=90, max_messages=100, commit=True,
        now_iso="2026-04-21T10:00:00Z",
        operator_emails={"operator@example.com"},
    )
    assert stats["stamps_declined_max_merge"] == 1
    assert stats["stamps_written"] == 0
    text = (sandbox.people / "alice-ex.md").read_text(encoding="utf-8")
    assert "- **last_interaction:** 2026-04-20" in text
