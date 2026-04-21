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


def test_load_facts_for_subject_default_filters_sub_review_confidence(tmp_path: Path):
    """Default min_confidence filter is REVIEW_CONFIDENCE (0.6). Facts
    below that have been flagged in _pending_review.md and must not
    leak into composer context until the operator triages them up.
    Prior to 2026-04-20 every caller had to filter post-load, which
    was easy to forget; centralizing the default here closes the gap."""
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(
        "- **id:** high\n"
        "- **content:** High-conf fact\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.8\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **source_agent:** connector\n"
        "---\n"
        "- **id:** low\n"
        "- **content:** Low-conf fact\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.45\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **source_agent:** connector\n",
        encoding="utf-8",
    )
    facts = load_facts_for_subject("priya-rivera", facts_dir)
    assert [f["id"] for f in facts] == ["high"], (
        "Low-conf fact must be filtered out by the default threshold"
    )


def test_load_facts_for_subject_explicit_min_confidence_zero_returns_all(tmp_path: Path):
    """Callers that need everything (audit tools, confidence-floor
    bypass, _pending_review triage UIs) can pass min_confidence=0 to
    disable the default filter."""
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(
        "- **id:** high\n"
        "- **content:** H\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.9\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **source_agent:** connector\n"
        "---\n"
        "- **id:** mid\n"
        "- **content:** M\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.45\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **source_agent:** connector\n"
        "---\n"
        "- **id:** low\n"
        "- **content:** L\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.1\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **source_agent:** connector\n",
        encoding="utf-8",
    )
    facts = load_facts_for_subject("priya-rivera", facts_dir, min_confidence=0.0)
    assert {f["id"] for f in facts} == {"high", "mid", "low"}


def test_load_facts_for_subject_custom_min_confidence(tmp_path: Path):
    """A caller can tighten or loosen the threshold. Tight: 0.8 only
    returns certain facts."""
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(
        "- **id:** very-high\n"
        "- **content:** VH\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.9\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **source_agent:** connector\n"
        "---\n"
        "- **id:** mid-high\n"
        "- **content:** MH\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.7\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **source_agent:** connector\n",
        encoding="utf-8",
    )
    facts = load_facts_for_subject(
        "priya-rivera", facts_dir, min_confidence=0.8,
    )
    assert [f["id"] for f in facts] == ["very-high"]


