"""Tests for recruiter_detector_lib — classifies an inbound email as
a likely recruiter outreach based on sender domain + subject/snippet
patterns. Used by Huckle's triage to route cold recruiter inbounds
into drafting instead of silently skipping as 'unknown_sender'.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from recruiter_detector_lib import (  # type: ignore
    RECRUITER_DOMAINS,
    is_likely_recruiter,
)


# --- Strong signal: ATS platform domains ---

def test_greenhouse_mail_is_recruiter():
    ok, conf, signals = is_likely_recruiter(
        from_email="no-reply@greenhouse-mail.io",
        from_header='"Jane @ Stripe" <no-reply@greenhouse-mail.io>',
        subject="Interested in your background",
        snippet="Hi the operator, I'm reaching out about a Director role at Stripe.",
    )
    assert ok
    assert conf >= 0.9
    assert "domain" in signals["reason"].lower() or "ats" in signals["reason"].lower()


def test_lever_domain_is_recruiter():
    ok, conf, _ = is_likely_recruiter(
        from_email="jane.recruiter@hire.lever.co",
        from_header="",
        subject="Opportunity at company",
        snippet="",
    )
    assert ok
    assert conf >= 0.9


def test_ashby_domain_is_recruiter():
    ok, _, _ = is_likely_recruiter(
        from_email="noreply@ashbyhq.com",
        from_header="",
        subject="",
        snippet="",
    )
    assert ok


def test_linkedin_inmail_is_recruiter_when_subject_implies_outreach():
    """LinkedIn messages-noreply domains alone are ambiguous, but when
    subject/snippet looks like executive outreach, treat as recruiter."""
    ok, conf, signals = is_likely_recruiter(
        from_email="messages-noreply@linkedin.com",
        from_header='"Jane Recruiter" <messages-noreply@linkedin.com>',
        subject="Opportunity — Director of Data Science, Marketplace",
        snippet="Hi the operator, we're looking for a leader to join our team...",
    )
    assert ok
    assert conf > 0.5


def test_retained_search_domain_is_recruiter():
    ok, conf, _ = is_likely_recruiter(
        from_email="partner@rivierapartners.com",
        from_header="",
        subject="",
        snippet="",
    )
    assert ok
    assert conf >= 0.7


# --- Weak signal: subject/snippet keywords, unknown domain ---

def test_subject_with_opportunity_keyword_plus_snippet_fit():
    """Subject keyword alone is weaker. Combined with fit-style snippet,
    treat as recruiter (with moderate confidence)."""
    ok, conf, _ = is_likely_recruiter(
        from_email="jane@somerecruiter.com",
        from_header='"Jane Smith" <jane@somerecruiter.com>',
        subject="Reaching out regarding a senior opportunity",
        snippet="I came across your background and wanted to chat about a Director-level role.",
    )
    assert ok
    assert 0.5 <= conf <= 0.85


def test_bare_subject_opportunity_not_enough_alone():
    """A vague 'opportunity' subject with no corroborating signals is
    NOT sufficient — could be anything (sales, internal, family)."""
    ok, _, _ = is_likely_recruiter(
        from_email="random@somedomain.com",
        from_header="",
        subject="New opportunity",
        snippet="",
    )
    assert not ok


# --- Negative cases ---

def test_friend_personal_email_not_recruiter():
    ok, _, _ = is_likely_recruiter(
        from_email="mom@example.com",
        from_header='"Mom" <mom@example.com>',
        subject="dinner Sunday?",
        snippet="Hi honey, want to come over this weekend?",
    )
    assert not ok


def test_service_notification_not_recruiter():
    """Automated service notifications shouldn't trip the recruiter
    detector — they're handled by is_likely_service_account upstream."""
    ok, _, _ = is_likely_recruiter(
        from_email="notifications@stripe.com",
        from_header="",
        subject="Invoice sent",
        snippet="",
    )
    assert not ok


def test_generic_sales_outreach_not_recruiter():
    """'Interested in your company' sales pitch ≠ recruiter."""
    ok, _, _ = is_likely_recruiter(
        from_email="sales@vendor.com",
        from_header="",
        subject="Interested in partnering with Clawford",
        snippet="We'd love to demo our platform to your engineering team.",
    )
    assert not ok


def test_empty_inputs_not_recruiter():
    ok, _, _ = is_likely_recruiter(
        from_email="",
        from_header="",
        subject="",
        snippet="",
    )
    assert not ok


def test_none_inputs_tolerated():
    """Defensive: shouldn't crash on None."""
    ok, _, _ = is_likely_recruiter(
        from_email=None,
        from_header=None,
        subject=None,
        snippet=None,
    )
    assert not ok


# --- Signals detail ---

def test_signals_dict_contains_diagnostic_fields():
    """The returned signals dict should tell downstream what tripped
    the detection — useful for Telegram FYI + debugging."""
    _, _, signals = is_likely_recruiter(
        from_email="recruiter@greenhouse-mail.io",
        from_header="",
        subject="Opportunity at Stripe",
        snippet="Senior leadership role",
    )
    assert "reason" in signals
    assert isinstance(signals["reason"], str)
    assert "matched_domain" in signals or "matched_keywords" in signals


def test_recruiter_domains_contains_common_ats():
    """Sanity: make sure common ATS/recruiting domains are in the set."""
    for d in ("greenhouse-mail.io", "lever.co", "ashbyhq.com"):
        assert any(d in rd for rd in RECRUITER_DOMAINS)
