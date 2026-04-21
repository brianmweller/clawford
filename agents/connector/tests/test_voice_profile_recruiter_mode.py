"""Tests for the --recruiter-mode addition to voice-profile-build.
Covers the pure Gmail query builder; LLM + network tested by running."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

# Import the query builder we're adding
import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "voice_profile_build", _SCRIPTS_DIR / "voice-profile-build.py"
)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_build_recruiter_domain_query_ors_all_domains():
    """Query should search for sent mail to any domain in the list."""
    q = _MOD.build_recruiter_domain_query(
        domains=["greenhouse-mail.io", "lever.co"],
        window_days=365,
    )
    assert "in:sent" in q
    assert "newer_than:365d" in q
    assert "to:greenhouse-mail.io" in q
    assert "to:lever.co" in q
    assert " OR " in q


def test_build_recruiter_domain_query_single_domain():
    q = _MOD.build_recruiter_domain_query(
        domains=["lever.co"],
        window_days=180,
    )
    assert "newer_than:180d" in q
    assert "to:lever.co" in q
    # No OR with single domain
    assert " OR " not in q


def test_build_recruiter_domain_query_empty_domains_empty_query():
    q = _MOD.build_recruiter_domain_query(domains=[], window_days=365)
    assert q == ""
