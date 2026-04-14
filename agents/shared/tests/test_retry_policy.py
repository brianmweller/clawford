"""Tests for agents/shared/retry_policy.py — pure retry decision logic.

Promoted from agents/shopping/scripts/test_reauth_retry_policy.py.
Originally written to pin the 429 rate-limit invariant discovered in
the 2026-04-12 Costco reauth trace: B2C returns 429 on /SelfAsserted
when the endpoint is hammered, and blindly retrying makes it worse.
The cron's schedule is the natural cooldown — reauth must fail fast on
429 and wait for the next invocation.

Same rules apply to any Tier 3 endpoint with a rate limiter, which is
why this policy lives in agents/shared/ now instead of agent-specific.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

from retry_policy import classify_selfasserted_response, should_retry  # noqa: E402


# ─── classify_selfasserted_response ────────────────────────────────────────


def test_200_classified_ok():
    assert classify_selfasserted_response(200) == "ok"


def test_204_classified_ok():
    assert classify_selfasserted_response(204) == "ok"


def test_429_classified_rate_limited():
    assert classify_selfasserted_response(429) == "rate_limited"


def test_400_classified_auth_failed():
    assert classify_selfasserted_response(400) == "auth_failed"


def test_401_classified_auth_failed():
    assert classify_selfasserted_response(401) == "auth_failed"


def test_403_classified_auth_failed():
    assert classify_selfasserted_response(403) == "auth_failed"


def test_500_classified_transient():
    assert classify_selfasserted_response(500) == "transient"


def test_503_classified_transient():
    assert classify_selfasserted_response(503) == "transient"


def test_404_classified_unknown():
    assert classify_selfasserted_response(404) == "unknown"


# ─── should_retry ──────────────────────────────────────────────────────────


def test_ok_never_retries():
    assert should_retry("ok", attempt=1) == (False, 0)


def test_rate_limited_never_retries():
    """The critical invariant from the 2026-04-12 trace: 429 must NOT
    retry. Hammering /SelfAsserted when B2C has rate-limited us makes
    the problem worse and risks account-level lockout. The scheduled
    cron cycle is the natural cooldown window."""
    assert should_retry("rate_limited", attempt=1) == (False, 0)
    assert should_retry("rate_limited", attempt=2) == (False, 0)
    assert should_retry("rate_limited", attempt=5) == (False, 0)


def test_auth_failed_never_retries():
    """Credential failures never retry — wrong password won't become
    right on the next try, and hammering risks account lockout."""
    assert should_retry("auth_failed", attempt=1) == (False, 0)


def test_transient_retries_with_backoff():
    """5xx errors can retry with exponential backoff: 30, 60, 120, ..."""
    ok, wait = should_retry("transient", attempt=1, max_attempts=3)
    assert ok is True
    assert wait == 30

    ok, wait = should_retry("transient", attempt=2, max_attempts=3)
    assert ok is True
    assert wait == 60

    ok, wait = should_retry("transient", attempt=3, max_attempts=3)
    assert ok is False


def test_transient_backoff_capped():
    """Backoff shouldn't grow unbounded even if max_attempts is high."""
    ok, wait = should_retry("transient", attempt=10, max_attempts=20)
    assert ok is True
    assert wait <= 300  # cap at 5 minutes


def test_unknown_retries_once():
    """Unknown status: one retry, then give up."""
    ok, wait = should_retry("unknown", attempt=1)
    assert ok is True
    assert wait == 60

    ok, wait = should_retry("unknown", attempt=2)
    assert ok is False
