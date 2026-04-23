"""Tests for agents/shared/company_research.py.

research_company(company_name) → CompanyBrief. Used by Huckle
(cold-recruiter drafting) and Murphy (meeting prep) to enrich the LLM
context with company-level signal beyond the recruiter email / calendar
invite text. Cached per-slug under ~/.clawford/company-research-cache/
so repeat lookups across agents share the same brief.

Tests never touch the network: _search_brave is monkeypatched, and
llm.infer is monkeypatched to return canned InferResult payloads.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.shared import company_research
from agents.shared.company_research import (
    CompanyBrief,
    _slugify_company,
    research_company,
)


# ---------------------------------------------------------------------------
# Slug normalization
# ---------------------------------------------------------------------------


def test_slugify_company_lowercase_hyphenates():
    assert _slugify_company("Anthropic") == "anthropic"
    assert _slugify_company("OpenAI Inc.") == "openai"
    assert _slugify_company("Reddit, Inc.") == "reddit"


def test_slugify_company_strips_suffixes():
    # Corporate suffixes shouldn't fragment the cache — "Anthropic, PBC"
    # and "Anthropic" must hash to the same slug.
    assert _slugify_company("Anthropic, PBC") == "anthropic"
    assert _slugify_company("Acme Corp.") == "acme"
    assert _slugify_company("Foo Ltd.") == "foo"
    assert _slugify_company("Bar LLC") == "bar"


def test_slugify_company_multiword():
    assert _slugify_company("Hugging Face") == "hugging-face"
    assert _slugify_company("Goldman Sachs Group, Inc.") == "goldman-sachs-group"


def test_slugify_company_empty():
    assert _slugify_company("") == ""
    assert _slugify_company("   ") == ""


# ---------------------------------------------------------------------------
# End-to-end research_company plumbing
# ---------------------------------------------------------------------------


def _make_infer_result(text: str = "", *, ok: bool = True, error: str | None = None):
    """Minimal stand-in for llm.InferResult — the module only consults
    .ok, .text, .error."""
    from agents.shared.llm import InferResult

    if ok:
        return InferResult(text=text, returncode=0, error=None)
    return InferResult(text="", returncode=500, error=error or "canned failure")


_CANNED_SYNTHESIS = {
    "what_they_do": "Ships a frontier LLM and aligned assistant.",
    "stage_signal": "series-C",
    "recent_news": [
        {"bullet": "Raised Series D", "dated": "2026-03"},
    ],
    "tech_or_product_hints": ["Claude", "Agent SDK"],
    "role_context": {
        "title": "Director of ML",
        "team_hint": "safety",
        "level_band_hint": "L7",
        "scope_hint": "cross-functional",
    },
    "operator_fit": {
        "strength_angles": ["trust & safety ops"],
        "concerns": ["late-stage, possibly level-compressed"],
        "questions_to_ask": ["How does safety-team report into research?"],
        "hooks_to_drop": ["your Series D + agent-SDK push"],
    },
    "confidence": "high",
    "sources": [{"url": "https://example.com/a", "title": "Article A"}],
}


def test_cache_hit_bypasses_network(monkeypatch, tmp_path):
    """A fresh cache file means zero Brave calls and zero LLM calls."""

    def _should_not_be_called(*a, **kw):
        raise AssertionError("network call made despite fresh cache")

    monkeypatch.setattr(company_research, "_search_brave", _should_not_be_called)
    monkeypatch.setattr(company_research, "_llm_infer", _should_not_be_called)

    # Seed a fresh cache (researched 1 hour ago).
    slug = "anthropic"
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    cache_path = tmp_path / f"{slug}.json"
    cache_path.write_text(json.dumps({
        "company_name": "Anthropic",
        "company_slug": slug,
        "researched_at_utc": (now - timedelta(hours=1)).isoformat(),
        "what_they_do": "Cached answer.",
        "confidence": "high",
        "sources": [],
    }), encoding="utf-8")

    brief = research_company(
        "Anthropic",
        cache_dir=tmp_path,
        now=now,
    )
    assert brief.ok
    assert brief.what_they_do == "Cached answer."


def test_cache_expired_triggers_refresh(monkeypatch, tmp_path):
    """A stale cache file should trigger a fresh Brave + LLM call."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    slug = "anthropic"
    cache_path = tmp_path / f"{slug}.json"
    stale_ts = now - timedelta(days=60)  # TTL default is 30d
    cache_path.write_text(json.dumps({
        "company_name": "Anthropic",
        "company_slug": slug,
        "researched_at_utc": stale_ts.isoformat(),
        "what_they_do": "Stale answer.",
    }), encoding="utf-8")

    brave_calls: list[str] = []

    def _fake_brave(query, api_key):
        brave_calls.append(query)
        return [{"title": "t", "url": "https://x", "description": "d"}]

    monkeypatch.setattr(company_research, "_search_brave", _fake_brave)
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result(json.dumps(_CANNED_SYNTHESIS)),
    )

    brief = research_company(
        "Anthropic",
        cache_dir=tmp_path,
        now=now,
        brave_api_key="test-key",
    )
    assert brief.ok
    assert brave_calls  # Brave was consulted
    assert brief.what_they_do == _CANNED_SYNTHESIS["what_they_do"]


