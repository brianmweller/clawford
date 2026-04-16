"""P0.1 wire-in helper — tests for reviewer.review_or_exit.

review_or_exit is the one-liner each cron-script mutation site calls
right before the network mutation. It runs the classifier; on a
blocking DENY it prints a degraded envelope on stdout and exits 0;
otherwise it returns the verdict so the caller can keep going.

Used by: gcal-write (calendar mutations), amazon-reorder (cart-add),
amazon-sns-manage (skip / change / cancel / resubscribe), amazon-
sns-skip (browser-driven skip), costco-reorder (cart-add).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import reviewer  # type: ignore


@dataclass
class FakeInferResult:
    ok: bool = True
    text: str = ""
    error: str = ""
    trace_id: str = ""


# ---------------------------------------------------------------------------
# Happy path — SAFE returns, doesn't exit
# ---------------------------------------------------------------------------


def test_safe_verdict_returns_does_not_exit() -> None:
    fake_infer = lambda *a, **kw: FakeInferResult(ok=True, text="SAFE — fits role")
    exits = []
    prints = []
    v = reviewer.review_or_exit(
        agent_id="shopping",
        action_kind="amazon_add_to_cart",
        payload={"asin": "B086PHR52V", "quantity": 1},
        infer_fn=fake_infer,
        print_envelope=prints.append,
        exit_fn=exits.append,
    )
    assert v.verdict == "safe"
    assert exits == [], "SAFE must not exit"
    assert prints == [], "SAFE must not print degraded envelope"


# ---------------------------------------------------------------------------
# Block path — DENY+enforce prints degraded JSON and exits 0
# ---------------------------------------------------------------------------


def test_deny_in_enforce_prints_degraded_envelope_and_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWFORD_REVIEWER_MODE", "enforce")
    fake_infer = lambda *a, **kw: FakeInferResult(
        ok=True, text="DENY — off-role payment via cart",
    )
    exits = []
    prints = []
    reviewer.review_or_exit(
        agent_id="shopping",
        action_kind="amazon_add_to_cart",
        payload={"asin": "B-SUSPICIOUS"},
        infer_fn=fake_infer,
        print_envelope=prints.append,
        exit_fn=exits.append,
    )
    assert exits == [0], "DENY+enforce must exit 0 (script-contract)"
    assert len(prints) == 1
    env = prints[0]
    assert env["status"] == "degraded"
    assert "blocked by outbound reviewer" in env["alert"]
    assert env["review"]["verdict"] == "deny"
    assert env["review"]["agent_id"] == "shopping"
    assert env["review"]["action_kind"] == "amazon_add_to_cart"


def test_deny_in_warn_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAWFORD_REVIEWER_MODE", "warn")
    fake_infer = lambda *a, **kw: FakeInferResult(ok=True, text="DENY — suspicious")
    exits = []
    prints = []
    v = reviewer.review_or_exit(
        agent_id="shopping",
        action_kind="amazon_sns_cancel",
        payload={"subscription_id": "x"},
        infer_fn=fake_infer,
        print_envelope=prints.append,
        exit_fn=exits.append,
    )
    assert v.verdict == "deny"
    assert v.blocking is False
    assert exits == [], "warn mode never blocks"
    assert prints == [], "warn mode doesn't print the degraded envelope"


def test_error_verdict_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWFORD_REVIEWER_MODE", "enforce")
    fake_infer = lambda *a, **kw: FakeInferResult(ok=False, error="net down")
    exits = []
    v = reviewer.review_or_exit(
        agent_id="shopping",
        action_kind="amazon_sns_skip",
        payload={"subscription_id": "x"},
        infer_fn=fake_infer,
        print_envelope=lambda x: None,
        exit_fn=exits.append,
    )
    assert v.verdict == "error"
    assert v.safe is True
    assert exits == [], "error verdict must NOT exit (fail open)"


def test_role_summary_resolved_from_roster_when_blank() -> None:
    """A caller can omit role_summary; the helper looks it up in the
    AGENT_ROLE_SUMMARIES roster from the agent_id."""
    captured = {}
    def capturing_infer(prompt, **kw):
        captured["prompt"] = prompt
        return FakeInferResult(ok=True, text="SAFE")
    reviewer.review_or_exit(
        agent_id="meetings-coach",
        action_kind="telegram_send",
        payload={"x": 1},
        infer_fn=capturing_infer,
        print_envelope=lambda x: None,
        exit_fn=lambda c: None,
    )
    # The roster's Murphy summary must show up in the prompt.
    assert "Sergeant Murphy" in captured["prompt"]


def test_explicit_role_summary_overrides_roster() -> None:
    captured = {}
    def capturing_infer(prompt, **kw):
        captured["prompt"] = prompt
        return FakeInferResult(ok=True, text="SAFE")
    reviewer.review_or_exit(
        agent_id="meetings-coach",
        action_kind="x",
        payload={},
        role_summary="Custom role for this one call",
        infer_fn=capturing_infer,
        print_envelope=lambda x: None,
        exit_fn=lambda c: None,
    )
    assert "Custom role for this one call" in captured["prompt"]
    assert "Sergeant Murphy" not in captured["prompt"]
