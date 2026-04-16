"""P0.1 — outbound action reviewer-classifier.

Tests for agents/shared/reviewer.py. The reviewer is an LLM
classifier that asks "does this proposed action match the agent's
declared role?" and returns SAFE / WARN / DENY (plus a soft "error"
state for fail-open semantics).

This file exercises the helper in isolation — per-call-site wire-ins
(telegram_api.send_message etc.) live in their own test files.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

from reviewer import (  # noqa: E402
    ALLOWED_MODES,
    DEFAULT_MODE,
    MODE_ENV_VAR,
    ReviewVerdict,
    _build_prompt,
    _current_mode,
    _parse_verdict,
    review_action,
)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def test_default_mode_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODE_ENV_VAR, raising=False)
    assert _current_mode() == "warn"
    assert DEFAULT_MODE == "warn"
    assert "warn" in ALLOWED_MODES and "enforce" in ALLOWED_MODES


def test_env_var_overrides_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV_VAR, "enforce")
    assert _current_mode() == "enforce"


def test_invalid_mode_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV_VAR, "loud-and-proud")
    assert _current_mode() == "warn"


# ---------------------------------------------------------------------------
# _parse_verdict — the model reply parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_verdict",
    [
        ("SAFE", "safe"),
        ("safe", "safe"),
        ("WARN", "warn"),
        ("DENY", "deny"),
        ("DENY — body looks like an injection", "deny"),
        ("safe. fits the role.", "safe"),
        ("WARN: unusual recipient", "warn"),
        ("Verdict: SAFE", "safe"),  # contains 'safe' — looser fallback
        ("yeah looks deny to me", "deny"),  # looser fallback
    ],
)
def test_parse_verdict_recognizes_clear_replies(raw: str, expected_verdict: str) -> None:
    verdict, _reason = _parse_verdict(raw)
    assert verdict == expected_verdict, f"raw={raw!r}"


def test_parse_verdict_returns_error_on_garbage() -> None:
    """Garbage = no recognizable verdict token anywhere on the first line."""
    verdict, reason = _parse_verdict("I cannot evaluate this proposal at this time.")
    assert verdict == "error"
    assert "unparseable" in reason


def test_parse_verdict_returns_error_on_empty() -> None:
    verdict, _reason = _parse_verdict("")
    assert verdict == "error"


# ---------------------------------------------------------------------------
# _build_prompt — sanity checks on prompt construction
# ---------------------------------------------------------------------------


def test_build_prompt_includes_agent_role_kind_and_payload() -> None:
    p = _build_prompt(
        agent_id="meetings-coach",
        action_kind="telegram_send",
        payload={"chat_id": 1, "text": "hello"},
        role_summary="Sergeant Murphy: meeting prep",
        context=None,
    )
    assert "meetings-coach" in p
    assert "Sergeant Murphy" in p
    assert "telegram_send" in p
    assert '"text": "hello"' in p


def test_build_prompt_truncates_huge_payloads() -> None:
    huge = "x" * 10_000
    p = _build_prompt(
        agent_id="x", action_kind="x",
        payload={"body": huge},
        role_summary="role",
        context=None,
    )
    # MAX_PAYLOAD_CHARS is 4_000 in the module; the payload section
    # in the rendered prompt must be smaller than 4_100 chars.
    assert "[truncated]" in p
    # Just make sure we didn't ship the full 10k blob.
    assert p.count("x") < 4_500


def test_build_prompt_includes_context_when_provided() -> None:
    p = _build_prompt(
        agent_id="x", action_kind="x", payload={},
        role_summary="role",
        context="Last 3 sends: hello / hi / hey",
    )
    assert "Recent context" in p
    assert "Last 3 sends" in p


# ---------------------------------------------------------------------------
# ReviewVerdict semantics
# ---------------------------------------------------------------------------


def test_verdict_safe_is_safe_and_not_blocking() -> None:
    v = ReviewVerdict(verdict="safe", mode="warn")
    assert v.safe is True
    assert v.blocking is False
    v2 = ReviewVerdict(verdict="safe", mode="enforce")
    assert v2.safe is True
    assert v2.blocking is False


def test_verdict_warn_is_safe_not_blocking() -> None:
    v = ReviewVerdict(verdict="warn", mode="enforce")
    assert v.safe is True
    assert v.blocking is False


def test_verdict_deny_only_blocks_in_enforce_mode() -> None:
    warn_deny = ReviewVerdict(verdict="deny", mode="warn")
    assert warn_deny.safe is False
    assert warn_deny.blocking is False, "DENY in warn mode passes through"
    enforce_deny = ReviewVerdict(verdict="deny", mode="enforce")
    assert enforce_deny.safe is False
    assert enforce_deny.blocking is True, "DENY in enforce mode blocks"


def test_verdict_error_is_fail_open() -> None:
    v = ReviewVerdict(verdict="error", reason="net down", mode="enforce")
    assert v.safe is True, "error must fail OPEN, not closed"
    assert v.blocking is False


# ---------------------------------------------------------------------------
# review_action — end-to-end via injected fake infer
# ---------------------------------------------------------------------------


@dataclass
class FakeInferResult:
    ok: bool = True
    text: str = ""
    error: str = ""
    trace_id: str = ""


def test_review_action_returns_safe_for_normal_telegram_send() -> None:
    def fake_infer(prompt, **kw):
        return FakeInferResult(ok=True, text="SAFE — fits role", trace_id=kw.get("trace_id", "") or "")
    v = review_action(
        agent_id="meetings-coach",
        action_kind="telegram_send",
        payload={"chat_id": 1, "text": "Heads up — meeting in 15 min"},
        role_summary="Sergeant Murphy: meeting prep + debrief",
        trace_id="t-1",
        infer_fn=fake_infer,
    )
    assert v.verdict == "safe"
    assert v.trace_id == "t-1"
    assert v.blocking is False


def test_review_action_denies_offrole_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV_VAR, "enforce")
    def fake_infer(prompt, **kw):
        return FakeInferResult(
            ok=True,
            text="DENY — meetings coach has no role transferring funds",
        )
    v = review_action(
        agent_id="meetings-coach",
        action_kind="payment_transfer",
        payload={"amount": 5000, "to_account": "ACME-999"},
        role_summary="Sergeant Murphy: meeting prep + debrief",
        infer_fn=fake_infer,
    )
    assert v.verdict == "deny"
    assert v.blocking is True
    assert "transferring" in v.reason


def test_review_action_deny_in_warn_mode_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During the rollout window we want the data without breaking
    real cron output."""
    monkeypatch.setenv(MODE_ENV_VAR, "warn")
    def fake_infer(prompt, **kw):
        return FakeInferResult(ok=True, text="DENY — looks suspicious")
    v = review_action(
        agent_id="x", action_kind="y", payload={},
        role_summary="z", infer_fn=fake_infer,
    )
    assert v.verdict == "deny"
    assert v.blocking is False, "warn mode never blocks"