def test_force_bypasses_fresh_cache(monkeypatch, tmp_path):
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    slug = "anthropic"
    cache_path = tmp_path / f"{slug}.json"
    cache_path.write_text(json.dumps({
        "company_name": "Anthropic",
        "company_slug": slug,
        "researched_at_utc": now.isoformat(),
        "what_they_do": "Old answer.",
    }), encoding="utf-8")

    monkeypatch.setattr(
        company_research, "_search_brave",
        lambda q, k: [{"title": "t", "url": "https://x", "description": "d"}],
    )
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result(json.dumps(_CANNED_SYNTHESIS)),
    )

    brief = research_company(
        "Anthropic",
        cache_dir=tmp_path,
        now=now,
        force=True,
        brave_api_key="test-key",
    )
    assert brief.what_they_do == _CANNED_SYNTHESIS["what_they_do"]


def test_brave_failure_still_synthesizes(monkeypatch, tmp_path):
    """If Brave returns nothing / fails, the LLM still runs with empty
    snippets and produces a low-confidence brief."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(company_research, "_search_brave", lambda q, k: [])

    captured_prompts: list[str] = []

    def _fake_llm(*, prompt, **_):
        captured_prompts.append(prompt)
        payload = dict(_CANNED_SYNTHESIS)
        payload["confidence"] = "low"
        payload["sources"] = []
        return _make_infer_result(json.dumps(payload))

    monkeypatch.setattr(company_research, "_llm_infer", _fake_llm)

    brief = research_company("Anthropic", cache_dir=tmp_path, now=now)
    assert brief.ok
    assert brief.confidence == "low"
    assert brief.sources == []
    assert captured_prompts, "LLM was never called"


def test_llm_failure_returns_error_brief_and_no_cache(monkeypatch, tmp_path):
    """LLM failure must leave no cache file on disk (so the next call
    retries) and must return a non-ok brief."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        company_research, "_search_brave",
        lambda q, k: [{"title": "t", "url": "https://x", "description": "d"}],
    )
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result(ok=False, error="simulated LLM down"),
    )

    brief = research_company(
        "Anthropic",
        cache_dir=tmp_path,
        now=now,
        brave_api_key="test-key",
    )
    assert not brief.ok
    assert brief.error
    # Crucial: no cache file was written — next call must retry.
    assert not (tmp_path / "anthropic.json").exists()


def test_llm_returns_invalid_json_returns_error_no_cache(monkeypatch, tmp_path):
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        company_research, "_search_brave",
        lambda q, k: [{"title": "t", "url": "https://x", "description": "d"}],
    )
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result("this is not json"),
    )

    brief = research_company(
        "Anthropic",
        cache_dir=tmp_path,
        now=now,
        brave_api_key="test-key",
    )
    assert not brief.ok
    assert brief.error
    assert not (tmp_path / "anthropic.json").exists()


def test_successful_run_writes_cache_atomically(monkeypatch, tmp_path):
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        company_research, "_search_brave",
        lambda q, k: [{"title": "t", "url": "https://x", "description": "d"}],
    )
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result(json.dumps(_CANNED_SYNTHESIS)),
    )

    brief = research_company(
        "Anthropic",
        cache_dir=tmp_path,
        now=now,
        brave_api_key="test-key",
    )

    cache_path = tmp_path / "anthropic.json"
    assert cache_path.exists()
    # No temp file left behind.
    leftover = list(tmp_path.glob("*.tmp"))
    assert not leftover, f"temp file(s) leaked: {leftover}"

    # Reading cache back matches the returned brief.
    disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert disk["what_they_do"] == brief.what_they_do
    assert disk["company_slug"] == "anthropic"
    assert disk["researched_at_utc"] == brief.researched_at_utc


