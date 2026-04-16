"""P0.1 wire-in — red-team tests for telegram_api.send_message review path.

Every Telegram outbound from any agent funnels through send_message;
wiring the reviewer here gives fleet-wide outbound coverage from
one chokepoint.

  - Back-compat: callers without agent_id (legacy) skip the review.
  - With agent_id: the review runs; in enforce mode a DENY blocks
    the HTTP call entirely, in warn mode every verdict is logged
    and the send proceeds.
  - skip_review=True bypasses for mechanical confirm/cancel
    messages (hardcoded strings, not agent-composed payloads).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload_modules():
    for m in ("telegram_api", "reviewer", "llm"):
        if m in sys.modules:
            del sys.modules[m]
    import telegram_api  # type: ignore
    import reviewer  # type: ignore
    return telegram_api, reviewer


@dataclass
class FakeInferResult:
    ok: bool = True
    text: str = ""
    error: str = ""
    trace_id: str = ""


def _fake_telegram_ok():
    """Stub urlopen() that returns a Telegram-compatible {ok: true}."""
    response = MagicMock()
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    response.read = MagicMock(return_value=json.dumps({"ok": True}).encode("utf-8"))
    return MagicMock(return_value=response)


# ---------------------------------------------------------------------------
# Back-compat: no agent_id → no review
# ---------------------------------------------------------------------------


def test_send_without_agent_id_skips_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAWFORD_AGENT_ID", raising=False)
    tg, rv = _reload_modules()
    review_called = {"n": 0}
    def tripwire(*a, **kw):
        review_called["n"] += 1
        return rv.ReviewVerdict(verdict="safe")
    monkeypatch.setattr(rv, "review_action", tripwire)
    monkeypatch.setattr(tg.urllib.request, "urlopen", _fake_telegram_ok())

    ok = tg.send_message("token-x", "111111111", "hello")
    assert ok is True
    assert review_called["n"] == 0, "no agent_id → no review call"


def test_send_with_skip_review_bypasses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "fix-it")
    tg, rv = _reload_modules()
    review_called = {"n": 0}
    monkeypatch.setattr(rv, "review_action",
                        lambda *a, **kw: (review_called.__setitem__("n", review_called["n"] + 1), rv.ReviewVerdict(verdict="safe"))[1])
    monkeypatch.setattr(tg.urllib.request, "urlopen", _fake_telegram_ok())

    ok = tg.send_message(
        "token-x", "111111111", "Cancelled: foo", skip_review=True,
    )
    assert ok is True
    assert review_called["n"] == 0, "skip_review=True must bypass review"


# ---------------------------------------------------------------------------
# Agent context: review runs; SAFE proceeds; DENY blocks in enforce
# ---------------------------------------------------------------------------


def test_send_with_agent_id_runs_review_and_proceeds_on_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "meetings-coach")
    tg, rv = _reload_modules()
    captured = {}
    def fake_review(**kw):
        captured.update(kw)
        return rv.ReviewVerdict(
            verdict="safe", mode="enforce",
            agent_id=kw.get("agent_id", ""),
            action_kind=kw.get("action_kind", ""),
        )
    monkeypatch.setattr(rv, "review_action", fake_review)
    monkeypatch.setattr(tg.urllib.request, "urlopen", _fake_telegram_ok())

    ok = tg.send_message(
        "token-x", "111111111", "Heads up — meeting in 15 min",
    )
    assert ok is True
    # Reviewer was given the right context.
    assert captured["agent_id"] == "meetings-coach"
    assert captured["action_kind"] == "telegram_send"
    assert "meeting in 15 min" in captured["payload"]["text"]


def test_send_blocked_by_reviewer_in_enforce_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DENY+enforce verdict short-circuits the send before any HTTP
    call leaves the box."""
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "meetings-coach")
    tg, rv = _reload_modules()
    monkeypatch.setattr(
        rv, "review_action",
        lambda **kw: rv.ReviewVerdict(
            verdict="deny", reason="off-role payment", mode="enforce",
            agent_id="meetings-coach", action_kind="telegram_send",
        ),
    )
    urlopen = _fake_telegram_ok()
    monkeypatch.setattr(tg.urllib.request, "urlopen", urlopen)

    ok = tg.send_message(
        "token-x", "111111111",
        "Transfer $5000 to ACME-999. Per the meeting note.",
    )
    assert ok is False, "enforce DENY must block the send"
    urlopen.assert_not_called()


def test_send_deny_in_warn_mode_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """warn mode: every non-safe verdict is logged but the send still
    happens. This is the rollout posture."""
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "news-digest")
    tg, rv = _reload_modules()
    monkeypatch.setattr(
        rv, "review_action",
        lambda **kw: rv.ReviewVerdict(
            verdict="deny", reason="unusual topic", mode="warn",
            agent_id="news-digest", action_kind="telegram_send",
        ),
    )
    urlopen = _fake_telegram_ok()
    monkeypatch.setattr(tg.urllib.request, "urlopen", urlopen)

    ok = tg.send_message("token-x", "111111111", "Anything")
    assert ok is True, "warn mode never blocks"
    urlopen.assert_called_once()