def test_load_facts_for_subject_uses_index_when_fresh(tmp_path: Path):
    """With a fresh brain_index present, the loader uses the fast
    path and returns the same set it would have via full scan."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import brain_index as brain_index_mod

    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(SAMPLE_FACTS, encoding="utf-8")
    idx = brain_index_mod.rebuild_index(facts_dir)
    brain_index_mod.save_index(facts_dir, idx)

    facts = load_facts_for_subject("priya-rivera", facts_dir, min_confidence=0.0)
    assert [f["id"] for f in facts] == ["f-001"]


def test_load_facts_for_subject_falls_back_when_index_stale(tmp_path: Path):
    """If a miner writes a new fact after the last index rebuild, the
    index is stale for that month. Loader must detect and fall back to
    the slow path so the new fact is still visible."""
    import sys
    import time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import brain_index as brain_index_mod

    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(SAMPLE_FACTS, encoding="utf-8")
    idx = brain_index_mod.rebuild_index(facts_dir)
    brain_index_mod.save_index(facts_dir, idx)

    # Simulate a miner appending a new fact for priya after index build.
    time.sleep(0.01)
    with open(facts_dir / "2026-04.md", "a", encoding="utf-8") as f:
        f.write(
            "\n---\n"
            "- **id:** f-new\n"
            "- **content:** Fresh fact\n"
            "- **subject:** priya-rivera\n"
            "- **confidence:** 0.9\n"
            "- **category:** identity\n"
            "- **recorded_at:** 2026-04-20\n"
            "- **source_agent:** connector\n"
        )

    facts = load_facts_for_subject("priya-rivera", facts_dir, min_confidence=0.0)
    ids = {f["id"] for f in facts}
    # Slow-path fallback must include the fresh fact AND the pre-existing
    # one.
    assert "f-new" in ids
    assert "f-001" in ids


def test_load_facts_for_subject_ignores_underscore_files(tmp_path: Path):
    """Slow-path glob should skip _pending_review.md and similar
    operator-internal files. Regression guard: those don't hold
    regular fact blocks and including them was a silent bug before
    the Phase 2c rewrite."""
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(SAMPLE_FACTS, encoding="utf-8")
    (facts_dir / "_pending_review.md").write_text(
        "- **id:** pending-001\n"
        "- **content:** Low-conf flagged\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.45\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **source_agent:** connector\n",
        encoding="utf-8",
    )
    # No index → slow path.
    facts = load_facts_for_subject("priya-rivera", facts_dir, min_confidence=0.0)
    ids = {f["id"] for f in facts}
    assert "pending-001" not in ids
    assert "f-001" in ids


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
    """Repeated calls with the same (subject, idempotency_key) don't
    duplicate. Post-reinforcement (2026-04-20): the second+ calls
    return status=reinforced instead of skipped, but the
    no-duplication invariant still holds — only one fact block on
    disk regardless of how many times we upsert."""
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
    assert result["status"] == "reinforced"
    # Only one fact entry on disk.
    facts = parse_facts_file(facts_dir / "2026-04.md")
    birthday_facts = [f for f in facts if f["subject"] == "priya-rivera"]
    assert len(birthday_facts) == 1


def test_upsert_fact_reinforces_on_collision_bumps_confidence(tmp_path: Path):
    """Flux-style reinforcement: re-observing the same fact bumps
    confidence and updates last_reinforced_at. Regression for the
    old 'skipped on idempotency collision' behavior that threw away
    the signal that a fact was seen multiple times."""
    facts_dir = tmp_path / "facts"
    # First write: confidence 0.7.
    first = upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="She's moving to Austin in June",
        source_agent="connector",
        idempotency_key="austin-move",
        recorded_at="2026-04-19T12:00:00Z",
        confidence=0.7,
    )
    assert first["status"] == "created"
    # Re-observation: same idempotency_key, later timestamp.
    second = upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="She's moving to Austin in June",
        source_agent="connector",
        idempotency_key="austin-move",
        recorded_at="2026-04-20T09:00:00Z",
        confidence=0.7,
    )
    assert second["status"] == "reinforced"
    assert second["new_confidence"] == pytest.approx(0.75)
    # On-disk: confidence bumped, last_reinforced_at updated.
    text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "- **confidence:** 0.75" in text
    assert "- **last_reinforced_at:** 2026-04-20T09:00:00Z" in text
    # Still only one fact block (not appended).
    facts = parse_facts_file(facts_dir / "2026-04.md")
    assert len(facts) == 1


def test_upsert_fact_reinforcement_caps_confidence_at_0_95(tmp_path: Path):
    """Repeated reinforcement must asymptote, not run away past 1.0.
    Cap at 0.95 — a ceiling below 1.0 leaves room for 'this is a
    verified/certain fact' to remain semantically distinct from
    'I've seen this a lot'."""
    facts_dir = tmp_path / "facts"
    upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="Birthday: 1953-09-15",
        source_agent="connector",
        idempotency_key="birthday",
        recorded_at="2026-04-19T12:00:00Z",
        confidence=0.93,  # Already near cap.
    )
    # Re-observe three times.
    for i in range(3):
        result = upsert_fact(
            facts_dir=facts_dir,
            subject="priya-rivera",
            category="identity",
            content="Birthday: 1953-09-15",
            source_agent="connector",
            idempotency_key="birthday",
            recorded_at=f"2026-04-{20+i:02d}T12:00:00Z",
            confidence=0.93,
        )
    # 0.93 → 0.95 (capped) → 0.95 → 0.95.
    assert result["new_confidence"] == pytest.approx(0.95)
    assert result["status"] == "reinforced"


