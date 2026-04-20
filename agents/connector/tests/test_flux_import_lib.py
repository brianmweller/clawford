"""Pure helpers for importing Flux knowledge facts into Huckle Cat's brain.

build_email_to_slug_map: scans ~/Dropbox/openclaw-backup/people/*.md
and returns email → slug lookup.

flux_row_to_huckle_fact: maps a Flux knowledge_facts row dict to the
Huckle fact dict that upsert_fact() consumes. Unknown subjects are
mapped to None so the caller can skip them cleanly.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from flux_import_lib import (  # type: ignore
    build_email_to_slug_map,
    flux_row_to_huckle_fact,
    parse_audience_scope,
)


# --- build_email_to_slug_map ---

def test_build_email_to_slug_map_reads_people_files(tmp_path: Path):
    people = tmp_path / "people"
    people.mkdir()
    (people / "alice-smith.md").write_text(
        "# Alice Smith\n\n"
        "- **slug:** alice-smith\n"
        "- **email:** alice@example.com\n",
        encoding="utf-8",
    )
    (people / "bob-jones.md").write_text(
        "# Bob Jones\n\n"
        "- **slug:** bob-jones\n"
        "- **email:** Bob.Jones@work.org\n",
        encoding="utf-8",
    )
    mapping = build_email_to_slug_map(people)
    assert mapping["alice@example.com"] == "alice-smith"
    assert mapping["bob.jones@work.org"] == "bob-jones"  # normalized to lowercase


def test_build_email_to_slug_map_skips_people_without_email(tmp_path: Path):
    people = tmp_path / "people"
    people.mkdir()
    (people / "no-email.md").write_text(
        "# No Email\n\n- **slug:** no-email\n- **email:** —\n",
        encoding="utf-8",
    )
    (people / "has-email.md").write_text(
        "# Has Email\n\n- **slug:** has-email\n- **email:** has@ex.com\n",
        encoding="utf-8",
    )
    mapping = build_email_to_slug_map(people)
    assert "has@ex.com" in mapping
    # em-dash / missing → skipped
    assert len(mapping) == 1


def test_build_email_to_slug_map_ignores_template_files(tmp_path: Path):
    people = tmp_path / "people"
    people.mkdir()
    (people / "_template.md").write_text(
        "# Template\n- **slug:** _template\n- **email:** template@example.com\n",
        encoding="utf-8",
    )
    (people / "real.md").write_text(
        "# Real\n- **slug:** real\n- **email:** real@example.com\n",
        encoding="utf-8",
    )
    mapping = build_email_to_slug_map(people)
    assert "template@example.com" not in mapping
    assert "real@example.com" in mapping


def test_build_email_to_slug_map_missing_dir_returns_empty(tmp_path: Path):
    assert build_email_to_slug_map(tmp_path / "does-not-exist") == {}


# --- parse_audience_scope ---

def test_parse_audience_scope_json_array():
    assert parse_audience_scope('["professional"]') == ["professional"]
    assert parse_audience_scope('["personal", "family"]') == ["personal", "family"]


def test_parse_audience_scope_null_or_empty():
    assert parse_audience_scope(None) is None
    assert parse_audience_scope("") is None
    assert parse_audience_scope("null") is None


def test_parse_audience_scope_malformed_returns_none():
    # Defensive: Flux stores JSON but we shouldn't explode on garbage
    assert parse_audience_scope("not json") is None


# --- flux_row_to_huckle_fact ---

def _flux_row(**overrides):
    base = {
        "id": 12345,
        "fact_type": "relationship",
        "subject": "Josh Cherry",
        "subject_address": "cherry@anthropic.com",
        "content": "Anthropic recruiter working on Product DS loops",
        "source_type": "message",
        "source_id": "gmail-abc123",
        "audience_scope": '["professional"]',
        "confidence": 0.85,
        "status": "active",
        "established_at": "2026-04-10T21:08:04Z",
    }
    base.update(overrides)
    return base


def test_flux_row_to_huckle_fact_returns_huckle_shape_when_subject_known():
    email_to_slug = {"cherry@anthropic.com": "josh-cherry"}
    result = flux_row_to_huckle_fact(_flux_row(), email_to_slug)
    assert result is not None
    assert result["subject"] == "josh-cherry"
    assert result["content"] == "Anthropic recruiter working on Product DS loops"
    assert result["category"] == "relationship"
    assert result["confidence"] == 0.85
    assert result["audience_scope"] == ["professional"]
    assert result["source_agent"] == "flux"
    assert result["source_type"] == "message"
    assert result["source_detail"] == "gmail-abc123"
    assert result["idempotency_key"] == "12345"
    assert result["recorded_at"].startswith("2026-04-10")


def test_flux_row_to_huckle_fact_returns_none_when_subject_unknown():
    email_to_slug = {"someone-else@example.com": "someone-else"}
    result = flux_row_to_huckle_fact(_flux_row(), email_to_slug)
    assert result is None


def test_flux_row_matches_email_case_insensitively():
    row = _flux_row(subject_address="Cherry@Anthropic.com")
    email_to_slug = {"cherry@anthropic.com": "josh-cherry"}
    result = flux_row_to_huckle_fact(row, email_to_slug)
    assert result is not None
    assert result["subject"] == "josh-cherry"


def test_flux_row_handles_missing_audience_scope():
    row = _flux_row(audience_scope=None)
    email_to_slug = {"cherry@anthropic.com": "josh-cherry"}
    result = flux_row_to_huckle_fact(row, email_to_slug)
    assert result["audience_scope"] is None  # preserved as None; Huckle treats as visible-to-all


def test_flux_row_handles_missing_subject_address():
    row = _flux_row(subject_address=None)
    result = flux_row_to_huckle_fact(row, {})
    assert result is None


def test_flux_row_handles_empty_content():
    row = _flux_row(content="")
    result = flux_row_to_huckle_fact(row, {"cherry@anthropic.com": "josh-cherry"})
    # Empty content is not useful; skip
    assert result is None


def test_flux_row_idempotency_key_uses_flux_id():
    # The upsert will write id = "flux-<subject>-flux-12345" via upsert_fact's
    # {source_agent}-{subject}-{idempotency_key} convention. This guards against
    # double-imports on re-runs.
    row = _flux_row(id=99999)
    result = flux_row_to_huckle_fact(row, {"cherry@anthropic.com": "josh-cherry"})
    assert result["idempotency_key"] == "99999"