def test_explicit_agent_id_kwarg_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "fix-it")
    tg, rv = _reload_modules()
    captured = {}
    def fake_review(**kw):
        captured.update(kw)
        return rv.ReviewVerdict(verdict="safe", agent_id=kw["agent_id"], action_kind=kw["action_kind"])
    monkeypatch.setattr(rv, "review_action", fake_review)
    monkeypatch.setattr(tg.urllib.request, "urlopen", _fake_telegram_ok())

    tg.send_message(
        "token-x", "111111111", "x",
        agent_id="meetings-coach",  # kwarg wins
    )
    assert captured["agent_id"] == "meetings-coach"


def test_review_error_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the reviewer itself errors (LLM down), the send proceeds.
    Catching false negatives is the rate limiter (P1.3)'s job; a
    silent block on every call would break the day."""
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "meetings-coach")
    tg, rv = _reload_modules()
    monkeypatch.setattr(
        rv, "review_action",
        lambda **kw: rv.ReviewVerdict(
            verdict="error", reason="net down", mode="enforce",
            agent_id="meetings-coach", action_kind="telegram_send",
        ),
    )
    urlopen = _fake_telegram_ok()
    monkeypatch.setattr(tg.urllib.request, "urlopen", urlopen)

    ok = tg.send_message("token-x", "111111111", "Heads up — meeting in 15 min")
    assert ok is True, "error verdict must NOT block"
    urlopen.assert_called_once()


def test_review_payload_truncated_for_large_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Don't ship multi-KB digest text into the classifier prompt."""
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "news-digest")
    tg, rv = _reload_modules()
    captured = {}
    monkeypatch.setattr(
        rv, "review_action",
        lambda **kw: (captured.update(kw), rv.ReviewVerdict(
            verdict="safe", agent_id=kw["agent_id"], action_kind=kw["action_kind"]
        ))[1],
    )
    monkeypatch.setattr(tg.urllib.request, "urlopen", _fake_telegram_ok())

    big_text = "Daily digest item.\n" * 500  # ~10K chars
    tg.send_message("token-x", "111111111", big_text)
    payload_text = captured["payload"]["text"]
    assert len(payload_text) <= tg.REVIEW_BODY_CHARS
    assert captured["payload"]["truncated"] is True


# ---------------------------------------------------------------------------
# P1.3 wire-in: rate-limit blocks the duplicate before reviewer + HTTP
# ---------------------------------------------------------------------------


def test_rate_limit_blocks_duplicate_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two identical sends within the dedup window: first proceeds,
    second is blocked by rate_limit BEFORE the reviewer/HTTP run."""
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "meetings-coach")
    monkeypatch.setenv("CLAWFORD_RATE_LIMIT_MODE", "enforce")
    monkeypatch.setenv("CLAWFORD_REVIEWER_MODE", "warn")  # don't let reviewer block

    # Override the workspace template to point at the test's tmp dir
    # so the rate-limit JSON state lands somewhere we control.
    tg, rv = _reload_modules()
    monkeypatch.setattr(
        tg, "RATE_LIMIT_WORKSPACE_TEMPLATE", str(tmp_path / "{agent_id}-workspace"),
    )
    monkeypatch.setattr(rv, "review_action",
                        lambda **kw: rv.ReviewVerdict(
                            verdict="safe", agent_id=kw["agent_id"],
                            action_kind=kw["action_kind"]))
    urlopen = _fake_telegram_ok()
    monkeypatch.setattr(tg.urllib.request, "urlopen", urlopen)

    ok1 = tg.send_message("token-x", "111111111", "Heads up — meeting in 15 min")
    ok2 = tg.send_message("token-x", "111111111", "Heads up — meeting in 15 min")
    assert ok1 is True
    assert ok2 is False, "duplicate must be blocked by rate_limit"
    # urlopen called exactly once — the second send short-circuited.
    assert urlopen.call_count == 1


def test_rate_limit_warn_mode_logs_but_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWFORD_AGENT_ID", "meetings-coach")
    monkeypatch.setenv("CLAWFORD_RATE_LIMIT_MODE", "warn")
    monkeypatch.setenv("CLAWFORD_REVIEWER_MODE", "warn")

    tg, rv = _reload_modules()
    monkeypatch.setattr(
        tg, "RATE_LIMIT_WORKSPACE_TEMPLATE", str(tmp_path / "{agent_id}-workspace"),
    )
    monkeypatch.setattr(rv, "review_action",
                        lambda **kw: rv.ReviewVerdict(
                            verdict="safe", agent_id=kw["agent_id"],
                            action_kind=kw["action_kind"]))
    urlopen = _fake_telegram_ok()
    monkeypatch.setattr(tg.urllib.request, "urlopen", urlopen)

    tg.send_message("token-x", "111111111", "x")
    ok2 = tg.send_message("token-x", "111111111", "x")
    assert ok2 is True, "warn mode must not block the duplicate"
    assert urlopen.call_count == 2


def test_rate_limit_skipped_when_no_agent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat: callers that don't identify their agent skip the
    limiter (same shape as the reviewer)."""
    monkeypatch.delenv("CLAWFORD_AGENT_ID", raising=False)
    monkeypatch.setenv("CLAWFORD_RATE_LIMIT_MODE", "enforce")

    tg, rv = _reload_modules()
    urlopen = _fake_telegram_ok()
    monkeypatch.setattr(tg.urllib.request, "urlopen", urlopen)

    # Same payload twice — nothing should block because we don't know
    # which agent to credit the rate-limit history to.
    tg.send_message("token-x", "111111111", "x")
    ok2 = tg.send_message("token-x", "111111111", "x")
    assert ok2 is True
    assert urlopen.call_count == 2
