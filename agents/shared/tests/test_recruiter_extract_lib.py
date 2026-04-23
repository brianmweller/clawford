"""Tests for agents/shared/recruiter_extract_lib.py.

The extractor turns an inbound recruiter email into a structured
`ExtractedPitch` (company_name, role_title, company_domain) that feeds
company_research.research_company. Fleet-shared so Huckle's cold-inbound
compose path AND Murphy's recruiter-meeting resolver both see the same
extraction logic.

Tests never call the network: _llm_infer is monkeypatched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.shared import recruiter_extract_lib
from agents.shared.recruiter_extract_lib import (
    ExtractedPitch,
    _domain_from_email,
    _infer_company_from_email,
    extract_company_and_role,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_domain_from_email():
    assert _domain_from_email("abby@coinbase.com") == "coinbase.com"
    assert _domain_from_email("Abby Mintert <abby@coinbase.com>") == "coinbase.com"
    assert _domain_from_email("") == ""
    assert _domain_from_email("no-at-sign") == ""


def test_infer_company_from_in_house_email():
    """Direct company domains should yield a company name without
    needing the LLM."""
    # Plain company domain → capitalize the stem.
    assert _infer_company_from_email("abby@anthropic.com") == "Anthropic"
    assert _infer_company_from_email("michelle@reddit.com") == "Reddit"


def test_infer_company_from_ats_subdomain():
    """Reuses extract_company_from_ats_domain for in-house ATS prefixes."""
    assert _infer_company_from_email("schedule@interview.adobe.com") == "Adobe"


def test_infer_company_from_third_party_ats_returns_none():
    """Lever / Greenhouse domains encode no company signal — the LLM
    must do the work from the body."""
    assert _infer_company_from_email("jane@lever.co") is None
    assert _infer_company_from_email("no-reply@greenhouse-mail.io") is None


def test_infer_company_from_linkedin_relay_returns_none():
    """LinkedIn InMail / relay addresses (hit-reply@linkedin.com,
    anything @linkedin.com) carry zero signal about the hiring company —
    the real sender is masked behind LinkedIn's routing. Regression:
    2026-04-23 Coinbase prep researched LinkedIn because the Gmail
    thread's From was 'Abby Mintert via LinkedIn <hit-reply@linkedin.com>'.
    Must fall through to body-based extraction (LLM)."""
    assert _infer_company_from_email("hit-reply@linkedin.com") is None
    assert _infer_company_from_email("Abby Mintert <abby@linkedin.com>") is None
    assert _infer_company_from_email("noreply@inmail.linkedin.com") is None


# ---------------------------------------------------------------------------
# extract_company_and_role — end to end
# ---------------------------------------------------------------------------


def _make_infer_result(text: str = "", *, ok: bool = True, error: str | None = None):
    from agents.shared.llm import InferResult
    if ok:
        return InferResult(text=text, returncode=0, error=None)
    return InferResult(text="", returncode=500, error=error or "canned")


_CANNED_LLM = {
    "company_name": "Anthropic",
    "role_title": "Director, Trust & Safety Engineering",
    "confidence": "high",
}


def test_extract_from_in_house_email_uses_domain_and_llm(monkeypatch, tmp_path):
    captured_prompts: list[str] = []

    def _fake_llm(*, prompt, **_):
        captured_prompts.append(prompt)
        return _make_infer_result(json.dumps(_CANNED_LLM))

    monkeypatch.setattr(recruiter_extract_lib, "_llm_infer", _fake_llm)

    inbound = {
        "from_name": "Abby Mintert",
        "from_email": "abby@anthropic.com",
        "subject": "Director role at Anthropic - interested?",
        "body": (
            "Hi the operator,\n\n"
            "I'd love to chat about a Director-level opening on our Trust "
            "& Safety team. Would next Tuesday work?\n\n"
            "Best,\nAbby"
        ),
    }
    pitch = extract_company_and_role(inbound, cache_dir=tmp_path)
    assert pitch.ok
    assert pitch.company_name == "Anthropic"
    assert "Director" in pitch.role_title
    assert pitch.company_domain == "anthropic.com"
    # The LLM still got called — it's the source of truth for role_title
    # and it gets a shot at correcting the company name if body signal
    # diverges from the domain stem.
    assert captured_prompts


def test_extract_from_ats_platform_lets_llm_find_company(monkeypatch, tmp_path):
    """Email from lever.co carries no direct company signal; the LLM
    reads the body."""
    def _fake_llm(*, prompt, **_):
        return _make_infer_result(json.dumps(_CANNED_LLM))

    monkeypatch.setattr(recruiter_extract_lib, "_llm_infer", _fake_llm)

    inbound = {
        "from_name": "Jane",
        "from_email": "jane@lever.co",
        "subject": "Anthropic — Director opening",
        "body": "Hi the operator, I'm reaching out on behalf of Anthropic...",
    }
    pitch = extract_company_and_role(inbound, cache_dir=tmp_path)
    assert pitch.ok
    assert pitch.company_name == "Anthropic"
    # Domain is still recorded mechanically for downstream reference.
    assert pitch.company_domain == "lever.co"


def test_cache_hit_skips_llm(monkeypatch, tmp_path):
    """Same inbound content → second call must not re-invoke the LLM."""
    call_count = {"n": 0}

    def _fake_llm(*, prompt, **_):
        call_count["n"] += 1
        return _make_infer_result(json.dumps(_CANNED_LLM))

    monkeypatch.setattr(recruiter_extract_lib, "_llm_infer", _fake_llm)

    inbound = {
        "from_name": "Abby",
        "from_email": "abby@anthropic.com",
        "subject": "Role at Anthropic",
        "body": "Director-level opening on Trust & Safety.",
    }
    first = extract_company_and_role(inbound, cache_dir=tmp_path)
    second = extract_company_and_role(inbound, cache_dir=tmp_path)
    assert first.company_name == second.company_name
    assert call_count["n"] == 1


def test_force_bypasses_cache(monkeypatch, tmp_path):
    call_count = {"n": 0}

    def _fake_llm(*, prompt, **_):
        call_count["n"] += 1
        return _make_infer_result(json.dumps(_CANNED_LLM))

    monkeypatch.setattr(recruiter_extract_lib, "_llm_infer", _fake_llm)

    inbound = {
        "from_name": "Abby",
        "from_email": "abby@anthropic.com",
        "subject": "Role",
        "body": "Director-level opening.",
    }
    extract_company_and_role(inbound, cache_dir=tmp_path)
    extract_company_and_role(inbound, cache_dir=tmp_path, force=True)
    assert call_count["n"] == 2


def test_llm_failure_returns_error_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recruiter_extract_lib, "_llm_infer",
        lambda **_: _make_infer_result(ok=False, error="simulated"),
    )
    inbound = {
        "from_name": "Abby",
        "from_email": "abby@anthropic.com",
        "subject": "Role",
        "body": "Body.",
    }
    pitch = extract_company_and_role(inbound, cache_dir=tmp_path)
    assert not pitch.ok
    assert pitch.error
    # No cache file was written, so the next call retries.
    assert not list(tmp_path.glob("*.json"))


def test_invalid_json_returns_error_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recruiter_extract_lib, "_llm_infer",
        lambda **_: _make_infer_result("not json"),
    )
    inbound = {
        "from_name": "Abby",
        "from_email": "abby@anthropic.com",
        "subject": "Role",
        "body": "Body.",
    }
    pitch = extract_company_and_role(inbound, cache_dir=tmp_path)
    assert not pitch.ok
    assert pitch.error
    assert not list(tmp_path.glob("*.json"))


def test_empty_body_still_attempts_extraction(monkeypatch, tmp_path):
    """No body? LLM still gets called with whatever we have. If it
    returns nothing useful, confidence should be low but no crash."""
    def _fake_llm(*, prompt, **_):
        return _make_infer_result(json.dumps(
            {"company_name": "", "role_title": "", "confidence": "low"}
        ))

    monkeypatch.setattr(recruiter_extract_lib, "_llm_infer", _fake_llm)
    inbound = {
        "from_name": "",
        "from_email": "contact@reddit.com",
        "subject": "",
        "body": "",
    }
    pitch = extract_company_and_role(inbound, cache_dir=tmp_path)
    # When LLM returns nothing but the domain gave us a company, use it.
    assert pitch.company_name == "Reddit"
    assert pitch.company_domain == "reddit.com"


def test_to_prompt_dict_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recruiter_extract_lib, "_llm_infer",
        lambda **_: _make_infer_result(json.dumps(_CANNED_LLM)),
    )
    inbound = {
        "from_name": "Abby",
        "from_email": "abby@anthropic.com",
        "subject": "Role",
        "body": "Director-level opening.",
    }
    pitch = extract_company_and_role(inbound, cache_dir=tmp_path)
    pd = pitch.to_prompt_dict()
    assert pd["company_name"] == "Anthropic"
    assert pd["role_title"].startswith("Director")
    assert pd["company_domain"] == "anthropic.com"
    assert pd["confidence"] == "high"


def test_errored_pitch_to_prompt_dict_falsy():
    pitch = ExtractedPitch(error="LLM unavailable")
    assert not pitch.ok
    assert not pitch.to_prompt_dict()


def test_llm_receives_from_domain_hint(monkeypatch, tmp_path):
    """When we have a strong in-house domain signal, the LLM prompt
    should carry it so the model can use it as a prior (and override
    only when the body is explicit)."""
    captured: list[str] = []

    def _fake_llm(*, prompt, **_):
        captured.append(prompt)
        return _make_infer_result(json.dumps(_CANNED_LLM))

    monkeypatch.setattr(recruiter_extract_lib, "_llm_infer", _fake_llm)

    inbound = {
        "from_name": "Abby",
        "from_email": "abby@anthropic.com",
        "subject": "Role",
        "body": "Body.",
    }
    extract_company_and_role(inbound, cache_dir=tmp_path)
    assert "anthropic.com" in captured[0].lower()
