"""Tests for agents/meetings-coach/scripts/krisp-facts-mine.py.

Daily cron that scans ~/.clawford/meetings-coach-workspace/cache/
pending-debrief-*.json files, runs each transcript through the shared
fact-extraction helper, and writes durable facts to the Huckle brain.

Lives in meetings-coach (not connector) so the cross-workspace read
that killed the old connector-based daily-refresh under bwrap isn't a
concern — the miner reads its own agent's cache.

Pure helpers live in krisp_facts_mine_lib.py. The thin orchestrator
wires everything.
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
        f"kfm_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def lib():
    return _load_script("krisp_facts_mine_lib.py")


@pytest.fixture
def miner():
    return _load_script("krisp-facts-mine.py")


# ─── Pending-debrief loading ─────────────────────────────────────────


def _write_debrief(cache_dir: Path, event_id: str, **overrides) -> Path:
    base = {
        "event_id": event_id,
        "meeting_title": "1:1",
        "meeting_start": "2026-04-19T10:00:00-07:00",
        "attendees": [{"name": "Sarah Chen", "email": "sarah@example.com"}],
        "transcript_text": "Sarah: I'm starting a fintech company next month.",
        "staged_at": "2026-04-19T11:00:00Z",
    }
    base.update(overrides)
    path = cache_dir / f"pending-debrief-{event_id}.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    return path


def test_load_debriefs_returns_all_pending_files(lib, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_debrief(cache, "evt-1")
    _write_debrief(cache, "evt-2")
    debriefs = lib.load_debriefs(cache)
    assert len(debriefs) == 2
    assert {d["event_id"] for d in debriefs} == {"evt-1", "evt-2"}


def test_load_debriefs_skips_malformed_json(lib, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_debrief(cache, "evt-ok")
    (cache / "pending-debrief-broken.json").write_text("not json", encoding="utf-8")
    debriefs = lib.load_debriefs(cache)
    assert len(debriefs) == 1
    assert debriefs[0]["event_id"] == "evt-ok"


def test_load_debriefs_returns_empty_when_no_cache(lib, tmp_path):
    cache = tmp_path / "missing"
    debriefs = lib.load_debriefs(cache)
    assert debriefs == []


# ─── All-hands filter ────────────────────────────────────────────────


def test_is_all_hands_true_when_attendees_over_6(lib):
    debrief = {"attendees": [
        {"name": f"P{i}", "email": f"p{i}@x.com"} for i in range(7)
    ]}
    assert lib.is_all_hands(debrief) is True


def test_is_all_hands_false_when_attendees_at_threshold(lib):
    """6 attendees is the boundary — INCLUDED, not excluded."""
    debrief = {"attendees": [
        {"name": f"P{i}", "email": f"p{i}@x.com"} for i in range(6)
    ]}
    assert lib.is_all_hands(debrief) is False


def test_is_all_hands_false_for_1on1(lib):
    debrief = {"attendees": [{"name": "Sarah", "email": "s@x.com"}]}
    assert lib.is_all_hands(debrief) is False


# ─── Candidate slug derivation ───────────────────────────────────────


def test_build_candidate_slugs_maps_attendee_emails(lib):
    debrief = {
        "attendees": [
            {"name": "Sarah Chen", "email": "sarah@example.com"},
            {"name": "Mike Jones", "email": "mike@example.com"},
        ],
    }
    email_to_slug = {
        "sarah@example.com": "sarah-chen",
        "mike@example.com": "mike-jones",
    }
    slugs = lib.build_candidate_slugs(
        debrief, email_to_slug=email_to_slug,
        operator_emails={"sam.smith@example.com"},
    )
    assert slugs == {"sarah-chen", "mike-jones"}


def test_build_candidate_slugs_filters_brian(lib):
    debrief = {
        "attendees": [
            {"name": "Sarah Chen", "email": "sarah@example.com"},
            {"name": "Sam Smith", "email": "sam.smith@example.com"},
        ],
    }
    email_to_slug = {"sarah@example.com": "sarah-chen"}
    slugs = lib.build_candidate_slugs(
        debrief, email_to_slug=email_to_slug,
        operator_emails={"sam.smith@example.com"},
    )
    assert slugs == {"sarah-chen"}


# ─── Transcript chunking ─────────────────────────────────────────────


def test_chunk_transcript_returns_single_chunk_when_under_limit(lib):
    text = "hello world"
    chunks = lib.chunk_transcript(text, max_chars=8000)
    assert chunks == ["hello world"]


def test_chunk_transcript_splits_when_over_limit(lib):
    text = "a" * 10000
    chunks = lib.chunk_transcript(text, max_chars=4000)
    assert len(chunks) >= 3
    assert "".join(chunks) == text


def test_chunk_transcript_empty_returns_empty_list(lib):
    assert lib.chunk_transcript("", max_chars=8000) == []
    assert lib.chunk_transcript(None, max_chars=8000) == []


# ─── Orchestrator run() ──────────────────────────────────────────────


def _fake_extract_returns(facts: list[dict]):
    def _fn(**kw):
        return facts
    return _fn


def test_run_skips_all_hands_transcripts(miner, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_debrief(
        cache, "evt-all-hands",
        attendees=[{"name": f"P{i}", "email": f"p{i}@x.com"} for i in range(10)],
    )
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)

    extract_calls = [0]
    def tracker(**kw):
        extract_calls[0] += 1
        return []
    monkeypatch.setattr(miner, "extract_facts_from_text", tracker)

    result = miner.run(
        cache_dir=cache,
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_age_days=30,
        commit=True,
    )
    assert result["transcripts_skipped_all_hands"] == 1
    assert extract_calls[0] == 0


def test_run_writes_fact_from_1on1(miner, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_debrief(cache, "evt-1on1")
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)
    (people_dir / "sarah-chen.md").write_text(
        "# Sarah Chen\n\n- **slug:** sarah-chen\n- **email:** sarah@example.com\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(miner, "extract_facts_from_text", _fake_extract_returns([
        {
            "subject": "sarah-chen",
            "category": "identity",
            "content": "Starting a fintech company",
            "confidence": 0.88,
            "audience_scope": ["professional"],
            "source_detail": "krisp:evt-1on1",
            "idempotency_key": "krisp-evt-1on1-sarah-chen-abc",
            "needs_review": False,
            "reason": "stated explicitly",
        }
    ]))

    result = miner.run(
        cache_dir=cache,
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_age_days=30,
        commit=True,
    )
    assert result["facts_minted"] == 1
    assert list(facts_dir.glob("*.md"))


def test_run_dry_run_does_not_write(miner, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_debrief(cache, "evt-dry")
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)
    (people_dir / "sarah-chen.md").write_text(
        "# Sarah Chen\n\n- **slug:** sarah-chen\n- **email:** sarah@example.com\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(miner, "extract_facts_from_text", _fake_extract_returns([
        {
            "subject": "sarah-chen",
            "category": "identity",
            "content": "x",
            "confidence": 0.9,
            "audience_scope": ["professional"],
            "source_detail": "krisp:evt-dry",
            "idempotency_key": "krisp-evt-dry-sarah-chen-abc",
            "needs_review": False,
            "reason": "y",
        }
    ]))

    result = miner.run(
        cache_dir=cache,
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_age_days=30,
        commit=False,
    )
    assert result["facts_minted"] == 1
    assert not facts_dir.exists() or not any(facts_dir.glob("*.md"))


def test_run_skips_transcripts_with_no_candidates(miner, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_debrief(
        cache, "evt-unknown",
        attendees=[{"name": "Stranger", "email": "stranger@example.com"}],
    )
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)

    extract_calls = [0]
    def tracker(**kw):
        extract_calls[0] += 1
        return []
    monkeypatch.setattr(miner, "extract_facts_from_text", tracker)

    result = miner.run(
        cache_dir=cache,
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_age_days=30,
        commit=True,
    )
    assert result["transcripts_skipped_no_candidates"] == 1
    assert extract_calls[0] == 0


def test_run_emits_script_contract_envelope(miner, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)

    result = miner.run(
        cache_dir=cache,
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_age_days=30,
        commit=True,
    )
    for key in (
        "status", "transcripts_scanned", "transcripts_skipped_all_hands",
        "transcripts_skipped_no_candidates", "facts_minted", "skipped_dup",
        "facts_flagged_low_conf",
    ):
        assert key in result, f"missing key: {key}"
    assert result["status"] == "ok"


def test_main_always_exits_zero(miner, monkeypatch, capsys):
    def boom(**kw):
        raise RuntimeError("simulated")
    monkeypatch.setattr(miner, "run", boom)
    rc = miner.main([])
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert envelope["status"] == "error"