def test_upsert_fact_reinforcement_finds_fact_in_prior_month(tmp_path: Path):
    """Reinforcement must work even when the fact was originally
    written in a different monthly file. The bump-and-update must
    rewrite the ORIGINAL file, not create a duplicate in the new
    month."""
    facts_dir = tmp_path / "facts"
    # Seed in March.
    upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="Old fact",
        source_agent="connector",
        idempotency_key="old",
        recorded_at="2026-03-15T12:00:00Z",
        confidence=0.6,
    )
    # Re-observe in April.
    result = upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="Old fact",
        source_agent="connector",
        idempotency_key="old",
        recorded_at="2026-04-05T12:00:00Z",
        confidence=0.6,
    )
    assert result["status"] == "reinforced"
    # April file should NOT exist.
    assert not (facts_dir / "2026-04.md").exists()
    # March file should have the bumped confidence + new last_reinforced_at.
    text = (facts_dir / "2026-03.md").read_text(encoding="utf-8")
    assert "- **confidence:** 0.65" in text
    assert "- **last_reinforced_at:** 2026-04-05T12:00:00Z" in text


def test_parse_facts_file_reads_last_reinforced_at(tmp_path: Path):
    """Back-compat: parse_facts_file yields last_reinforced_at when
    present; absent when the field isn't written yet."""
    p = tmp_path / "2026-04.md"
    p.write_text(
        "- **id:** f-001\n"
        "- **content:** X\n"
        "- **subject:** priya-rivera\n"
        "- **confidence:** 0.8\n"
        "- **category:** identity\n"
        "- **recorded_at:** 2026-04-10\n"
        "- **last_reinforced_at:** 2026-04-19T12:00:00Z\n"
        "- **source_agent:** connector\n"
        "---\n"
        "- **id:** f-002\n"
        "- **content:** Y\n"
        "- **subject:** aaron-nuti\n"
        "- **confidence:** 0.7\n"
        "- **category:** work\n"
        "- **recorded_at:** 2026-04-12\n"
        "- **source_agent:** connector\n",
        encoding="utf-8",
    )
    facts = parse_facts_file(p)
    assert facts[0]["last_reinforced_at"] == "2026-04-19T12:00:00Z"
    # Missing field → defaults to recorded_at (treat as "not yet reinforced").
    assert facts[1]["last_reinforced_at"] == "2026-04-12"


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
    # Now upsert in April — should reinforce the March fact in place.
    result = upsert_fact(
        facts_dir=facts_dir,
        subject="priya-rivera",
        category="identity",
        content="Birthday: 1953-09-15",
        source_agent="connector",
        idempotency_key="birthday",
        recorded_at="2026-04-19T12:00:00Z",
    )
    assert result["status"] == "reinforced"
    # April file should still not exist — reinforcement rewrites the
    # original monthly file, not the current-month one.
    assert not (facts_dir / "2026-04.md").exists()


# ─── mention_slugs roundtrip ────────────────────────────────────────

