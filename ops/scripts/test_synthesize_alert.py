"""Tests for ops/scripts/synthesize_alert.py.

The synthesizer chooses the Telegram alert text for a contract
envelope, with three precedence rules:

  1. Explicit `alert` field (SCRIPT_CONTRACT v2).
  2. Legacy `message` field (v1, kept for back-compat).
  3. Synthesized auth-failure alert when neither is present but
     stderr_tail / error contains an OAuth refresh-failure pattern.

Rule 3 closes the silent-fail gap that bit Huckle Cat: connector
scripts (inbox-triage, gmail-facts-mine, …) returned status=error
with the RefreshError trace tucked inside `wrapped.stderr_tail` and
no `alert`, so the wrapper logged-but-didn't-page for 16 hours.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "synthesize_alert.py"


def _run(envelope: dict, logname: str = "") -> str:
    """Invoke the synthesizer as a subprocess; return stdout (alert text)."""
    line = json.dumps(envelope, ensure_ascii=False)
    res = subprocess.run(
        [sys.executable, str(SCRIPT), line, logname],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return res.stdout.rstrip("\n")


# ─── Explicit fields take precedence ─────────────────────────────────


def test_explicit_alert_wins_over_synthesis():
    """An explicit `alert` field is returned verbatim, even if stderr_tail
    would otherwise trigger synthesis."""
    out = _run({
        "status": "error",
        "alert": "📦 explicit alert text",
        "wrapped": {"stderr_tail": "RefreshError: invalid_grant"},
    }, "shopping-heartbeat")
    assert out == "📦 explicit alert text"


def test_legacy_message_field_used_when_alert_absent():
    """Pre-v2 scripts use `message` instead of `alert`; honor it."""
    out = _run({
        "status": "error",
        "message": "legacy message body",
    }, "any-cron")
    assert out == "legacy message body"


def test_alert_beats_message_when_both_present():
    out = _run({
        "status": "error",
        "alert": "v2 alert",
        "message": "v1 message",
    })
    assert out == "v2 alert"


# ─── Status=ok produces no alert ─────────────────────────────────────


def test_status_ok_returns_empty_even_with_other_fields():
    """Successful runs are silent. The wrapper only relays errors."""
    out = _run({
        "status": "ok",
        "alert": "would-have-paged",  # ignored on ok
        "wrapped": {"stderr_tail": "RefreshError: invalid_grant"},
    }, "noisy-cron")
    assert out == ""


# ─── Auth-failure synthesis (the silent-fail fix) ────────────────────


@pytest.mark.parametrize("stderr_tail", [
    "google.auth.exceptions.RefreshError: ('invalid_grant: ...",
    "raise exceptions.RefreshError(",
    "invalid_grant: Token has been expired or revoked.",
    "Token has been expired or revoked",
    "invalid_credentials",
])
def test_synthesizes_alert_for_auth_failure_in_stderr_tail(stderr_tail):
    """Any of the canonical OAuth failure patterns in stderr_tail
    should produce a synthesized alert with the logname for context."""
    out = _run({
        "status": "error",
        "error": "target exited 1",
        "wrapped": {"exit_code": 1, "stderr_tail": stderr_tail},
    }, "connector-inbox-triage")
    assert "connector-inbox-triage" in out
    assert "OAuth" in out or "auth" in out.lower()
    assert "re-auth" in out.lower()


def test_synthesizes_alert_when_pattern_in_top_level_error():
    """RefreshError can also surface as the envelope's top-level error
    if a script catches and re-raises into its own envelope."""
    out = _run({
        "status": "error",
        "error": "RefreshError: ('invalid_grant: Token has been expired or revoked.',)",
    }, "connector-gmail-watch-renew")
    assert "connector-gmail-watch-renew" in out
    assert "re-auth" in out.lower()


def test_no_alert_for_non_auth_error_without_explicit_field():
    """Generic errors without `alert` / `message` stay silent — the
    contract is opt-in for non-auth errors. We don't want to spam
    Telegram on every transient script failure."""
    out = _run({
        "status": "error",
        "error": "target exited 1",
        "wrapped": {"exit_code": 1, "stderr_tail": "ValueError: bad input"},
    }, "shopping-heartbeat")
    assert out == ""


def test_case_insensitive_match():
    """Patterns match regardless of case."""
    out = _run({
        "status": "error",
        "wrapped": {"stderr_tail": "REFRESHERROR raised in production"},
    }, "x")
    assert "x" in out


def test_no_alert_when_logname_empty_but_pattern_match_still_synthesizes():
    """Even without a logname, an auth failure produces some alert text
    (so the wrapper relays *something* useful)."""
    out = _run({
        "status": "error",
        "wrapped": {"stderr_tail": "invalid_grant"},
    }, "")
    assert out != ""
    assert "auth" in out.lower() or "OAuth" in out


# ─── Malformed input ─────────────────────────────────────────────────


def test_bad_json_returns_empty():
    """Non-JSON input must not crash the bash wrapper. The synthesizer
    is invoked from `script-contract-host.sh` on whatever the script
    happens to print as its last stdout line."""
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "not-json-at-all", "anything"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == ""


def test_empty_envelope_returns_empty():
    out = _run({}, "x")
    assert out == ""
