"""Tests for the pure helpers that power backfill-known-by.py.

Resolvers per source type:
  - Flux numeric (source_detail is a bare integer → Flux message.id):
    lookup sender + recipient in the Flux SQLite `messages` table, map
    emails to Clawford slugs, drop the operator.
  - Gmail (source_detail starts with "gmail:"): fetch message via Gmail
    API, pull From/To/Cc headers, map + drop the operator.
  - Everything else: return None (leave known_by empty).

Rewriter:
  - rewrite_block_with_known_by splices the JSON list into the block,
    after audience_scope when present. Idempotent — blocks already
    carrying known_by are left untouched.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

_spec = importlib.util.spec_from_file_location(
    "backfill_known_by_lib", _SCRIPTS_DIR / "backfill_known_by_lib.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ─── detect_source_type ──────────────────────────────────────────────


def test_detect_flux_numeric():
    assert mod.detect_source_type("2396") == "flux_numeric"
    assert mod.detect_source_type("24148") == "flux_numeric"


def test_detect_gmail():
    assert mod.detect_source_type("gmail:19db047e655993c2") == "gmail"


def test_detect_llm_extracted():
    # Facts with this source came from the scope-augment retroactive pass
    # and have no backref to participants.
    assert mod.detect_source_type("LLM-extracted from interaction data") == "llm_extracted"


def test_detect_birthday_miner():
    assert mod.detect_source_type("birthday-miner/calendar/Xiao Yu's birthday") == "birthday_miner"
    assert mod.detect_source_type("birthday-miner/manual") == "birthday_miner"


def test_detect_krisp():
    assert mod.detect_source_type("Google Meet with Alexis Lloyd debrief (event_id abc)") == "krisp"


def test_detect_workflowy():
    assert mod.detect_source_type("workflowy:xyz123") == "workflowy"


def test_detect_unknown():
    assert mod.detect_source_type("") == "unknown"
    assert mod.detect_source_type("?") == "unknown"


# ─── resolve_flux_numeric ────────────────────────────────────────────


def _seed_flux_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            sender_address TEXT,
            recipient_address TEXT,
            platform TEXT
        )
    """)
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", [
        (2396, "danzylber@gmail.com", "sam.smith@example.com", "gmail"),
        (2397, "sam.smith@example.com", "jane@example.com", "gmail"),
        (2398, None, None, "whatsapp"),  # anonymous WhatsApp row
        (2399, "sarah@example.com", "operator@example.com", "gmail"),
    ])
    conn.commit()
    conn.close()


def test_resolve_flux_numeric_returns_non_brian_slugs(tmp_path: Path):
    db = tmp_path / "flux.db"
    _seed_flux_db(db)
    conn = sqlite3.connect(db)
    email_to_slug = {
        "danzylber@gmail.com": "dan-zylberglejd",
        "sarah@example.com": "sarah-chen",
        "jane@example.com": "jane-doe",
    }
    brian_addrs = {"sam.smith@example.com", "operator@example.com"}
    out = mod.resolve_flux_numeric(
        conn, "2396",
        email_to_slug=email_to_slug, operator_emails=brian_addrs,
    )
    assert out == ["dan-zylberglejd"]  # operator recipient filtered
    conn.close()


def test_resolve_flux_numeric_outbound_message_resolves_recipient(tmp_path: Path):
    # Message 2397: the operator → jane. the operator filtered; Jane's slug retained.
    db = tmp_path / "flux.db"
    _seed_flux_db(db)
    conn = sqlite3.connect(db)
    email_to_slug = {"jane@example.com": "jane-doe"}
    brian_addrs = {"sam.smith@example.com"}
    out = mod.resolve_flux_numeric(
        conn, "2397",
        email_to_slug=email_to_slug, operator_emails=brian_addrs,
    )
    assert out == ["jane-doe"]
    conn.close()


def test_resolve_flux_numeric_unknown_email_drops_silently(tmp_path: Path):
    db = tmp_path / "flux.db"
    _seed_flux_db(db)
    conn = sqlite3.connect(db)
    # No slug for sarah@example.com → she's dropped, operator also dropped → empty list
    out = mod.resolve_flux_numeric(
        conn, "2399",
        email_to_slug={}, operator_emails={"operator@example.com"},
    )
    assert out == []
    conn.close()


def test_resolve_flux_numeric_null_addresses_returns_empty(tmp_path: Path):
    # WhatsApp row with NULL addresses
    db = tmp_path / "flux.db"
    _seed_flux_db(db)
    conn = sqlite3.connect(db)
    out = mod.resolve_flux_numeric(
        conn, "2398",
        email_to_slug={}, operator_emails=set(),
    )
    assert out == []
    conn.close()