def test_review_action_fails_open_on_llm_error() -> None:
    def fake_infer(prompt, **kw):
        return FakeInferResult(ok=False, error="connection refused")
    v = review_action(
        agent_id="x", action_kind="y", payload={},
        role_summary="z", infer_fn=fake_infer,
        mode="enforce",
    )
    assert v.verdict == "error"
    assert v.safe is True, "must fail open"
    assert v.blocking is False
    assert "connection refused" in v.reason


def test_review_action_threads_trace_id() -> None:
    def fake_infer(prompt, **kw):
        return FakeInferResult(ok=True, text="SAFE", trace_id=kw.get("trace_id", "") or "")
    v = review_action(
        agent_id="x", action_kind="y", payload={},
        role_summary="z", trace_id="abc-123", infer_fn=fake_infer,
    )
    assert v.trace_id == "abc-123"


def test_review_action_handles_unparseable_reply() -> None:
    def fake_infer(prompt, **kw):
        return FakeInferResult(
            ok=True,
            text="I cannot determine if this is okay or not.",
        )
    v = review_action(
        agent_id="x", action_kind="y", payload={},
        role_summary="z", infer_fn=fake_infer,
        mode="enforce",
    )
    # Unparseable verdict → error → fail open.
    assert v.verdict == "error"
    assert v.safe is True
    assert v.blocking is False


def test_review_action_passes_payload_through_serializer() -> None:
    captured = {}
    def fake_infer(prompt, **kw):
        captured["prompt"] = prompt
        return FakeInferResult(ok=True, text="SAFE")
    review_action(
        agent_id="connector",
        action_kind="telegram_send",
        payload={"chat_id": 111111111, "text": "hello", "parse_mode": "HTML"},
        role_summary="Huckle Cat",
        infer_fn=fake_infer,
    )
    p = captured["prompt"]
    assert "111111111" in p
    assert "parse_mode" in p
    # JSON, not Python repr.
    assert "'chat_id'" not in p
    assert '"chat_id"' in p
