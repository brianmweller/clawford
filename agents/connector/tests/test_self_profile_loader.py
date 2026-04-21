"""Tests for agents.shared.self_profile — loads the operator's professional
brain (profile.md + structured facts) for consumption by Huckle
(recruiter drafting) and Murphy (recruiter meeting prep).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))

from agents.shared.self_profile import (  # type: ignore
    extract_level_bar_section,
    load_self_profile,
)


def _write_profile(self_dir: Path, body: str) -> None:
    self_dir.mkdir(parents=True, exist_ok=True)
    (self_dir / "profile.md").write_text(body, encoding="utf-8")


def _write_facts(self_dir: Path, fact_type: str, records: list[dict]) -> None:
    facts_dir = self_dir / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    payload = {"type": fact_type, "records": records, "count": len(records)}
    (facts_dir / f"{fact_type}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )


# --- load_self_profile ---

def test_load_self_profile_reads_profile_md_and_facts(tmp_path: Path):
    _write_profile(tmp_path, "# the operator\n\n## Self-description\nHi\n\n## Level & scope bar\nMust own a lever.\n")
    _write_facts(tmp_path, "target_company", [
        {"company": "Anthropic", "tier_company": "A", "tier_opportunity": "A",
         "search_round": "search-post-airbnb"},
    ])
    _write_facts(tmp_path, "strength_theme", [
        {"theme": "Marketplace systems thinking", "confidence": 0.9},
    ])
    _write_facts(tmp_path, "employer", [
        {"company": "Example Corp", "title": "Director", "start": "2024-05-01", "end": "2026-01-31"},
    ])
    profile = load_self_profile(self_dir=tmp_path)
    assert profile.profile_md is not None
    assert "Self-description" in profile.profile_md
    assert len(profile.target_companies) == 1
    assert profile.target_companies[0]["company"] == "Anthropic"
    assert len(profile.strength_themes) == 1
    assert len(profile.employer_history) == 1
    assert profile.level_bar_text  # extracted from profile.md


def test_load_self_profile_missing_profile_md_returns_none_body(tmp_path: Path):
    """Graceful degradation: if profile.md doesn't exist, loader returns
    empty fields rather than raising. Callers decide whether to proceed."""
    profile = load_self_profile(self_dir=tmp_path)
    assert profile.profile_md is None
    assert profile.target_companies == []


def test_load_self_profile_missing_facts_returns_empty_lists(tmp_path: Path):
    _write_profile(tmp_path, "# the operator\n")
    profile = load_self_profile(self_dir=tmp_path)
    assert profile.profile_md is not None
    assert profile.target_companies == []
    assert profile.strength_themes == []
    assert profile.employer_history == []


def test_load_self_profile_filters_targets_to_open_outcomes(tmp_path: Path):
    """For Huckle's fit-eval, we want the CURRENT target set — not
    historical landed roles. load_self_profile exposes a
    current_targets property that filters by outcome."""
    _write_profile(tmp_path, "# the operator\n")
    _write_facts(tmp_path, "target_company", [
        {"company": "Anthropic", "tier_company": "A", "tier_opportunity": "A",
         "outcome": "targeted", "search_round": "search-post-airbnb"},
        {"company": "Example Corp", "tier_company": "A", "tier_opportunity": "A",
         "outcome": "landed", "search_round": "search-post-linkedin"},
        {"company": "Wayfair", "tier_company": "B", "tier_opportunity": "A",
         "outcome": "in-progress", "search_round": "search-post-airbnb"},
    ])
    profile = load_self_profile(self_dir=tmp_path)
    assert len(profile.target_companies) == 3  # raw list has all
    current = profile.current_targets
    assert len(current) == 2   # landed excluded
    companies = {r["company"] for r in current}
    assert "Example Corp" not in companies
    assert "Anthropic" in companies
    assert "Wayfair" in companies


# --- extract_level_bar_section ---

def test_extract_level_bar_section_finds_section():
    md = """# the operator

## Self-description
I'm a marketplace AI leader.

## Level & scope bar
I need roles that own a major lever OR have C-suite adjacency.

- Own a major lever
- C-suite adjacency

## Target-role shape
...
"""
    text = extract_level_bar_section(md)
    assert "own a major lever" in text.lower()
    assert "c-suite" in text.lower()
    # Should NOT include the next section's content
    assert "Target-role shape" not in text


def test_extract_level_bar_section_missing_returns_empty():
    md = "# the operator\n\n## Self-description\nhello\n"
    assert extract_level_bar_section(md) == ""


def test_extract_level_bar_section_handles_none():
    assert extract_level_bar_section(None) == ""
    assert extract_level_bar_section("") == ""