def test_upsert_fact_writes_mention_slugs_as_json_list(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    upsert_fact(
        facts_dir=facts_dir,
        subject="jamie-fitzgerald",
        category="relationship",
        content="Eliott and Arthur are Jamie's kids.",
        source_agent="connector",
        idempotency_key="kids-claim",
        recorded_at="2026-04-21T12:00:00Z",
        audience_scope=["personal", "family"],
        mention_slugs=["eliott-fitzgerald", "arthur-fitzgerald"],
    )
    text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert '- **mention_slugs:** ["eliott-fitzgerald", "arthur-fitzgerald"]' in text


def test_upsert_fact_omits_mention_slugs_line_when_empty(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    upsert_fact(
        facts_dir=facts_dir,
        subject="jamie-fitzgerald",
        category="event",
        content="Jamie is exploring opportunities.",
        source_agent="connector",
        idempotency_key="exploring",
        recorded_at="2026-04-21T12:00:00Z",
        audience_scope=["professional"],
        mention_slugs=[],
    )
    text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "mention_slugs" not in text


def test_upsert_fact_omits_mention_slugs_line_when_none(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    upsert_fact(
        facts_dir=facts_dir,
        subject="jamie-fitzgerald",
        category="event",
        content="Jamie is exploring opportunities.",
        source_agent="connector",
        idempotency_key="exploring2",
        recorded_at="2026-04-21T12:00:00Z",
        audience_scope=["professional"],
    )
    text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "mention_slugs" not in text


def test_parse_facts_file_extracts_mention_slugs(tmp_path: Path):
    p = tmp_path / "2026-04.md"
    p.write_text(
        "# Facts — 2026-04\n"
        "\n---\n\n"
        "- **id:** connector-jamie-1\n"
        "- **content:** Eliott and Arthur are Jamie's kids.\n"
        "- **subject:** jamie-fitzgerald\n"
        "- **category:** relationship\n"
        '- **audience_scope:** ["personal", "family"]\n'
        '- **mention_slugs:** ["eliott-fitzgerald", "arthur-fitzgerald"]\n'
        "- **source_agent:** connector\n"
        "- **recorded_at:** 2026-04-21T12:00:00Z\n",
        encoding="utf-8",
    )
    facts = parse_facts_file(p)
    assert len(facts) == 1
    assert facts[0]["mention_slugs"] == ["eliott-fitzgerald", "arthur-fitzgerald"]


def test_parse_facts_file_missing_mention_slugs_is_empty_list(tmp_path: Path):
    p = tmp_path / "2026-04.md"
    p.write_text(
        "# Facts — 2026-04\n"
        "\n---\n\n"
        "- **id:** connector-jamie-2\n"
        "- **content:** Jamie likes email.\n"
        "- **subject:** jamie-fitzgerald\n"
        "- **category:** preference\n"
        "- **source_agent:** connector\n"
        "- **recorded_at:** 2026-04-21T12:00:00Z\n",
        encoding="utf-8",
    )
    facts = parse_facts_file(p)
    assert len(facts) == 1
    assert facts[0].get("mention_slugs", []) == []


# ─── Retrieval: union subject + mention_slugs ───────────────────────

def _seed_jamie_fact_with_mentions(facts_dir: Path) -> None:
    facts_dir.mkdir(parents=True, exist_ok=True)
    (facts_dir / "2026-04.md").write_text(
        "# Facts — 2026-04\n"
        "\n---\n\n"
        "- **id:** connector-jamie-kids\n"
        "- **content:** Eliott and Arthur are Jamie's kids.\n"
        "- **subject:** jamie-fitzgerald\n"
        "- **confidence:** 0.91\n"
        "- **category:** relationship\n"
        "- **source_agent:** connector\n"
        "- **recorded_at:** 2026-04-21T12:00:00Z\n"
        '- **audience_scope:** ["personal", "family"]\n'
        '- **mention_slugs:** ["eliott-fitzgerald", "arthur-fitzgerald"]\n',
        encoding="utf-8",
    )


def test_load_facts_for_subject_returns_mentions(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    _seed_jamie_fact_with_mentions(facts_dir)
    eliott_facts = load_facts_for_subject("eliott-fitzgerald", facts_dir)
    assert len(eliott_facts) == 1
    assert eliott_facts[0]["subject"] == "jamie-fitzgerald"
    assert "eliott-fitzgerald" in eliott_facts[0]["mention_slugs"]


def test_load_facts_for_subject_unions_subject_and_mentions_without_duplicates(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    _seed_jamie_fact_with_mentions(facts_dir)
    # Add a direct fact about Eliott (same subject)
    (facts_dir / "2026-04.md").write_text(
        (facts_dir / "2026-04.md").read_text(encoding="utf-8") +
        "\n---\n\n"
        "- **id:** connector-eliott-pref\n"
        "- **content:** Eliott likes trucks.\n"
        "- **subject:** eliott-fitzgerald\n"
        "- **confidence:** 0.9\n"
        "- **category:** preference\n"
        "- **source_agent:** connector\n"
        "- **recorded_at:** 2026-04-22T12:00:00Z\n",
        encoding="utf-8",
    )
    eliott_facts = load_facts_for_subject("eliott-fitzgerald", facts_dir)
    ids = [f["id"] for f in eliott_facts]
    assert "connector-eliott-pref" in ids
    assert "connector-jamie-kids" in ids
    # No duplicate
    assert len(ids) == len(set(ids))


def test_upsert_fact_writes_fact_type_and_value_json(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    upsert_fact(
        facts_dir=facts_dir,
        subject="eliott-fitzgerald",
        category="identity",
        content="Eliott was born approx 2021-06.",
        source_agent="connector",
        idempotency_key="birth-claim",
        recorded_at="2026-04-21T12:00:00Z",
        audience_scope=["personal", "family"],
        fact_type="birthdate",
        value={"year": 2021, "month": 6, "day": None, "precision": "month"},
    )
    text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "- **fact_type:** birthdate" in text
    # value line must be JSON-serialized so structured readers can round-trip
    assert '"year": 2021' in text
    assert '"precision": "month"' in text


def test_upsert_fact_omits_fact_type_line_when_empty(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    upsert_fact(
        facts_dir=facts_dir,
        subject="jane-doe",
        category="event",
        content="Jane moved to Seattle.",
        source_agent="connector",
        idempotency_key="move",
        recorded_at="2026-04-21T12:00:00Z",
        audience_scope=["personal"],
    )
    text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "fact_type" not in text
    # Narrative content is unchanged for untyped facts
    assert "Jane moved to Seattle." in text


def test_upsert_fact_omits_value_line_when_none(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    upsert_fact(
        facts_dir=facts_dir,
        subject="jane-doe",
        category="identity",
        content="x",
        source_agent="connector",
        idempotency_key="typed-no-value",
        recorded_at="2026-04-21T12:00:00Z",
        audience_scope=["personal"],
        fact_type="birthdate",
        value=None,
    )
    text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    # fact_type writes but value doesn't when None
    assert "- **fact_type:** birthdate" in text
    # Must not emit a bare `- **value:**` line when value is None
    assert "- **value:**" not in text


def test_parse_facts_file_roundtrips_fact_type_and_value(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(
        "# Facts — 2026-04\n"
        "\n---\n\n"
        "- **id:** connector-eliott-birth\n"
        "- **content:** Eliott was born approx 2021-06.\n"
        "- **subject:** eliott-fitzgerald\n"
        "- **category:** identity\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.7\n"
        "- **recorded_at:** 2026-04-21T12:00:00Z\n"
        "- **fact_type:** birthdate\n"
        '- **value:** {"year": 2021, "month": 6, "precision": "month"}\n',
        encoding="utf-8",
    )
    facts = parse_facts_file(facts_dir / "2026-04.md")
    assert len(facts) == 1
    assert facts[0]["fact_type"] == "birthdate"
    assert facts[0]["value"] == {"year": 2021, "month": 6, "precision": "month"}


def test_parse_facts_file_missing_fact_type_is_empty_string(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(
        "# Facts — 2026-04\n"
        "\n---\n\n"
        "- **id:** x\n"
        "- **content:** narrative-only fact\n"
        "- **subject:** jane-doe\n"
        "- **category:** event\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.8\n"
        "- **recorded_at:** 2026-04-21T12:00:00Z\n",
        encoding="utf-8",
    )
    facts = parse_facts_file(facts_dir / "2026-04.md")
    assert len(facts) == 1
    assert facts[0].get("fact_type", "") == ""
    assert facts[0].get("value") is None


def test_parse_facts_file_tolerates_malformed_value_json(tmp_path: Path):
    # A corrupted value line should degrade to None, never raise
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(
        "# Facts — 2026-04\n"
        "\n---\n\n"
        "- **id:** x\n"
        "- **content:** y\n"
        "- **subject:** jane-doe\n"
        "- **category:** identity\n"
        "- **source_agent:** connector\n"
        "- **confidence:** 0.9\n"
        "- **recorded_at:** 2026-04-21T12:00:00Z\n"
        "- **fact_type:** birthdate\n"
        "- **value:** {not valid json\n",
        encoding="utf-8",
    )
    facts = parse_facts_file(facts_dir / "2026-04.md")
    assert facts[0]["fact_type"] == "birthdate"
    assert facts[0].get("value") is None  # graceful degrade


def test_load_facts_for_subject_respects_min_confidence_on_mention_matches(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "2026-04.md").write_text(
        "# Facts — 2026-04\n"
        "\n---\n\n"
        "- **id:** connector-jamie-lowconf\n"
        "- **content:** Maybe Eliott plays soccer.\n"
        "- **subject:** jamie-fitzgerald\n"
        "- **confidence:** 0.4\n"
        "- **category:** guess\n"
        "- **source_agent:** connector\n"
        "- **recorded_at:** 2026-04-21T12:00:00Z\n"
        '- **mention_slugs:** ["eliott-fitzgerald"]\n',
        encoding="utf-8",
    )
    assert load_facts_for_subject("eliott-fitzgerald", facts_dir) == []
    # But pass min_confidence=0 and it surfaces
    assert len(load_facts_for_subject("eliott-fitzgerald", facts_dir, min_confidence=0.0)) == 1
