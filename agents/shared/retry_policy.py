"""agents/shared/retry_policy.py — pure retry decision logic for Tier 3.

Originally extracted from the Costco reauth flow in
costco-token-daemon.py after a 2026-04-12 incident where the daemon
hammered B2C's /SelfAsserted endpoint on a 429 response and made the
rate-limit worse. Promoted to agents/shared/ because the same logic
applies to any Tier 3 endpoint — sites with real rate limiters,
anti-bot systems, and account-lockout risks.

Side-effect free: no I/O, no sleeps, no network. The daemon imports
classify_selfasserted_response() and should_retry() and decides what
to do; this module only makes the decision.

Decision contract:

    classify_selfasserted_response(status) -> str
        Maps an HTTP status to one of:
          "ok"              200-299; proceed with flow
          "rate_limited"    429; abort retries, wait for cron cycle
          "auth_failed"     400/401/403; credentials rejected
          "transient"       5xx; recoverable, retry with backoff
          "unknown"         anything else

    should_retry(failure_class, attempt, max_attempts) -> (bool, int)
        Returns (retry_yes_no, seconds_to_wait_before_retry).
        Rules:
          - "rate_limited" → NEVER retry. The cron's schedule is the
            cooldown; hammering makes rate-limits worse and risks
            account lockout.
          - "auth_failed"  → never retry. Wrong credentials don't get
            better by trying again, and hammering risks account lockout.
          - "transient"    → retry up to max_attempts with exponential
            backoff starting at 30s (30, 60, 120, ...), capped at 300s.
          - "unknown"      → retry once with 60s backoff, then give up.
          - "ok"           → never retry (we're done).
"""
from __future__ import annotations


def classify_selfasserted_response(status: int) -> str:
    """Map an HTTP status code to a failure class."""
    if 200 <= status < 300:
        return "ok"
    if status == 429:
        return "rate_limited"
    if status in (400, 401, 403):
        return "auth_failed"
    if 500 <= status < 600:
        return "transient"
    return "unknown"


def should_retry(
    failure_class: str, attempt: int, max_attempts: int = 3
) -> tuple[bool, int]:
    """Given a failure class and current attempt number, decide whether
    to retry and how long to wait first.

    attempt is 1-indexed: the first failure is attempt=1.
    """
    if failure_class == "ok":
        return (False, 0)
    if failure_class in ("rate_limited", "auth_failed"):
        return (False, 0)
    if failure_class == "transient":
        if attempt >= max_attempts:
            return (False, 0)
        # Exponential backoff: 30, 60, 120, 240, ... capped at 300.
        return (True, min(30 * (2 ** (attempt - 1)), 300))
    if failure_class == "unknown":
        if attempt >= 2:
            return (False, 0)
        return (True, 60)
    return (False, 0)
