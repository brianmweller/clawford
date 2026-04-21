"""Tests for structured_facts_lib — per-type prompt builders, response
parsers, and writers for the typed self-facts layer.

Consumers (Huckle, Murphy, scouting) query these JSON files directly:
  self/facts/employers.json
  self/facts/target_companies.json
  self/facts/strength_themes.json
  self/facts/major_accomplishments.json
  self/facts/tenets_authored.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from structured_facts_lib import (  # type: ignore
    FACT_TYPES,
    build_extraction_prompt,
    merge_facts,
    parse_extraction_response,
    validate_record,
)


_SAMPLE_ARCHIVES = {
    "amazon.md": "# AMAZON — Prime Video WMS\n\nBrian led Worldwide Marketplace Science.",
    "linkedin.md": "# LINKEDIN — FLEX DS\n\nBrian authored FLEX DS Tenets in 2023.",
}


# --- FACT_TYPES sanity ---

def test_fact_types_contains_core_v1_types():
    for t in ["employer", "target_company", "strength_theme",
              "major_accomplishment", "tenet_authored"]:
        assert t in FACT_TYPES


# --- build_extraction_prompt ---

def test_build_extraction_prompt_includes_archive_names():
    prompt = build_extraction_prompt("employer", _SAMPLE_ARCHIVES)
    assert "amazon.md" in prompt
    assert "linkedin.md" in prompt


def test_build_extraction_prompt_includes_archive_content():
    prompt = build_extraction_prompt("employer", _SAMPLE_ARCHIVES)
    assert "Worldwide Marketplace Science" in prompt
    assert "FLEX DS Tenets" in prompt


def test_build_extraction_prompt_mentions_fact_type():
    prompt = build_extraction_prompt("tenet_authored", _SAMPLE_ARCHIVES)
    assert "tenet" in prompt.lower()


def test_build_extraction_prompt_requests_json_shape():
    """Every type's prompt must specify JSON output and the required schema."""
    for t in FACT_TYPES:
        prompt = build_extraction_prompt(t, _SAMPLE_ARCHIVES)
        assert "json" in prompt.lower() or "JSON" in prompt
        assert "records" in prompt or "facts" in prompt


def test_build_extraction_prompt_unknown_type_raises():
    with pytest.raises(ValueError):
        build_extraction_prompt("not_a_type", _SAMPLE_ARCHIVES)


# --- parse_extraction_response ---

def test_parse_extraction_response_valid_employer():
    raw = json.dumps({
        "records": [
            {
                "company": "Amazon",
                "title": "Head of Science, WMS",
                "start": "2020-09-01",
                "end": "2022-04-01",
                "reporting_line": "Jay Marine (VP Video)",
                "confidence": 0.95,
            }
        ]
    })
    result = parse_extraction_response("employer", raw)
    assert len(result) == 1
    r = result[0]
    assert r["company"] == "Amazon"
    assert r["title"] == "Head of Science, WMS"
    assert r["type"] == "employer"
    assert r["confidence"] == 0.95
    assert "id" in r
    assert r["id"].startswith("self-employer-")


def test_parse_extraction_response_handles_json_fences():
    raw = '```json\n{"records": [{"company": "Amazon", "title": "x", "start": "2020-01-01", "end": "2021-01-01"}]}\n```'
    result = parse_extraction_response("employer", raw)
    assert len(result) == 1


def test_parse_extraction_response_invalid_json_raises():
    with pytest.raises(ValueError):
        parse_extraction_response("employer", "not json")


def test_parse_extraction_response_missing_records_key_raises():
    with pytest.raises(ValueError):
        parse_extraction_response("employer", json.dumps({"wrong_key": []}))


def test_parse_extraction_response_drops_records_missing_required_fields():
    """If the LLM omits a required field, that record is dropped (not the whole response)."""
    raw = json.dumps({
        "records": [
            {"company": "Amazon", "title": "x", "start": "2020-01-01", "end": "2021-01-01"},  # valid
            {"title": "y"},   # missing required fields
            {"company": "LinkedIn", "title": "z", "start": "2022-11-01", "end": "2024-01-01"},  # valid
        ]
    })
    result = parse_extraction_response("employer", raw)
    assert len(result) == 2
    companies = {r["company"] for r in result}
    assert companies == {"Amazon", "LinkedIn"}


def test_parse_extraction_response_assigns_stable_ids():
    """Same company+title → same id on re-parsing. Supports idempotent merge."""
    raw = json.dumps({"records": [
        {"company": "Amazon", "title": "Head of Science, WMS", "start": "2020-09-01", "end": "2022-04-01"}
    ]})
    r1 = parse_extraction_response("employer", raw)
    r2 = parse_extraction_response("employer", raw)
    assert r1[0]["id"] == r2[0]["id"]


def test_parse_extraction_response_target_company_schema():
    raw = json.dumps({"records": [
        {"company": "Wayfair", "search_round": "search-post-airbnb",
         "role_type": "Chief Data Officer", "tier_implied": "A",
         "outcome": "in-progress"},
    ]})
    result = parse_extraction_response("target_company", raw)
    assert len(result) == 1
    assert result[0]["company"] == "Wayfair"
    assert result[0]["type"] == "target_company"


