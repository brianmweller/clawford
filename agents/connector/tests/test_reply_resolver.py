"""Tests for the fuzzy thread-descriptor resolver in
agents/connector/tools.py::_resolve_thread_descriptor.

Operator never has a Gmail thread_id handy. They describe the email:
"the Michelle email", "Jamie's thread", "that Reddit recruiter reply".
The resolver bridges that gap — scans the auto-compose log + triage
queue + people dir and returns a thread_id when it can, or candidates
when the descriptor is ambiguous.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO / "agents" / "connector" / "scripts"
_SHARED = _REPO / "agents" / "shared"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SHARED))

# tools.py is in the connector dir, not scripts/
_TOOLS = _REPO / "agents" / "connector" / "tools.py"
_spec = importlib.util.spec_from_file_location("conn_tools", _TOOLS)
tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tools)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Seed a fake workspace with a triage queue + compose log."""
    cache = tmp_path / "cache"
    cache.mkdir()
    log = {
        "19db1111111111aa": {
            "at": "2026-04-21T20:00:00Z",
            "slug": "jamie-fitzgerald",
            "cold_inbound": False,
            "fit_tier": "",
            "reply_needed": False,
            "subject": "Re: hello!",
        },
        "19db2222222222bb": {
            "at": "2026-04-21T21:00:00Z",
            "slug": "michelle-leist",
            "cold_inbound": False,
            "fit_tier": "B",
            "reply_needed": True,
            "subject": "Senior Director role at Reddit",
            "gmail_draft_id": "r1234",
        },
        "19db3333333333cc": {
            "at": "2026-04-20T10:00:00Z",
            "slug": None,
            "cold_inbound": True,
            "fit_tier": "A",
            "reply_needed": True,
            "subject": "Head of Data Science at Anthropic",
        },
    }
    queue = {
        "queued": [
            {"thread_id": "19db2222222222bb",
             "from_email": "michelle@rivierapartners.com",
             "from_header": '"Michelle Leist" <michelle@rivierapartners.com>',
             "subject": "Senior Director role at Reddit",
             "status": "queued"},
            {"thread_id": "19db3333333333cc",
             "from_email": "recruiter@anthropic.com",
             "from_header": '"Anthropic Talent" <recruiter@anthropic.com>',
             "subject": "Head of Data Science at Anthropic",
             "status": "queued_cold_recruiter"},
            {"thread_id": "19db4444444444dd",
             "from_email": "john@example.com",
             "from_header": 'John <john@example.com>',
             "subject": "Quick question",
             "status": "queued"},
        ],
    }
    (cache / "auto-compose-log.json").write_text(json.dumps(log), encoding="utf-8")
    (cache / "triage-queue.json").write_text(json.dumps(queue), encoding="utf-8")

    monkeypatch.setattr(tools, "_AUTO_COMPOSE_LOG",
                        str(cache / "auto-compose-log.json"))
    monkeypatch.setattr(tools, "_TRIAGE_QUEUE_PATH",
                        str(cache / "triage-queue.json"))
    # Stub brain.get_person — no real filesystem lookup in tests.
    monkeypatch.setattr(tools.brain, "get_person", lambda name: None)


# ---------------------------------------------------------------------------
# Explicit thread_id passthrough
# ---------------------------------------------------------------------------


def test_explicit_thread_id_passes_through_when_known(seeded):
    r = tools._resolve_thread_descriptor("19db2222222222bb")
    assert r["status"] == "ok"
    assert r["thread_id"] == "19db2222222222bb"


def test_explicit_thread_id_passes_through_when_unknown(seeded):
    r = tools._resolve_thread_descriptor("19dbffffffffffff")
    # Valid shape → pass through even if not in state (Gmail will 404).
    assert r["status"] == "ok"
    assert r["thread_id"] == "19dbffffffffffff"


# ---------------------------------------------------------------------------
# Email matching
# ---------------------------------------------------------------------------


def test_exact_email_resolves_to_single_thread(seeded):
    r = tools._resolve_thread_descriptor("michelle@rivierapartners.com")
    assert r["status"] == "ok"
    assert r["thread_id"] == "19db2222222222bb"
    assert "email_exact" in r["matched"]["match_reason"] \
        or "email_substring" in r["matched"]["match_reason"]


