"""Tests for profile_synthesize_lib — prompt construction for the
narrative profile.md synthesis (Stage 1.2).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from profile_synthesize_lib import (  # type: ignore
    build_profile_prompt,
    format_facts_block,
)


_FACTS = {
    "employer": [
        {"company": "LinkedIn", "title": "Director, Flagship Data / FLEX DS",
         "start": "2022-11-01", "end": "2023-12-31"},
        {"company": "Example Corp", "title": "Marketplace Data Science",
         "start": "2024-05-01", "end": "2026-01-31"},
    ],
    "target_company": [
        {"company": "Anthropic", "search_round": "search-post-airbnb",
         "tier_company": "A", "tier_opportunity": "A"},
    ],
    "strength_theme": [
        {"theme": "Marketplace systems thinking",
         "across_roles": ["airbnb", "linkedin"]},
    ],
    "major_accomplishment": [
        {"role": "airbnb", "summary": "Shipped Base Price Redesign",
         "metric": "51% reduction vs 5% target"},
    ],
    "tenet_authored": [
        {"name": "FLEX DS Tenets", "role": "linkedin",
         "doc_ref": "FLEX DS Tenets.docx"},
    ],
}

_ARCHIVES = {
    "airbnb.md": "# AIRBNB\n\nBrian led Marketplace Data Science.",
    "linkedin.md": "# LINKEDIN\n\nBrian led FLEX DS.",
}

_PRIORITY_DOCS = [
    ("E:/.../Notes.docx", "Amazon Notes content: meeting insights from WMS tenure..."),
    ("E:/.../Consolidated Prep.docx", "Leadership philosophy: hire excellent people..."),
]


# --- format_facts_block ---

def test_format_facts_block_includes_all_types():
    block = format_facts_block(_FACTS)
    assert "employer" in block
    assert "target_company" in block
    assert "strength_theme" in block
    assert "major_accomplishment" in block
    assert "tenet_authored" in block


def test_format_facts_block_preserves_specific_values():
    block = format_facts_block(_FACTS)
    assert "Director, Flagship Data / FLEX DS" in block
    assert "Anthropic" in block
    assert "51% reduction vs 5% target" in block
    assert "FLEX DS Tenets.docx" in block


def test_format_facts_block_empty_facts():
    block = format_facts_block({})
    assert isinstance(block, str)


# --- build_profile_prompt ---

def test_build_profile_prompt_includes_all_inputs():
    prompt = build_profile_prompt(_FACTS, _ARCHIVES, _PRIORITY_DOCS)
    # Facts
    assert "Anthropic" in prompt
    assert "FLEX DS Tenets" in prompt
    # Archives
    assert "AIRBNB" in prompt
    assert "Marketplace Data Science" in prompt
    # Priority docs
    assert "meeting insights" in prompt.lower() or "meeting insights" in prompt
    assert "hire excellent people" in prompt


def test_build_profile_prompt_requests_required_sections():
    """Prompt must request the profile sections."""
    prompt = build_profile_prompt(_FACTS, _ARCHIVES, _PRIORITY_DOCS)
    for heading in ["Self-description", "Track record", "Excited by",
                    "Great at", "Leadership", "Level & scope bar", "Target-role"]:
        assert heading in prompt


def test_build_profile_prompt_level_bar_mentions_both_criteria():
    """The Level & scope bar section must encode the operator's two
    conditions: own a major lever OR work directly with C-suite."""
    prompt = build_profile_prompt(_FACTS, _ARCHIVES, _PRIORITY_DOCS)
    text = prompt.lower()
    assert "major lever" in text or "own" in text
    assert "c-suite" in text
    assert "pricing" in text or "supply" in text   # examples of levers


def test_build_profile_prompt_mentions_downstream_consumers():
    """Prompt should tell the LLM what this is FOR so voice is calibrated."""
    prompt = build_profile_prompt(_FACTS, _ARCHIVES, _PRIORITY_DOCS)
    # Hook into Huckle / Murphy framing somewhere in the prompt
    assert "Huckle" in prompt or "recruiter" in prompt.lower()


def test_build_profile_prompt_includes_confidentiality_guard():
    prompt = build_profile_prompt(_FACTS, _ARCHIVES, _PRIORITY_DOCS)
    assert "confidential" in prompt.lower() or "proprietary" in prompt.lower()


def test_build_profile_prompt_handles_empty_priority_docs():
    prompt = build_profile_prompt(_FACTS, _ARCHIVES, [])
    # Still usable — just no priority-doc raw content
    assert "AIRBNB" in prompt


def test_build_profile_prompt_instructs_evidence_citing():
    """Each claim should be grounded — tell the LLM to cite source types."""
    prompt = build_profile_prompt(_FACTS, _ARCHIVES, _PRIORITY_DOCS)
    # Some form of "cite evidence" or "grounded in"
    text = prompt.lower()
    assert "evidence" in text or "grounded" in text or "cite" in text