def test_parse_extraction_response_strength_theme_schema():
    raw = json.dumps({"records": [
        {"theme": "Marketplace systems thinking",
         "evidence_types": ["authored_work"],
         "across_roles": ["airbnb", "search-post-airbnb"],
         "confidence": 0.9},
    ]})
    result = parse_extraction_response("strength_theme", raw)
    assert len(result) == 1
    assert result[0]["theme"] == "Marketplace systems thinking"
    assert "airbnb" in result[0]["across_roles"]


def test_parse_extraction_response_major_accomplishment_schema():
    raw = json.dumps({"records": [
        {"role": "airbnb", "year": "2025",
         "summary": "Base Price Redesign reduced poor pricing by 51%",
         "metric": "51% reduction vs 5% target"},
    ]})
    result = parse_extraction_response("major_accomplishment", raw)
    assert len(result) == 1
    assert result[0]["role"] == "airbnb"


def test_parse_extraction_response_tenet_authored_schema():
    raw = json.dumps({"records": [
        {"name": "FLEX DS Tenets", "role": "linkedin",
         "doc_ref": "FLEX DS Tenets.docx",
         "year_approx": "2023", "description": "Operating principles"},
    ]})
    result = parse_extraction_response("tenet_authored", raw)
    assert len(result) == 1
    assert result[0]["name"] == "FLEX DS Tenets"


def test_parse_extraction_response_tenet_drops_records_without_doc_ref():
    """v2: tenet without a canonical source doc is almost always a
    hallucination. Drop such records."""
    raw = json.dumps({"records": [
        {"name": "FLEX DS Tenets", "role": "linkedin", "doc_ref": "FLEX DS Tenets.docx"},
        {"name": "Project Prometheus", "role": "airbnb"},  # no doc_ref → dropped
    ]})
    result = parse_extraction_response("tenet_authored", raw)
    assert len(result) == 1
    assert result[0]["name"] == "FLEX DS Tenets"


def test_parse_extraction_response_tenet_drops_doc_ref_without_file_extension():
    """v2: if the LLM just echoes the tenet name into doc_ref (e.g.,
    'Project Prometheus' as both name AND doc_ref), drop it — that's
    a hallucination bypass."""
    raw = json.dumps({"records": [
        {"name": "Project Prometheus", "role": "airbnb",
         "doc_ref": "Project Prometheus"},   # no file extension → dropped
        {"name": "FLEX DS Tenets", "role": "linkedin",
         "doc_ref": "FLEX DS Tenets.docx"},  # real filename → kept
    ]})
    result = parse_extraction_response("tenet_authored", raw)
    assert len(result) == 1
    assert result[0]["name"] == "FLEX DS Tenets"


# --- validate_record ---

def test_validate_record_employer_requires_core_fields():
    assert validate_record("employer", {"company": "X", "title": "Y", "start": "2020-01-01", "end": "2021-01-01"})
    assert not validate_record("employer", {"company": "X"})


def test_validate_record_target_company_requires_company_and_round():
    assert validate_record("target_company", {"company": "X", "search_round": "search-post-airbnb"})
    assert not validate_record("target_company", {"company": "X"})


# --- merge_facts ---

def test_merge_facts_is_idempotent_on_id():
    existing = [{"id": "self-employer-amazon", "company": "Amazon", "confidence": 0.8}]
    new = [{"id": "self-employer-amazon", "company": "Amazon", "confidence": 0.95}]
    merged = merge_facts(existing, new)
    assert len(merged) == 1
    # New confidence wins
    assert merged[0]["confidence"] == 0.95


def test_parse_extraction_response_employer_converts_exclusive_end_to_inclusive():
    """the operator expects end date to read as last-day-inclusive of tenure.
    If the LLM emits 2024-01-01 (exclusive boundary from timeline), the
    parser converts it to 2023-12-31."""
    raw = json.dumps({"records": [
        {"company": "LinkedIn", "title": "Director", "start": "2022-11-01", "end": "2024-01-01"},
        {"company": "Example Corp", "title": "Director", "start": "2024-05-01", "end": "2026-02-01"},
    ]})
    result = parse_extraction_response("employer", raw)
    assert result[0]["end"] == "2023-12-31"
    assert result[1]["end"] == "2026-01-31"


def test_parse_extraction_response_employer_preserves_non_first_day_end():
    """End dates NOT on the 1st of a month stay as-is (already inclusive)."""
    raw = json.dumps({"records": [
        {"company": "X", "title": "y", "start": "2020-01-01", "end": "2021-03-15"},
    ]})
    result = parse_extraction_response("employer", raw)
    assert result[0]["end"] == "2021-03-15"


def test_merge_facts_appends_new_ids():
    existing = [{"id": "self-employer-amazon", "company": "Amazon"}]
    new = [{"id": "self-employer-linkedin", "company": "LinkedIn"}]
    merged = merge_facts(existing, new)
    assert len(merged) == 2
    companies = {r["company"] for r in merged}
    assert companies == {"Amazon", "LinkedIn"}
