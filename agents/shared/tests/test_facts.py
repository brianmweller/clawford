"""Fact-file reader with audience_scope support.

parse_facts_file() reads a facts/YYYY-MM.md file and returns one dict per
entry. The schema extends monthly-archival.py's existing parser with an
audience_scope field (JSON list, comma-separated, or missing).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.shared.facts import parse_facts_file, load_facts_for_subject


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
