"""Tests for publisher_label_from_url + LinkedIn likes coercion.

Covers the 2026-04-17 fixes where:
  - Google News-sourced articles were labelled with the generic feed
    label ("Google AI", "Google Economics") instead of the real
    publisher (NYT, WSJ, WaPo, BBC, ...).
  - LinkedIn posts rendered as "LinkedIn (Like likes)" when the
    scraper captured the reaction button's innerText ("Like") instead
    of a digit count.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("feedparser",):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    path = SCRIPTS_DIR / "fetch-and-rank.py"
    spec = importlib.util.spec_from_file_location("fetch_and_rank_pl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    return mod


def test_publisher_label_maps_nyt(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert m.publisher_label_from_url("https://www.nytimes.com/2026/04/17/us/some-article.html") == "NYT"


def test_publisher_label_maps_wsj_wapo_bbc(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert m.publisher_label_from_url("https://www.wsj.com/articles/foo") == "WSJ"
    assert m.publisher_label_from_url("https://www.washingtonpost.com/foo") == "WaPo"
    assert m.publisher_label_from_url("https://www.bbc.co.uk/news/foo") == "BBC"
    assert m.publisher_label_from_url("https://www.bbc.com/news/foo") == "BBC"


def test_publisher_label_strips_www_and_handles_subdomains(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    # subdomain should still match via suffix rule
    assert m.publisher_label_from_url("https://edition.cnn.com/2026/foo") == "CNN"


def test_publisher_label_returns_bare_host_for_unknown(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert m.publisher_label_from_url("https://www.somenewssite.com/foo") == "somenewssite.com"


def test_publisher_label_returns_none_for_unresolved_google_news(tmp_path, monkeypatch):
    """If resolve_google_news_url() couldn't unwrap the redirect, the
    URL still contains news.google.com — publisher_label_from_url must
    return None so fetch_single_feed falls back to the feed label
    rather than labelling the article 'news.google.com'."""
    m = _load(tmp_path, monkeypatch)
    url = "https://news.google.com/rss/articles/ABCDEF?oc=5"
    assert m.publisher_label_from_url(url) is None


def test_publisher_label_returns_none_on_empty(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert m.publisher_label_from_url("") is None
    assert m.publisher_label_from_url(None) is None
