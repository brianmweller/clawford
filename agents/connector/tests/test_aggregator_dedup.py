"""Tests for the token-subset dedup helper in contact-aggregator.py.

The existing fuzzy-alias pass in contact-aggregator.py groups contacts
by (lastname, first-name-with-nicknames). This missed duplicates where
the same person appears under two different first-name spellings that
aren't in NICKNAME_MAP — e.g. the operator's Example Corp direct report "Yendrick
Zieleniak" vs his personal Gmail "Jedrzej Yendrick Zieleniak". Same
person; different first name token; fuzzy pass doesn't catch it.

`is_name_subset(a, b)` returns True when one full name's tokens are a
subset of the other's AND at least two tokens are shared. That's
strict enough to avoid false positives ("John Smith" / "Jane Smith"
share only one token → no merge) and permissive enough to catch the
middle-name pattern that broke the Yendrick case.

Run: cd agents/connector && python3 -m pytest tests/test_aggregator_dedup.py -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

AGGREGATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "mine" / "contact-aggregator.py"
)


@pytest.fixture
def agg():
    spec = importlib.util.spec_from_file_location("agg_dedup", AGGREGATOR_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_subset_with_middle_name(agg):
    assert agg.is_name_subset("Yendrick Zieleniak", "Jedrzej Yendrick Zieleniak")
    assert agg.is_name_subset("Jedrzej Yendrick Zieleniak", "Yendrick Zieleniak")


def test_subset_brian_michael(agg):
    assert agg.is_name_subset("Sam Smith", "the operator Sam M Smith")


def test_not_subset_sibling_names(agg):
    """John Smith and Jane Smith share only the last name — not a
    subset, and not a single-token overlap either. Must not merge."""
    assert not agg.is_name_subset("John Smith", "Jane Smith")


def test_not_subset_different_people_same_last_name(agg):
    assert not agg.is_name_subset("Anna Zieleniak", "Jedrzej Zieleniak")


def test_identical_names_are_subsets(agg):
    assert agg.is_name_subset("Yendrick Zieleniak", "Yendrick Zieleniak")


def test_case_insensitive(agg):
    assert agg.is_name_subset("yendrick zieleniak", "JEDRZEJ YENDRICK ZIELENIAK")


def test_single_word_names_never_subset(agg):
    """A bare single-token name is too weak to be a reliable match."""
    assert not agg.is_name_subset("Zieleniak", "Yendrick Zieleniak")
    assert not agg.is_name_subset("Yendrick", "Yendrick Zieleniak")


def test_handles_suffix_tokens(agg):
    """Jr/III/Sr suffixes should be stripped before comparing."""
    assert agg.is_name_subset("John Doe", "John Doe Jr")
    assert agg.is_name_subset("Robert Smith", "Robert Smith III")


def test_empty_or_missing(agg):
    assert not agg.is_name_subset("", "Sam Smith")
    assert not agg.is_name_subset("Sam Smith", "")
    assert not agg.is_name_subset(None, "Sam Smith")