def test_operator_context_appears_in_synthesis_prompt(monkeypatch, tmp_path):
    """The synthesis LLM call must see the operator's strength themes
    and current targets so operator_fit is tailored to the operator, not
    generic."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        company_research, "_search_brave",
        lambda q, k: [{"title": "t", "url": "https://x", "description": "d"}],
    )

    captured_prompts: list[str] = []

    def _fake_llm(*, prompt, **_):
        captured_prompts.append(prompt)
        return _make_infer_result(json.dumps(_CANNED_SYNTHESIS))

    monkeypatch.setattr(company_research, "_llm_infer", _fake_llm)

    operator_context = {
        "strength_themes": [
            {"value": "trust & safety ops at scale"},
            {"value": "org rewires under growth inflection"},
        ],
        "current_targets": [
            {"value": "Anthropic"},
            {"value": "OpenAI"},
        ],
        "level_bar_text": "Director+ at 500-2000 eng orgs",
    }
    research_company(
        "Anthropic",
        cache_dir=tmp_path,
        now=now,
        operator_context=operator_context,
        brave_api_key="test-key",
    )

    assert captured_prompts
    prompt = captured_prompts[0]
    assert "trust & safety ops at scale" in prompt
    assert "org rewires under growth inflection" in prompt
    assert "Director+" in prompt


def test_role_title_spawns_jd_query(monkeypatch, tmp_path):
    """When role_title is provided, we should fire a third Brave query
    targeted at the JD, not just the company."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    queries: list[str] = []
    monkeypatch.setattr(
        company_research, "_search_brave",
        lambda q, k: (queries.append(q) or [{"title": "t", "url": "u", "description": "d"}]),
    )
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result(json.dumps(_CANNED_SYNTHESIS)),
    )

    research_company(
        "Anthropic",
        role_title="Director, ML Infrastructure",
        cache_dir=tmp_path,
        now=now,
        brave_api_key="test-key",
    )

    assert len(queries) == 3
    assert any("Director, ML Infrastructure" in q and "Anthropic" in q for q in queries)


def test_no_role_title_means_two_queries(monkeypatch, tmp_path):
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    queries: list[str] = []
    monkeypatch.setattr(
        company_research, "_search_brave",
        lambda q, k: (queries.append(q) or []),
    )
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result(json.dumps(_CANNED_SYNTHESIS)),
    )

    research_company(
        "Anthropic",
        cache_dir=tmp_path,
        now=now,
        brave_api_key="test-key",
    )
    assert len(queries) == 2


def test_missing_brave_api_key_skips_search(monkeypatch, tmp_path):
    """No BRAVE_API_KEY at all → skip search, still synthesize with
    empty snippets. Must not blow up."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    # If _search_brave is ever called with an empty key, that's the bug.
    def _should_not_be_called(query, api_key):
        assert api_key, "Brave should not be called without an API key"
        return []

    monkeypatch.setattr(company_research, "_search_brave", _should_not_be_called)
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result(json.dumps(_CANNED_SYNTHESIS)),
    )
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    brief = research_company("Anthropic", cache_dir=tmp_path, now=now)
    assert brief.ok


def test_brief_to_prompt_dict_shape(monkeypatch, tmp_path):
    """to_prompt_dict() returns a compact dict safe to splice into the
    downstream compose / prep prompts — no bulky sources/raw_search."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(company_research, "_search_brave", lambda q, k: [])
    monkeypatch.setattr(
        company_research, "_llm_infer",
        lambda **_: _make_infer_result(json.dumps(_CANNED_SYNTHESIS)),
    )

    brief = research_company("Anthropic", cache_dir=tmp_path, now=now)
    pd = brief.to_prompt_dict()
    assert pd["company_name"] == "Anthropic"
    assert pd["what_they_do"] == _CANNED_SYNTHESIS["what_they_do"]
    assert pd["stage_signal"] == "series-C"
    assert "operator_fit" in pd
    assert "hooks_to_drop" in pd["operator_fit"]
    # Confidence should round-trip so downstream renderers can gate on it.
    assert pd["confidence"] == "high"


def test_error_brief_to_prompt_dict_returns_none_or_empty():
    """An errored CompanyBrief must degrade safely — callers should be
    able to check .ok or treat to_prompt_dict() as falsy."""
    brief = CompanyBrief(
        company_name="Anthropic",
        company_slug="anthropic",
        error="LLM unavailable",
    )
    assert not brief.ok
    # Implementations may return {} or None; either is acceptable.
    pd = brief.to_prompt_dict()
    assert not pd


def test_same_slug_shared_across_name_variants(monkeypatch, tmp_path):
    """First call with 'Anthropic, PBC' seeds the cache; second call
    with bare 'Anthropic' hits it."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    call_count = {"brave": 0, "llm": 0}

    def _fake_brave(q, k):
        call_count["brave"] += 1
        return [{"title": "t", "url": "u", "description": "d"}]

    def _fake_llm(**_):
        call_count["llm"] += 1
        return _make_infer_result(json.dumps(_CANNED_SYNTHESIS))

    monkeypatch.setattr(company_research, "_search_brave", _fake_brave)
    monkeypatch.setattr(company_research, "_llm_infer", _fake_llm)

    first = research_company(
        "Anthropic, PBC", cache_dir=tmp_path, now=now, brave_api_key="k",
    )
    second = research_company(
        "Anthropic", cache_dir=tmp_path, now=now, brave_api_key="k",
    )
    assert first.company_slug == second.company_slug == "anthropic"
    # Second call must have been a cache hit.
    assert call_count["llm"] == 1
