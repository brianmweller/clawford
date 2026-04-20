"""Tests for agents/connector/scripts/gmail-facts-mine.py.

Daily cron that scans the last window of Gmail inbox + sent mail,
calls the shared fact-extraction helper, and writes durable facts to
the Huckle brain via upsert_fact().

Pure helpers live in gmail_facts_mine_lib.py; the thin orchestrator
pulls from Gmail and wires everything together.

TDD: tests land before implementation.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"gfm_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def lib():
    return _load_script("gmail_facts_mine_lib.py")


@pytest.fixture
def miner():
    return _load_script("gmail-facts-mine.py")


# ─── Cursor I/O ──────────────────────────────────────────────────────


def test_load_cursor_returns_empty_dict_when_absent(lib, tmp_path):
    path = tmp_path / "cursor.json"
    assert lib.load_cursor(path) == {}


def test_load_cursor_returns_parsed_dict(lib, tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps({"last_internalDate": "123", "last_run_at": "x"}))
    c = lib.load_cursor(path)
    assert c["last_internalDate"] == "123"


def test_save_cursor_atomic_tmp_replace(lib, tmp_path):
    path = tmp_path / "cursor.json"
    lib.save_cursor(path, {"last_internalDate": "999", "last_run_at": "2026-04-19T09:00:00Z"})
    # tmp file should not linger
    assert not (tmp_path / "cursor.json.tmp").exists()
    loaded = json.loads(path.read_text())
    assert loaded["last_internalDate"] == "999"


# ─── Fallback window logic ───────────────────────────────────────────


def test_use_fallback_when_no_cursor(lib):
    assert lib.should_use_fallback_window({}, now_iso="2026-04-19T09:00:00Z") is True


def test_use_fallback_when_cursor_older_than_48h(lib):
    cursor = {"last_run_at": "2026-04-15T09:00:00Z"}
    assert lib.should_use_fallback_window(
        cursor, now_iso="2026-04-19T09:00:00Z"
    ) is True


def test_no_fallback_when_cursor_recent(lib):
    cursor = {"last_run_at": "2026-04-18T22:00:00Z"}
    assert lib.should_use_fallback_window(
        cursor, now_iso="2026-04-19T09:00:00Z"
    ) is False


def test_use_fallback_when_last_run_missing(lib):
    cursor = {"last_internalDate": "123"}  # no last_run_at
    assert lib.should_use_fallback_window(
        cursor, now_iso="2026-04-19T09:00:00Z"
    ) is True


# ─── Gmail query construction ────────────────────────────────────────


def test_build_query_uses_fallback_days_when_no_cursor(lib):
    q = lib.build_gmail_query(cursor={}, fallback_days=1, now_iso="2026-04-19T09:00:00Z")
    assert "newer_than:1d" in q
    # default mines both inbox and sent mail
    assert "in:inbox" in q or "in:anywhere" in q or "-in:chats" in q


def test_build_query_uses_internaldate_when_cursor_exists(lib):
    q = lib.build_gmail_query(
        cursor={"last_internalDate": "1713500000000",
                "last_run_at": "2026-04-19T07:00:00Z"},
        fallback_days=1,
        now_iso="2026-04-19T09:00:00Z",
    )
    # Gmail's query supports `after:` with an epoch-seconds value
    assert "after:" in q


# ─── Candidate slug derivation ───────────────────────────────────────


def _fake_msg(from_email: str, to_emails: list[str] = None,
              internal_date: str = "1713500000000", message_id: str = "m1",
              body: str = "body") -> dict:
    headers = [
        {"name": "From", "value": from_email},
        {"name": "To", "value": ", ".join(to_emails or [])},
        {"name": "Date", "value": "Sun, 19 Apr 2026 09:00:00 +0000"},
        {"name": "Subject", "value": "Hi"},
    ]
    return {
        "id": message_id,
        "internalDate": internal_date,
        "payload": {
            "headers": headers,
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
        "_body": body,  # testing shortcut — orchestrator passes this
    }


def test_build_candidate_slugs_maps_from_to_cc(lib):
    msg = _fake_msg(
        from_email="sarah@example.com",
        to_emails=["sam.smith@example.com", "mike@example.com"],
    )
    email_to_slug = {
        "sarah@example.com": "sarah-chen",
        "mike@example.com": "mike-jones",
    }
    slugs = lib.build_candidate_slugs(
        msg, email_to_slug=email_to_slug,
        operator_emails={"sam.smith@example.com"},
    )
    assert slugs == {"sarah-chen", "mike-jones"}


def test_build_candidate_slugs_filters_brian(lib):
    msg = _fake_msg(
        from_email="sam.smith@example.com",
        to_emails=["sarah@example.com"],
    )
    email_to_slug = {"sarah@example.com": "sarah-chen"}
    slugs = lib.build_candidate_slugs(
        msg, email_to_slug=email_to_slug,
        operator_emails={"sam.smith@example.com"},
    )
    assert slugs == {"sarah-chen"}


def test_build_candidate_slugs_filters_unknown_emails(lib):
    msg = _fake_msg(
        from_email="stranger@example.com",
        to_emails=["sarah@example.com"],
    )
    email_to_slug = {"sarah@example.com": "sarah-chen"}
    slugs = lib.build_candidate_slugs(
        msg, email_to_slug=email_to_slug,
        operator_emails={"sam.smith@example.com"},
    )
    assert slugs == {"sarah-chen"}


def test_build_candidate_slugs_returns_empty_when_all_filtered(lib):
    msg = _fake_msg(
        from_email="stranger@example.com",
        to_emails=["alsostranger@example.com"],
    )
    slugs = lib.build_candidate_slugs(
        msg, email_to_slug={},
        operator_emails={"sam.smith@example.com"},
    )
    assert slugs == set()


# ─── Message metadata extraction ─────────────────────────────────────


def test_message_metadata_detects_outbound(lib):
    msg = _fake_msg(
        from_email="sam.smith@example.com",
        to_emails=["sarah@example.com"],
    )
    meta = lib.message_metadata(msg, operator_emails={"sam.smith@example.com"})
    assert meta["direction"] == "outbound"
    assert meta["message_id"] == "m1"
    assert meta["from_email"] == "sam.smith@example.com"


def test_message_metadata_detects_inbound(lib):
    msg = _fake_msg(
        from_email="sarah@example.com",
        to_emails=["sam.smith@example.com"],
    )
    meta = lib.message_metadata(msg, operator_emails={"sam.smith@example.com"})
    assert meta["direction"] == "inbound"


# ─── Orchestrator run() ──────────────────────────────────────────────


def _fake_extract_returns(facts: list[dict]):
    """Returns a callable that matches extract_facts_from_text signature."""
    def _fn(**kw):
        return facts
    return _fn


def test_run_dry_run_does_not_write_facts(miner, tmp_path, monkeypatch):
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    workspace = tmp_path / "workspace"
    (workspace / "cache").mkdir(parents=True)
    people_dir.mkdir(parents=True)
    # A people file so email->slug has an entry
    (people_dir / "sarah-chen.md").write_text(
        "# Sarah Chen\n\n- **slug:** sarah-chen\n- **email:** sarah@example.com\n",
        encoding="utf-8",
    )

    fake_msg = _fake_msg(from_email="sarah@example.com",
                         to_emails=["sam.smith@example.com"])
    monkeypatch.setattr(miner, "_fetch_messages", lambda service, q, max_messages: [fake_msg])
    monkeypatch.setattr(miner, "_build_service", lambda token, creds: object())
    monkeypatch.setattr(miner, "_extract_body", lambda msg: "She runs product at Acme")
    monkeypatch.setattr(miner, "extract_facts_from_text", _fake_extract_returns([
        {
            "subject": "sarah-chen",
            "category": "identity",
            "content": "Runs product at Acme",
            "confidence": 0.9,
            "audience_scope": ["professional"],
            "source_detail": "gmail:m1",
            "idempotency_key": "gmail-m1-sarah-chen-abc",
            "needs_review": False,
            "reason": "stated in sig",
        }
    ]))

    result = miner.run(
        token_path=tmp_path / "token.json",
        creds_path=tmp_path / "creds.json",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=workspace / "cache" / "gmail-mine-cursor.json",
        window_days=1,
        max_messages=10,
        commit=False,
    )

    assert result["status"] == "ok"
    assert result["messages_scanned"] == 1
    # In dry-run, facts_minted reflects what *would* have been written
    assert result["facts_minted"] == 1
    # But nothing actually got written to disk
    assert not facts_dir.exists() or not any(facts_dir.glob("*.md"))


def test_run_commit_writes_fact_and_updates_cursor(miner, tmp_path, monkeypatch):
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    workspace = tmp_path / "workspace"
    (workspace / "cache").mkdir(parents=True)
    people_dir.mkdir(parents=True)
    (people_dir / "sarah-chen.md").write_text(
        "# Sarah Chen\n\n- **slug:** sarah-chen\n- **email:** sarah@example.com\n",
        encoding="utf-8",
    )

    fake_msg = _fake_msg(
        from_email="sarah@example.com",
        to_emails=["sam.smith@example.com"],
        internal_date="1713500000000",
        message_id="msg-1",
    )
    monkeypatch.setattr(miner, "_fetch_messages", lambda service, q, max_messages: [fake_msg])
    monkeypatch.setattr(miner, "_build_service", lambda token, creds: object())
    monkeypatch.setattr(miner, "_extract_body", lambda msg: "She runs product at Acme")
    monkeypatch.setattr(miner, "extract_facts_from_text", _fake_extract_returns([
        {
            "subject": "sarah-chen",
            "category": "identity",
            "content": "Runs product at Acme",
            "confidence": 0.9,
            "audience_scope": ["professional"],
            "source_detail": "gmail:msg-1",
            "idempotency_key": "gmail-msg-1-sarah-chen-abc",
            "needs_review": False,
            "reason": "sig",
        }
    ]))

    result = miner.run(
        token_path=tmp_path / "token.json",
        creds_path=tmp_path / "creds.json",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=workspace / "cache" / "gmail-mine-cursor.json",
        window_days=1,
        max_messages=10,
        commit=True,
    )

    assert result["status"] == "ok"
    assert result["facts_minted"] == 1
    # Fact file exists for the current month
    assert list(facts_dir.glob("*.md"))
    # Cursor got saved
    cursor = json.loads((workspace / "cache" / "gmail-mine-cursor.json").read_text())
    assert cursor["last_internalDate"] == "1713500000000"
    assert "last_run_at" in cursor


def test_run_commit_flags_medium_confidence_to_review(miner, tmp_path, monkeypatch):
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    workspace = tmp_path / "workspace"
    (workspace / "cache").mkdir(parents=True)
    people_dir.mkdir(parents=True)
    (people_dir / "sarah-chen.md").write_text(
        "# Sarah Chen\n\n- **slug:** sarah-chen\n- **email:** sarah@example.com\n",
        encoding="utf-8",
    )

    fake_msg = _fake_msg(
        from_email="sarah@example.com",
        to_emails=["sam.smith@example.com"],
        message_id="m-review",
    )
    monkeypatch.setattr(miner, "_fetch_messages", lambda service, q, max_messages: [fake_msg])
    monkeypatch.setattr(miner, "_build_service", lambda token, creds: object())
    monkeypatch.setattr(miner, "_extract_body", lambda msg: "possibly moving")
    monkeypatch.setattr(miner, "extract_facts_from_text", _fake_extract_returns([
        {
            "subject": "sarah-chen",
            "category": "preference",
            "content": "Possibly moving to Austin",
            "confidence": 0.45,
            "audience_scope": ["personal"],
            "source_detail": "gmail:m-review",
            "idempotency_key": "gmail-m-review-sarah-chen-abc",
            "needs_review": True,
            "reason": "passing mention",
        }
    ]))

    result = miner.run(
        token_path=tmp_path / "token.json",
        creds_path=tmp_path / "creds.json",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=workspace / "cache" / "gmail-mine-cursor.json",
        window_days=1,
        max_messages=10,
        commit=True,
    )

    assert result["facts_minted"] == 1
    assert result["facts_flagged_low_conf"] == 1
    review_path = facts_dir / "_pending_review.md"
    assert review_path.exists()
    assert "Possibly moving to Austin" in review_path.read_text(encoding="utf-8")


def test_run_idempotent_dup_counts_separately(miner, tmp_path, monkeypatch):
    """Second run over same message should report skipped_dup, not
    a duplicate write."""
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    workspace = tmp_path / "workspace"
    (workspace / "cache").mkdir(parents=True)
    people_dir.mkdir(parents=True)
    (people_dir / "sarah-chen.md").write_text(
        "# Sarah Chen\n\n- **slug:** sarah-chen\n- **email:** sarah@example.com\n",
        encoding="utf-8",
    )

    fake_msg = _fake_msg(
        from_email="sarah@example.com",
        to_emails=["sam.smith@example.com"],
        message_id="dup-1",
    )
    monkeypatch.setattr(miner, "_fetch_messages", lambda service, q, max_messages: [fake_msg])
    monkeypatch.setattr(miner, "_build_service", lambda token, creds: object())
    monkeypatch.setattr(miner, "_extract_body", lambda msg: "x")
    # same idempotency_key on both calls
    payload = [
        {
            "subject": "sarah-chen",
            "category": "identity",
            "content": "Runs product at Acme",
            "confidence": 0.9,
            "audience_scope": ["professional"],
            "source_detail": "gmail:dup-1",
            "idempotency_key": "gmail-dup-1-sarah-chen-abc",
            "needs_review": False,
            "reason": "sig",
        }
    ]
    monkeypatch.setattr(miner, "extract_facts_from_text", _fake_extract_returns(payload))

    kwargs = dict(
        token_path=tmp_path / "token.json",
        creds_path=tmp_path / "creds.json",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=workspace / "cache" / "gmail-mine-cursor.json",
        window_days=1,
        max_messages=10,
        commit=True,
    )

    r1 = miner.run(**kwargs)
    assert r1["facts_minted"] == 1

    r2 = miner.run(**kwargs)
    assert r2["facts_minted"] == 0
    # Post-reinforcement (2026-04-20): second observation reinforces
    # instead of silently skipping, so the counter lands in
    # facts_reinforced rather than skipped_dup.
    assert r2["facts_reinforced"] == 1
    assert r2["skipped_dup"] == 0


def test_run_skips_messages_with_no_candidates(miner, tmp_path, monkeypatch):
    """Stranger → stranger message, no candidate slugs, no LLM call."""
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    workspace = tmp_path / "workspace"
    (workspace / "cache").mkdir(parents=True)
    people_dir.mkdir(parents=True)

    fake_msg = _fake_msg(
        from_email="stranger@example.com",
        to_emails=["alsostranger@example.com"],
    )
    monkeypatch.setattr(miner, "_fetch_messages", lambda service, q, max_messages: [fake_msg])
    monkeypatch.setattr(miner, "_build_service", lambda token, creds: object())
    monkeypatch.setattr(miner, "_extract_body", lambda msg: "x")

    extractor_called = [0]
    def tracker(**kw):
        extractor_called[0] += 1
        return []
    monkeypatch.setattr(miner, "extract_facts_from_text", tracker)

    result = miner.run(
        token_path=tmp_path / "token.json",
        creds_path=tmp_path / "creds.json",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=workspace / "cache" / "gmail-mine-cursor.json",
        window_days=1,
        max_messages=10,
        commit=True,
    )

    assert result["messages_scanned"] == 1
    assert result["messages_skipped_no_candidates"] == 1
    assert extractor_called[0] == 0


def test_run_emits_script_contract_envelope(miner, tmp_path, monkeypatch):
    """Final JSON envelope must carry status=ok and the expected metrics."""
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    workspace = tmp_path / "workspace"
    (workspace / "cache").mkdir(parents=True)
    people_dir.mkdir(parents=True)

    monkeypatch.setattr(miner, "_fetch_messages", lambda service, q, max_messages: [])
    monkeypatch.setattr(miner, "_build_service", lambda token, creds: object())

    result = miner.run(
        token_path=tmp_path / "token.json",
        creds_path=tmp_path / "creds.json",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=workspace / "cache" / "gmail-mine-cursor.json",
        window_days=1,
        max_messages=10,
        commit=True,
    )

    for key in (
        "status", "messages_scanned", "facts_minted",
        "skipped_dup", "facts_flagged_low_conf",
        "messages_skipped_no_candidates",
    ):
        assert key in result, f"missing key: {key}"
    assert result["status"] == "ok"


def test_main_always_exits_zero(miner, monkeypatch, capsys):
    def boom(**kw):
        raise RuntimeError("simulated")
    monkeypatch.setattr(miner, "run", boom)
    rc = miner.main([])
    assert rc == 0
    out = capsys.readouterr().out.strip().split("\n")[-1]
    envelope = json.loads(out)
    assert envelope["status"] == "error"
