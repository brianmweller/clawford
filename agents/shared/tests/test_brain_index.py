"""brain_index — per-subject fact index over brain/facts/*.md.

Goal: `load_facts_for_subject()` doesn't have to re-parse every monthly
file on every call. With 600+ facts today and 20-50 new/day under the
6h miner cadence, the brain will cross 1K soon. An index maps
subject → list of [month, fact_id] so readers can open only the
files that actually contain facts about the subject.

Staleness: the index is rebuilt nightly by a cron. If any monthly
file was modified AFTER the index's built_at timestamp, callers fall
back to the slow path — the index is a hint, not a source of truth.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import brain_index  # type: ignore


def _write_fact_file(path: Path, facts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f"# Facts — {path.stem}\n"]
    for f in facts:
        parts.append("\n---\n\n")
        parts.append(f"- **id:** {f['id']}\n")
        parts.append(f"- **content:** {f.get('content', 'X')}\n")
        parts.append(f"- **subject:** {f['subject']}\n")
        parts.append(f"- **confidence:** {f.get('confidence', 0.8)}\n")
        parts.append(f"- **category:** {f.get('category', 'identity')}\n")
        parts.append(f"- **recorded_at:** {f.get('recorded_at', '2026-04-10')}\n")
        parts.append("- **source_agent:** connector\n")
    path.write_text("".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# rebuild_index
# ---------------------------------------------------------------------------


def test_rebuild_index_empty_dir_returns_empty_mapping(tmp_path: Path) -> None:
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    idx = brain_index.rebuild_index(facts_dir)
    assert idx["by_subject"] == {}
    assert "built_at" in idx


def test_rebuild_index_maps_subjects_to_month_and_fact_id(tmp_path: Path) -> None:
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "priya-rivera"},
        {"id": "f-002", "subject": "aaron-nuti"},
        {"id": "f-003", "subject": "priya-rivera"},
    ])
    idx = brain_index.rebuild_index(facts_dir)
    leslie_entries = idx["by_subject"]["priya-rivera"]
    assert {tuple(e) for e in leslie_entries} == {
        ("2026-04", "f-001"),
        ("2026-04", "f-003"),
    }
    assert idx["by_subject"]["aaron-nuti"] == [["2026-04", "f-002"]]


def test_rebuild_index_spans_multiple_months(tmp_path: Path) -> None:
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-03.md", [
        {"id": "f-old", "subject": "priya-rivera"},
    ])
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-new", "subject": "priya-rivera"},
    ])
    idx = brain_index.rebuild_index(facts_dir)
    entries = {tuple(e) for e in idx["by_subject"]["priya-rivera"]}
    assert entries == {("2026-03", "f-old"), ("2026-04", "f-new")}


def test_rebuild_index_subject_is_lowercased_for_lookup_stability(tmp_path: Path) -> None:
    """Fact subjects in the wild can arrive with mixed case. The index
    stores lowercase keys so callers don't have to normalize before
    lookup."""
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "Priya-Rivera"},
    ])
    idx = brain_index.rebuild_index(facts_dir)
    assert "priya-rivera" in idx["by_subject"]
    assert "Priya-Rivera" not in idx["by_subject"]


# ---------------------------------------------------------------------------
# save / load / index file location
# ---------------------------------------------------------------------------


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "priya-rivera"},
    ])
    built = brain_index.rebuild_index(facts_dir)
    brain_index.save_index(facts_dir, built)

    loaded = brain_index.load_index(facts_dir)
    assert loaded is not None
    assert loaded["by_subject"] == built["by_subject"]


def test_load_index_missing_file_returns_none(tmp_path: Path) -> None:
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    assert brain_index.load_index(facts_dir) is None


def test_load_index_corrupt_json_returns_none(tmp_path: Path) -> None:
    """Corrupt index file shouldn't crash the reader — treat as absent
    so the fallback slow path kicks in and subsequent rebuild fixes it."""
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "_index.json").write_text("not-json{", encoding="utf-8")
    assert brain_index.load_index(facts_dir) is None


def test_save_index_uses_underscore_prefix(tmp_path: Path) -> None:
    """Filename is `_index.json`: the underscore sorts before every
    monthly YYYY-MM.md file so `ls` surfaces it at the top of the
    facts/ directory listing."""
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    brain_index.save_index(facts_dir, {"by_subject": {}, "built_at": "x"})
    assert (facts_dir / "_index.json").exists()


# ---------------------------------------------------------------------------
# Staleness check — is the index still valid?
# ---------------------------------------------------------------------------


def test_is_fresh_true_when_no_fact_files_newer_than_index(tmp_path: Path) -> None:
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "priya-rivera"},
    ])
    idx = brain_index.rebuild_index(facts_dir)
    # Backdate the fact file so the index is "newer" than it.
    past = time.time() - 60
    import os
    os.utime(facts_dir / "2026-04.md", (past, past))
    assert brain_index.is_fresh(facts_dir, idx) is True


def test_is_fresh_false_when_fact_file_modified_after_index_build(tmp_path: Path) -> None:
    """A miner wrote a new fact after the last rebuild — index is
    stale for that month and callers must fall back to the slow path."""
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "priya-rivera"},
    ])
    idx = brain_index.rebuild_index(facts_dir)
    # Simulate a fresh write after index build.
    time.sleep(0.01)
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "priya-rivera"},
        {"id": "f-002", "subject": "aaron-nuti"},
    ])
    assert brain_index.is_fresh(facts_dir, idx) is False


def test_is_fresh_ignores_own_index_file(tmp_path: Path) -> None:
    """Writing the index itself must not flag the index as stale.
    Regression guard: is_fresh compares against monthly *.md files
    only, never the index JSON."""
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "priya-rivera"},
    ])
    idx = brain_index.rebuild_index(facts_dir)
    brain_index.save_index(facts_dir, idx)  # touches _index.json AFTER build
    loaded = brain_index.load_index(facts_dir)
    assert brain_index.is_fresh(facts_dir, loaded) is True


# ---------------------------------------------------------------------------
# facts_for_subject accessor
# ---------------------------------------------------------------------------


def test_facts_for_subject_returns_entries_for_known_slug(tmp_path: Path) -> None:
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "priya-rivera"},
        {"id": "f-002", "subject": "aaron-nuti"},
    ])
    idx = brain_index.rebuild_index(facts_dir)
    entries = brain_index.facts_for_subject(idx, "priya-rivera")
    assert entries == [["2026-04", "f-001"]]


def test_facts_for_subject_unknown_slug_returns_empty(tmp_path: Path) -> None:
    idx = {"by_subject": {}, "built_at": "x"}
    assert brain_index.facts_for_subject(idx, "ghost-person") == []


def test_facts_for_subject_case_insensitive_lookup(tmp_path: Path) -> None:
    facts_dir = tmp_path / "facts"
    _write_fact_file(facts_dir / "2026-04.md", [
        {"id": "f-001", "subject": "priya-rivera"},
    ])
    idx = brain_index.rebuild_index(facts_dir)
    # Caller passes uppercase; index is lowercase; lookup still works.
    entries = brain_index.facts_for_subject(idx, "PRIYA-RIVERA")
    assert entries == [["2026-04", "f-001"]]
