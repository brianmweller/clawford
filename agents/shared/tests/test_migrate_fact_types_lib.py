"""Tests for the pure helpers that power agents/shared/scripts/migrate-fact-types.py.

The migration has two classes of helpers tested here:

1. Parsing/serialization — iter_facts_to_classify reads a month file and
   yields only the blocks missing fact_type; rewrite_block_with_fact_type
   splices the new lines into a block preserving every other field.

2. LLM-pass shaping — build_classifier_prompt asks the LLM for
   {fact_type, value, migration_confidence}; parse_classifier_response
   validates the response and re-validates value against the per-type
   schema from fact_extraction.

The orchestration main() is exercised via a small smoke test in test_migrate_fact_types_script.py.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

_spec = importlib.util.spec_from_file_location(
    "migrate_fact_types_lib", _SCRIPTS_DIR / "migrate_fact_types_lib.py"
)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


_MONTH_TEXT = """# Facts — 2026-04

---

- **id:** connector-jamie-1
- **content:** Jamie's son Eliott, born approx 2021-06 (age ~5 as of 2026-04-21).
- **subject:** jamie-fitzgerald
- **category:** relationship
- **source_agent:** connector
- **confidence:** 0.9
- **recorded_at:** 2026-04-21T12:00:00Z
- **audience_scope:** ["personal", "family"]

---

- **id:** connector-jane-2
- **content:** Jane is Director of Data at Example Corp.
- **subject:** jane-doe
- **category:** role
- **source_agent:** connector
- **confidence:** 0.95
- **recorded_at:** 2026-04-21T12:00:00Z
- **fact_type:** employer
- **value:** {"company": "Example Corp", "status": "current"}

---

