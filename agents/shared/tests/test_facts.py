"""Fact-file reader with audience_scope support.

parse_facts_file() reads a facts/YYYY-MM.md file and returns one dict per
entry. The schema extends monthly-archival.py's existing parser with an
audience_scope field (JSON list, comma-separated, or missing).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.shared.facts import parse_facts_file, load_facts_for_subject, upsert_fact


SAMPLE_FACTS = """\
- **id:** f-001
- **content:** Priya's chemo is Monday
- **subject:** priya-rivera
- **confidence:** 0.95
- **category:** health
- **recorded_at:** 2026-04-10
- **audience_scope:** ["family", "personal"]
- **source_agent:** connector
---
- **id:** f-002
- **content:** Aaron is your wealth advisor
- **subject:** aaron-nuti
- **confidence:** 0.9
- **category:** work
- **recorded_at:** 2026-04-12
- **audience_scope:** professional
- **source_agent:** connector
---
- **id:** f-003
- **content:** Jane mentioned she's pregnant (not public yet)
- **subject:** jane-doe
- **confidence:** 0.8
- **category:** identity
- **recorded_at:** 2026-04-15
- **source_agent:** connector
"""


@pytest.fixture
def facts_file(tmp_path: Path) -> Path:
    p = tmp_path / "2026-04.md"
    p.write_text(SAMPLE_FACTS, encoding="utf-8")
    return p


def test_parse_facts_file_yields_three_entries(facts_file):
    facts = parse_facts_file(facts_file)
    assert len(facts) == 3
    assert [f["id"] for f in facts] == ["f-001", "f-002", "f-003"]


def test_parse_facts_extracts_core_fields(facts_file):
    facts = parse_facts_file(facts_file)
    f1 = facts[0]
    assert f1["content"] == "Priya's chemo is Monday"
    assert f1["subject"] == "priya-rivera"
    assert f1["confidence"] == 0.95
    assert f1["category"] == "health"


def test_parse_facts_extracts_audience_scope_from_json_array(facts_file):
    facts = parse_facts_file(facts_file)
    assert facts[0]["audience_scope"] == ["family", "personal"]


def test_parse_facts_extracts_audience_scope_from_bare_string(facts_file):
    facts = parse_facts_file(facts_file)
    # "professional" (no brackets) should still become a one-element list
    assert facts[1]["audience_scope"] == ["professional"]


def test_parse_facts_missing_scope_is_none_not_empty(facts_file):
    # A fact with no audience_scope: downstream audience filter treats None
    # as "visible to all" (Flux-compatible). Keep it as None so callers can
    # detect the difference if they need to.
    facts = parse_facts_file(facts_file)
    assert facts[2].get("audience_scope") is None


def test_parse_facts_missing_file_returns_empty_list(tmp_path):
    assert parse_facts_file(tmp_path / "does-not-exist.md") == []


def test_load_facts_for_subject_filters_across_months(tmp_path: Path):
    # Fake facts dir with two months; facts for priya in both
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-03.md").write_text(
        "- **id:** f-010\n"
        "- **content:** March fact\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.8\n"
        "- **category:** health\n"
        "- **recorded_at:** 2026-03-05\n",
        encoding="utf-8",
    )
    (facts_dir / "2026-04.md").write_text(SAMPLE_FACTS, encoding="utf-8")

    facts = load_facts_for_subject("priya-rivera", facts_dir)
    assert len(facts) == 2
    assert {f["id"] for f in facts} == {"f-001", "f-010"}


def test_load_facts_for_subject_is_case_insensitive(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(SAMPLE_FACTS, encoding="utf-8")
    facts = load_facts_for_subject("AARON-NUTI", facts_dir)
    assert len(facts) == 1
    assert facts[0]["id"] == "f-002"


# ---------------------------------------------------------------------------
# upsert_fact — idempotent fact writer for passive miners (birthday miner et al)
# ---------------------------------------------------------------------------


def test_upsert_fact_creates_month_file_with_header(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    result = upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="Birthday: 1953-09-15",
        source_agent="connector",
        source_type="derived",
        source_detail="birthday-miner/calendar",
        idempotency_key="birthday",
        recorded_at="2026-04-19T12:00:00Z",
    )
    month_file = facts_dir / "2026-04.md"
    assert month_file.exists()
    text = month_file.read_text(encoding="utf-8")
    assert "# Facts — 2026-04" in text
    assert "- **subject:** priya-rivera" in text
    assert "- **content:** Birthday: 1953-09-15" in text
    assert "- **category:** identity" in text
    assert "- **source_agent:** connector" in text
    assert result["status"] == "created"
    assert result["id"].startswith("connector-priya-rivera-birthday")


def test_upsert_fact_appends_to_existing_month_file(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(
        "# Facts — 2026-04\n"
        "\n---\n\n"
        "- **id:** prior-001\n"
        "- **content:** Something else\n"
        "- **subject:** other-person\n"
        "- **category:** identity\n"
        "- **source_agent:** connector\n",
        encoding="utf-8",
    )
    upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="Birthday: 1953-09-15",
        source_agent="connector",
        idempotency_key="birthday",
        recorded_at="2026-04-19T12:00:00Z",
    )
    text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    # Header should not repeat
    assert text.count("# Facts — 2026-04") == 1
    # Both facts present
    assert "prior-001" in text
    assert "priya-rivera" in text


def test_upsert_fact_is_idempotent_on_key(tmp_path: Path):
    """Repeated calls with the same (subject, idempotency_key) don't duplicate."""
    facts_dir = tmp_path / "facts"
    for _ in range(3):
        result = upsert_fact(
            facts_dir=facts_dir,
            subject="priya-rivera",
            category="identity",
            content="Birthday: 1953-09-15",
            source_agent="connector",
            idempotency_key="birthday",
            recorded_at="2026-04-19T12:00:00Z",
        )
    # Last two calls should have status=skipped
    assert result["status"] == "skipped"
    # Only one fact entry on disk
    facts = parse_facts_file(facts_dir / "2026-04.md")
    birthday_facts = [f for f in facts if f["subject"] == "priya-rivera"]
    assert len(birthday_facts) == 1


def test_upsert_fact_idempotency_scans_all_months(tmp_path: Path):
    """A prior-month fact with the same id should block re-add in the new month."""
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    # Seed a birthday fact in March
    (facts_dir / "2026-03.md").write_text(
        "# Facts — 2026-03\n"
        "\n---\n\n"
        "- **id:** connector-priya-rivera-birthday\n"
        "- **content:** Birthday: 1953-09-15\n"
        "- **subject:** priya-rivera\n"
        "- **category:** identity\n"
        "- **source_agent:** connector\n",
        encoding="utf-8",
    )
    # Now upsert in April — should skip
    result = upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="Birthday: 1953-09-15",
        source_agent="connector",
        idempotency_key="birthday",
        recorded_at="2026-04-19T12:00:00Z",
    )
    assert result["status"] == "skipped"
    # April file should not exist
    assert not (facts_dir / "2026-04.md").exists()