def test_resolve_flux_numeric_missing_message_id_returns_none(tmp_path: Path):
    db = tmp_path / "flux.db"
    _seed_flux_db(db)
    conn = sqlite3.connect(db)
    out = mod.resolve_flux_numeric(
        conn, "99999",
        email_to_slug={}, operator_emails=set(),
    )
    # None (not empty list) — signals "couldn't resolve" so caller can
    # choose to skip vs mark empty explicitly.
    assert out is None
    conn.close()


def test_resolve_flux_numeric_case_insensitive_email_match(tmp_path: Path):
    db = tmp_path / "flux.db"
    _seed_flux_db(db)
    conn = sqlite3.connect(db)
    # Map has lowercase; DB has lowercase already, but guard the logic
    email_to_slug = {"danzylber@gmail.com": "dan-z"}
    out = mod.resolve_flux_numeric(
        conn, "2396",
        email_to_slug=email_to_slug,
        operator_emails={"Sam.M.Smith@gmail.com"},  # mixed case
    )
    assert out == ["dan-z"]  # operator filtered even though case differs
    conn.close()


# ─── resolve_gmail ───────────────────────────────────────────────────


class _FakeGmailService:
    def __init__(self, message: dict):
        self._message = message

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId, id, format):
        self._last = {"id": id, "format": format}
        outer = self

        class _Exec:
            def execute(s):
                return outer._message

        return _Exec()


def _fake_message(from_: str, to: str = "", cc: str = "") -> dict:
    headers = [{"name": "From", "value": from_}]
    if to:
        headers.append({"name": "To", "value": to})
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {"payload": {"headers": headers}}


def test_resolve_gmail_extracts_from_to_cc():
    svc = _FakeGmailService(_fake_message(
        from_="<sarah@example.com>",
        to="Sam Smith <operator@example.com>, Mike <mike@example.com>",
        cc="Dana <dana@example.com>",
    ))
    out = mod.resolve_gmail(
        svc, "gmail:abc",
        email_to_slug={
            "sarah@example.com": "sarah-chen",
            "mike@example.com": "mike-jones",
            "dana@example.com": "dana-wright",
        },
        operator_emails={"operator@example.com"},
    )
    # the operator dropped; others preserved, sorted for stable output
    assert out == ["dana-wright", "mike-jones", "sarah-chen"]


def test_resolve_gmail_drops_unknown_emails():
    svc = _FakeGmailService(_fake_message(
        from_="stranger@example.com",
        to="operator@example.com",
    ))
    out = mod.resolve_gmail(
        svc, "gmail:abc",
        email_to_slug={},
        operator_emails={"operator@example.com"},
    )
    assert out == []


def test_resolve_gmail_handles_api_error_returns_none():
    class _Bad:
        def users(self):
            raise RuntimeError("api down")

    out = mod.resolve_gmail(_Bad(), "gmail:abc",
                            email_to_slug={}, operator_emails=set())
    assert out is None


# ─── rewrite_block_with_known_by ─────────────────────────────────────


def test_rewrite_inserts_known_by_after_audience_scope():
    block = (
        "\n- **id:** x\n"
        "- **content:** c\n"
        "- **subject:** s\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.9\n"
        "- **recorded_at:** 2026-04-21\n"
        '- **audience_scope:** ["personal"]\n'
    )
    out = mod.rewrite_block_with_known_by(block, ["sarah-chen", "mike-jones"])
    assert '- **known_by:** ["sarah-chen", "mike-jones"]' in out
    # Ordering: known_by lands after audience_scope
    idx_audience = out.index("audience_scope")
    idx_known = out.index("known_by")
    assert idx_known > idx_audience


def test_rewrite_idempotent_when_already_carries_known_by():
    block = (
        "\n- **id:** x\n"
        "- **content:** c\n"
        "- **subject:** s\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.9\n"
        "- **recorded_at:** 2026-04-21\n"
        '- **known_by:** ["old-slug"]\n'
    )
    out = mod.rewrite_block_with_known_by(block, ["new-slug"])
    # Re-runs must not clobber existing known_by
    assert out == block


def test_rewrite_appends_at_end_when_no_audience_scope_line():
    block = (
        "\n- **id:** x\n"
        "- **content:** c\n"
        "- **subject:** s\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.9\n"
        "- **recorded_at:** 2026-04-21\n"
    )
    out = mod.rewrite_block_with_known_by(block, ["sarah-chen"])
    assert '- **known_by:** ["sarah-chen"]' in out


def test_rewrite_noop_on_empty_known_by_list():
    block = (
        "\n- **id:** x\n"
        "- **content:** c\n"
        "- **subject:** s\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.9\n"
        "- **recorded_at:** 2026-04-21\n"
    )
    out = mod.rewrite_block_with_known_by(block, [])
    assert out == block  # empty list = no value to write