- **id:** connector-alex-3
- **content:** Alex mentioned a new client in DC.
- **subject:** alex-reyes
- **category:** event
- **source_agent:** connector
- **confidence:** 0.8
- **recorded_at:** 2026-04-21T12:00:00Z
"""


# ─── iter_facts_to_classify ──────────────────────────────────────────


def test_iter_yields_only_blocks_without_fact_type(tmp_path: Path):
    path = tmp_path / "2026-04.md"
    path.write_text(_MONTH_TEXT, encoding="utf-8")
    out = list(mig.iter_facts_to_classify(path))
    # Two of three facts have empty fact_type; one (jane-doe employer)
    # already has it set.
    ids = [fields["id"] for _raw, fields in out]
    assert "connector-jamie-1" in ids
    assert "connector-alex-3" in ids
    assert "connector-jane-2" not in ids


def test_iter_empty_on_missing_file(tmp_path: Path):
    assert list(mig.iter_facts_to_classify(tmp_path / "nope.md")) == []


# ─── rewrite_block_with_fact_type ────────────────────────────────────


def test_rewrite_inserts_fact_type_and_value_after_audience_scope():
    block = (
        "\n- **id:** x\n"
        "- **content:** c\n"
        "- **subject:** s\n"
        "- **category:** identity\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.9\n"
        "- **recorded_at:** 2026-04-21\n"
        '- **audience_scope:** ["personal"]\n'
    )
    out = mig.rewrite_block_with_fact_type(
        block, fact_type="birthdate",
        value={"year": 2021, "month": 6, "precision": "month"},
    )
    assert "- **fact_type:** birthdate" in out
    assert '"year": 2021' in out
    # Every original line is still present
    assert "- **id:** x" in out
    assert '- **audience_scope:** ["personal"]' in out


def test_rewrite_idempotent_when_block_already_typed():
    block = (
        "\n- **id:** x\n"
        "- **content:** c\n"
        "- **subject:** s\n"
        "- **category:** identity\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.9\n"
        "- **recorded_at:** 2026-04-21\n"
        "- **fact_type:** birthdate\n"
        '- **value:** {"year": 2021, "precision": "year"}\n'
    )
    out = mig.rewrite_block_with_fact_type(
        block, fact_type="birthdate",
        value={"year": 2022, "precision": "year"},
    )
    # Already-typed blocks are untouched — migration doesn't clobber
    # existing typing (idempotence guarantee from the plan)
    assert out == block


# ─── classifier prompt + response parsing ────────────────────────────


def test_build_classifier_prompt_names_all_four_types():
    prompt = mig.build_classifier_prompt(
        content="Jane is Director of Data at Example Corp.",
        subject="jane-doe",
        category="role",
    )
    low = prompt.lower()
    for t in ("birthdate", "employer", "role", "preference"):
        assert t in low
    # Must explicitly prompt for migration_confidence
    assert "migration_confidence" in prompt


def test_parse_classifier_accepts_well_formed_response():
    raw = (
        '{"fact_type": "employer", '
        '"value": {"company": "Example Corp", "status": "current", "role": "Director of Data"}, '
        '"migration_confidence": 0.92}'
    )
    parsed = mig.parse_classifier_response(raw)
    assert parsed["fact_type"] == "employer"
    assert parsed["value"]["company"] == "Example Corp"
    assert parsed["migration_confidence"] == 0.92


def test_parse_classifier_returns_none_shape_when_fact_type_is_none():
    raw = '{"fact_type": "none", "value": null, "migration_confidence": 0.3}'
    parsed = mig.parse_classifier_response(raw)
    assert parsed["fact_type"] == ""
    assert parsed["value"] is None
    assert parsed["migration_confidence"] == 0.3


def test_parse_classifier_drops_invalid_value_shape():
    # Claims employer but missing required company → revalidator rejects
    raw = (
        '{"fact_type": "employer", '
        '"value": {"status": "current"}, '
        '"migration_confidence": 0.95}'
    )
    parsed = mig.parse_classifier_response(raw)
    # Invalid value → fact_type cleared (don't apply typing)
    assert parsed["fact_type"] == ""
    assert parsed["value"] is None


def test_parse_classifier_strips_markdown_fences():
    raw = (
        "```json\n"
        '{"fact_type": "birthdate", '
        '"value": {"year": 2021, "month": 6, "precision": "month"}, '
        '"migration_confidence": 0.85}\n'
        "```"
    )
    parsed = mig.parse_classifier_response(raw)
    assert parsed["fact_type"] == "birthdate"
    assert parsed["value"]["year"] == 2021


def test_parse_classifier_malformed_json_returns_none_shape():
    raw = "lol this is not json at all"
    parsed = mig.parse_classifier_response(raw)
    assert parsed["fact_type"] == ""
    assert parsed["value"] is None
    assert parsed["migration_confidence"] == 0.0


# ─── Route decision ──────────────────────────────────────────────────


def test_routes_high_confidence_to_apply():
    assert mig.classify_route(0.85) == "apply"
    assert mig.classify_route(0.8) == "apply"


def test_routes_medium_confidence_to_queue():
    assert mig.classify_route(0.65) == "queue"
    assert mig.classify_route(0.5) == "queue"


def test_routes_low_confidence_to_skip():
    assert mig.classify_route(0.45) == "skip"
    assert mig.classify_route(0.0) == "skip"


# ─── rewrite_month_file ──────────────────────────────────────────────


def test_rewrite_month_file_preserves_unrelated_blocks(tmp_path: Path):
    path = tmp_path / "2026-04.md"
    path.write_text(_MONTH_TEXT, encoding="utf-8")
    applied = mig.rewrite_month_file(
        path,
        {"connector-alex-3": ("role", {"title": "Account Executive", "status": "current"})},
    )
    assert applied == 1
    new_text = path.read_text(encoding="utf-8")
    # Target block got typed
    assert "connector-alex-3" in new_text
    # The already-typed employer block is untouched
    assert "connector-jane-2" in new_text
    assert '"company": "Example Corp"' in new_text
    # Alex now carries fact_type + value
    alex_section = new_text[new_text.index("connector-alex-3"):]
    assert "- **fact_type:** role" in alex_section
    assert '"Account Executive"' in alex_section


def test_rewrite_month_file_atomic(tmp_path: Path):
    # No .tmp file leftover after rewrite
    path = tmp_path / "2026-04.md"
    path.write_text(_MONTH_TEXT, encoding="utf-8")
    mig.rewrite_month_file(
        path,
        {"connector-alex-3": ("role", {"title": "AE", "status": "current"})},
    )
    assert not path.with_suffix(".md.tmp").exists()