# ---------------------------------------------------------------------------
# Sender-name substring matching (common case)
# ---------------------------------------------------------------------------


def test_partial_name_in_sender_header_resolves(seeded):
    r = tools._resolve_thread_descriptor("michelle")
    assert r["status"] == "ok"
    assert r["thread_id"] == "19db2222222222bb"


def test_uppercase_descriptor_still_matches(seeded):
    r = tools._resolve_thread_descriptor("MICHELLE")
    assert r["status"] == "ok"
    assert r["thread_id"] == "19db2222222222bb"


# ---------------------------------------------------------------------------
# Subject-substring matching
# ---------------------------------------------------------------------------


def test_subject_keyword_resolves_single_match(seeded):
    r = tools._resolve_thread_descriptor("Reddit")
    assert r["status"] == "ok"
    assert r["thread_id"] == "19db2222222222bb"


def test_subject_keyword_with_multiple_matches_is_ambiguous(seeded, monkeypatch):
    # Add a second 'Reddit' in a different thread.
    import json as _json
    log_path = tools._AUTO_COMPOSE_LOG
    log = _json.loads(Path(log_path).read_text(encoding="utf-8"))
    log["19db5555555555ee"] = {
        "at": "2026-04-21T12:00:00Z", "slug": "other-slug",
        "subject": "Reddit onsite prep", "reply_needed": True,
        "cold_inbound": False, "fit_tier": "",
    }
    Path(log_path).write_text(_json.dumps(log), encoding="utf-8")

    r = tools._resolve_thread_descriptor("Reddit")
    assert r["status"] == "ambiguous"
    assert len(r["candidates"]) == 2
    tids = {c["thread_id"] for c in r["candidates"]}
    assert "19db2222222222bb" in tids
    assert "19db5555555555ee" in tids


# ---------------------------------------------------------------------------
# Person name via brain.get_person
# ---------------------------------------------------------------------------


def test_person_name_resolves_via_brain_then_slug_or_email(seeded, monkeypatch):
    """brain.get_person returns a people record with email in the raw
    frontmatter; the resolver matches that email against queue
    from_email."""
    monkeypatch.setattr(tools.brain, "get_person", lambda name: (
        {"slug": "michelle-leist", "name": "Michelle Leist",
         "fields": {"email": "michelle@rivierapartners.com"},
         "raw": "# Michelle Leist\n- **email:** michelle@rivierapartners.com\n"}
        if name.lower() in ("michelle", "michelle leist", "michelle-leist")
        else None
    ))

    r = tools._resolve_thread_descriptor("Michelle Leist")
    assert r["status"] == "ok"
    assert r["thread_id"] == "19db2222222222bb"
    assert "slug=michelle-leist" in r["matched"]["match_reason"] \
        or "person_email" in r["matched"]["match_reason"]


# ---------------------------------------------------------------------------
# Empty + not-found paths
# ---------------------------------------------------------------------------


def test_empty_descriptor_returns_not_found(seeded):
    r = tools._resolve_thread_descriptor("")
    assert r["status"] == "not_found"


def test_completely_unknown_descriptor_returns_recent_threads(seeded):
    r = tools._resolve_thread_descriptor("zzqwertynothinglikethat")
    assert r["status"] == "not_found"
    assert "recent_threads" in r
    # Three threads in the fake log → up to 5 returned.
    assert len(r["recent_threads"]) >= 1


# ---------------------------------------------------------------------------
# Cache-path robustness
# ---------------------------------------------------------------------------


def test_resolver_handles_missing_log_and_queue(tmp_path, monkeypatch):
    """If the workspace is fresh (no log, no queue), the resolver must
    not crash — it should return not_found with empty recent_threads."""
    monkeypatch.setattr(tools, "_AUTO_COMPOSE_LOG",
                        str(tmp_path / "nope.json"))
    monkeypatch.setattr(tools, "_TRIAGE_QUEUE_PATH",
                        str(tmp_path / "also-nope.json"))
    monkeypatch.setattr(tools.brain, "get_person", lambda n: None)
    r = tools._resolve_thread_descriptor("Michelle")
    assert r["status"] == "not_found"
