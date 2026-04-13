"""Tests for engagement-poller.py::extract_engagement.

The poller scans openclaw session transcripts for reaction commands.
When Sam taps an inline keyboard button in Telegram, openclaw
forwards the callback_data (like:N / dislike:N / more:N) as a session
text message. The poller extracts these and writes engagement.jsonl.

Prior to 2026-04-13 the poller only knew about /like and /dislike.
Now we also need /more:N (expand action) so Sam's 📖 "more" taps
feed into update-preferences.py's existing "expand" WEIGHT_RULES
entry (topic ×1.05).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_poller():
    path = SCRIPTS_DIR / "engagement-poller.py"
    spec = importlib.util.spec_from_file_location("engagement_poller", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def poller():
    return _load_poller()


# ── /like (existing support, pin behavior) ──────────────────────────────


def test_slash_like_with_space_number(poller):
    assert poller.extract_engagement("/like 3") == ("thumbs_up", "3")


def test_like_colon_number(poller):
    assert poller.extract_engagement("like:5") == ("thumbs_up", "5")


def test_like_uppercase(poller):
    assert poller.extract_engagement("LIKE:2") == ("thumbs_up", "2")


def test_bare_like_without_number_returns_none(poller):
    assert poller.extract_engagement("/like") is None


def test_like_in_surrounding_text(poller):
    assert poller.extract_engagement("wow I really /like 7 that") == ("thumbs_up", "7")


# ── /dislike (existing support) ─────────────────────────────────────────


def test_slash_dislike_with_number(poller):
    assert poller.extract_engagement("/dislike 4") == ("thumbs_down", "4")


def test_dislike_colon_number(poller):
    assert poller.extract_engagement("dislike:9") == ("thumbs_down", "9")


# ── /more (NEW — expand action for subtopic enrichment) ────────────────


def test_slash_more_with_number(poller):
    """`/more 3` → ("expand", "3"). Matches WEIGHT_RULES["expand"] in
    update-preferences.py which nudges the topic ×1.05 to indicate
    Sam wanted deeper content on that subtopic."""
    assert poller.extract_engagement("/more 3") == ("expand", "3")


def test_more_colon_number(poller):
    """Inline keyboard callback_data = 'more:N' format."""
    assert poller.extract_engagement("more:7") == ("expand", "7")


def test_more_uppercase(poller):
    assert poller.extract_engagement("MORE:11") == ("expand", "11")


def test_bare_more_without_number_returns_none(poller):
    assert poller.extract_engagement("/more") is None


def test_no_engagement_in_plain_text(poller):
    assert poller.extract_engagement("hello how are you") is None


# ── First-match-wins ordering ───────────────────────────────────────────


def test_like_wins_over_dislike_when_both_present(poller):
    """Pathological input — pin that the first match wins so behavior
    is predictable."""
    result = poller.extract_engagement("/like 1 /dislike 2")
    assert result is not None
    assert result[0] == "thumbs_up"


def test_item_number_with_multiple_digits(poller):
    assert poller.extract_engagement("like:42") == ("thumbs_up", "42")
    assert poller.extract_engagement("/dislike 100") == ("thumbs_down", "100")
    assert poller.extract_engagement("more:15") == ("expand", "15")
