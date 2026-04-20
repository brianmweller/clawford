"""Pure helpers for facts-scope-augment.py.

Tags Huckle-native facts (created before Flux import) with audience_scope
so the confidentiality filter has something to go on. Without explicit
scope, Flux-compatible default treats facts as visible-to-all — fine as
a baseline but tightens well with explicit tags.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from facts_scope_augment_lib import (  # type: ignore
    build_scope_classifier_prompt,
    load_untagged_facts,
    parse_scope_response,
    rewrite_facts_file_with_scopes,
)


SAMPLE_FACTS_MD = """\
# Facts — 2026-04

---

- **id:** f-001
- **content:** Jay is the operator's father.
- **subject:** ravi-rivera
- **source_type:** direct
- **source_agent:** connector
- **confidence:** 0.95
- **category:** identity
- **recorded_at:** 2026-04-10

---

- **id:** f-002
- **content:** Josh is an Anthropic recruiter.
- **subject:** josh-cherry
- **source_type:** direct
- **source_agent:** connector
- **confidence:** 0.9
- **category:** relationship
- **recorded_at:** 2026-04-12
- **audience_scope:** ["professional"]

---

- **id:** f-003
- **content:** Ellen likes chamomile tea.
- **subject:** ellen-von-geyso
- **source_type:** direct
- **source_agent:** connector
- **confidence:** 0.85
- **category:** preference
- **recorded_at:** 2026-04-15
"""


# --- load_untagged_facts ---

def test_load_skips_facts_with_existing_scope(tmp_path: Path):
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "2026-04.md").write_text(SAMPLE_FACTS_MD, encoding="utf-8")
    untagged = load_untagged_facts(facts)
    assert len(untagged) == 2   # f-001 and f-003 missing scope
    ids = {f["id"] for f in untagged}
    assert ids == {"f-001", "f-003"}


def test_load_empty_dir_returns_empty(tmp_path: Path):
    assert load_untagged_facts(tmp_path / "nope") == []


# --- build_scope_classifier_prompt ---

def test_prompt_includes_all_fact_ids_and_content():
    batch = [
        {"id": "f-001", "content": "Jay is the operator's father.", "subject": "ravi-rivera", "category": "identity"},
        {"id": "f-003", "content": "Ellen likes chamomile tea.", "subject": "ellen-von-geyso", "category": "preference"},
    ]
    prompt = build_scope_classifier_prompt(batch)
    assert "f-001" in prompt
    assert "f-003" in prompt
    assert "Jay is the operator's father" in prompt
    assert "chamomile tea" in prompt


def test_prompt_enumerates_valid_scope_tags():
    prompt = build_scope_classifier_prompt([])
    for tag in ("professional", "personal", "family", "friends"):
        assert tag in prompt


# --- parse_scope_response ---

def test_parse_valid_response():
    raw = json.dumps({
        "f-001": ["personal", "family"],
        "f-003": ["personal"],
    })
    out = parse_scope_response(raw, fact_ids={"f-001", "f-003"})
    assert out["f-001"] == ["personal", "family"]
    assert out["f-003"] == ["personal"]


def test_parse_strips_markdown_fences():
    raw = "```json\n" + json.dumps({"f-001": ["family"]}) + "\n```"
    out = parse_scope_response(raw, fact_ids={"f-001"})
    assert out["f-001"] == ["family"]


def test_parse_drops_unknown_ids():
    raw = json.dumps({"f-001": ["family"], "f-hallucinated": ["personal"]})
    out = parse_scope_response(raw, fact_ids={"f-001"})
    assert "f-001" in out
    assert "f-hallucinated" not in out


def test_parse_malformed_returns_empty():
    assert parse_scope_response("not json", fact_ids={"f-001"}) == {}


def test_parse_filters_invalid_scope_tags():
    raw = json.dumps({"f-001": ["family", "made-up-tag"], "f-002": ["garbage"]})
    out = parse_scope_response(raw, fact_ids={"f-001", "f-002"})
    assert out["f-001"] == ["family"]
    # f-002 had no valid tags → dropped
    assert "f-002" not in out


# --- rewrite_facts_file_with_scopes ---

def test_rewrite_adds_scope_line_to_untagged_fact(tmp_path: Path):
    facts = tmp_path / "facts"
    facts.mkdir()
    month_file = facts / "2026-04.md"
    month_file.write_text(SAMPLE_FACTS_MD, encoding="utf-8")
    scope_map = {"f-001": ["personal", "family"], "f-003": ["personal"]}
    rewrite_facts_file_with_scopes(month_file, scope_map)
    text = month_file.read_text(encoding="utf-8")
    # f-001 now has scope
    block_f001 = text.split("- **id:** f-001", 1)[1].split("---", 1)[0]
    assert "audience_scope" in block_f001
    assert "personal" in block_f001
    assert "family" in block_f001
    # f-003 now has scope
    block_f003 = text.split("- **id:** f-003", 1)[1]
    assert "audience_scope" in block_f003
    # f-002 unchanged (had scope already)
    block_f002 = text.split("- **id:** f-002", 1)[1].split("---", 1)[0]
    assert block_f002.count("audience_scope") == 1   # not duplicated


def test_rewrite_preserves_idempotency(tmp_path: Path):
    facts = tmp_path / "facts"
    facts.mkdir()
    month_file = facts / "2026-04.md"
    month_file.write_text(SAMPLE_FACTS_MD, encoding="utf-8")
    scope_map = {"f-001": ["family"]}
    rewrite_facts_file_with_scopes(month_file, scope_map)
    text1 = month_file.read_text(encoding="utf-8")
    rewrite_facts_file_with_scopes(month_file, scope_map)
    text2 = month_file.read_text(encoding="utf-8")
    assert text1 == text2
