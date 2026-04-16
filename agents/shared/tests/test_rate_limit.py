"""P1.3 — outbound rate limit + dedup tests.

The deterministic backstop for the LLM-based outbound reviewer.
Two checks: per-(agent, tool, parameters_hash) dedup window (catches
the 5x-resend class), and per-(agent, tool) volume cap.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

from rate_limit import (  # noqa: E402
    DEDUP_WINDOW_S,
    DEFAULT_MODE,
    DEFAULT_PER_HOUR_LIMIT,
    KILL_SWITCH_PATH,
    MODE_ENV_VAR,
    RateLimitVerdict,
    WINDOW_S,
    _current_mode,
    _per_hour_limit,
    check_rate_limit,
)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def test_default_mode_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODE_ENV_VAR, raising=False)
    monkeypatch.setattr("rate_limit._kill_switch_active", lambda: False)
    assert _current_mode() == "warn"
    assert DEFAULT_MODE == "warn"


def test_env_var_overrides_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rate_limit._kill_switch_active", lambda: False)
    monkeypatch.setenv(MODE_ENV_VAR, "enforce")
    assert _current_mode() == "enforce"


def test_kill_switch_file_forces_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV_VAR, "enforce")
    monkeypatch.setattr("rate_limit._kill_switch_active", lambda: True)
    assert _current_mode() == "skip"


def test_invalid_mode_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rate_limit._kill_switch_active", lambda: False)
    monkeypatch.setenv(MODE_ENV_VAR, "loud-and-proud")
    assert _current_mode() == "warn"


# ---------------------------------------------------------------------------
# Per-tool override
# ---------------------------------------------------------------------------


def test_default_per_hour_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAWFORD_RATE_LIMIT_TELEGRAM_SEND_PER_HOUR", raising=False)
    assert _per_hour_limit("telegram_send") == DEFAULT_PER_HOUR_LIMIT


def test_per_tool_override_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAWFORD_RATE_LIMIT_TELEGRAM_SEND_PER_HOUR", "50")
    assert _per_hour_limit("telegram_send") == 50


def test_per_tool_override_handles_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAWFORD_RATE_LIMIT_TELEGRAM_SEND_PER_HOUR", "abc")
    assert _per_hour_limit("telegram_send") == DEFAULT_PER_HOUR_LIMIT


# ---------------------------------------------------------------------------
# Verdict semantics
# ---------------------------------------------------------------------------


def test_verdict_allowed_is_not_blocking() -> None:
    v = RateLimitVerdict(allowed=True, mode="enforce")
    assert v.blocking is False


def test_verdict_denied_in_warn_does_not_block() -> None:
    v = RateLimitVerdict(allowed=False, mode="warn", kind="duplicate")
    assert v.blocking is False


def test_verdict_denied_in_enforce_blocks() -> None:
    v = RateLimitVerdict(allowed=False, mode="enforce", kind="volume_cap")
    assert v.blocking is True


# ---------------------------------------------------------------------------
# check_rate_limit — happy path
# ---------------------------------------------------------------------------


def test_first_call_is_allowed(tmp_path: Path) -> None:
    v = check_rate_limit(
        agent_id="meetings-coach", tool="telegram_send",
        parameters_hash="abc", workspace=tmp_path,
        mode="enforce", now_s=1000.0,
    )
    assert v.allowed is True
    assert v.kind == "ok"


def test_state_persisted_to_workspace(tmp_path: Path) -> None:
    check_rate_limit(
        agent_id="meetings-coach", tool="telegram_send",
        parameters_hash="abc", workspace=tmp_path,
        mode="enforce", now_s=1000.0,
    )
    state_file = tmp_path / "cache" / "rate-limits.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "meetings-coach::telegram_send" in state


# ---------------------------------------------------------------------------
# Dedup window — the canonical 5x-resend defense
# ---------------------------------------------------------------------------


def test_same_hash_within_window_blocked(tmp_path: Path) -> None:
    """Replay the 5x resend at the second send: identical parameters_hash
    within the dedup window → block."""
    h = "duplicate-payload-hash"
    v1 = check_rate_limit(
        agent_id="meetings-coach", tool="telegram_send",
        parameters_hash=h, workspace=tmp_path,
        mode="enforce", now_s=1000.0,
    )
    assert v1.allowed is True

    # 60 seconds later, identical payload.
    v2 = check_rate_limit(
        agent_id="meetings-coach", tool="telegram_send",
        parameters_hash=h, workspace=tmp_path,
        mode="enforce", now_s=1060.0,
    )
    assert v2.allowed is False
    assert v2.kind == "duplicate"
    assert v2.blocking is True
    assert "60s ago" in v2.reason


def test_same_hash_outside_window_allowed(tmp_path: Path) -> None:
    h = "abc"
    check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash=h,
        workspace=tmp_path, mode="enforce", now_s=1000.0,
    )
    later = 1000.0 + DEDUP_WINDOW_S + 60
    v = check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash=h,
        workspace=tmp_path, mode="enforce", now_s=later,
    )
    assert v.allowed is True


def test_different_hashes_in_window_both_allowed(tmp_path: Path) -> None:
    """Two distinct messages back-to-back are fine (until the volume cap)."""
    v1 = check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash="msg1",
        workspace=tmp_path, mode="enforce", now_s=1000.0,
    )
    v2 = check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash="msg2",
        workspace=tmp_path, mode="enforce", now_s=1010.0,
    )
    assert v1.allowed and v2.allowed


def test_dedup_in_warn_mode_still_logs_but_does_not_block(tmp_path: Path) -> None:
    h = "abc"
    check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash=h,
        workspace=tmp_path, mode="warn", now_s=1000.0,
    )
    v = check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash=h,
        workspace=tmp_path, mode="warn", now_s=1010.0,
    )
    assert v.allowed is False  # dedup detected
    assert v.kind == "duplicate"
    assert v.blocking is False  # warn mode never blocks


# ---------------------------------------------------------------------------
# Volume cap
# ---------------------------------------------------------------------------


def test_volume_cap_blocks_after_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWFORD_RATE_LIMIT_TELEGRAM_SEND_PER_HOUR", "3")
    # Three distinct hashes — dedup is fine, only volume should kick in.
    for i in range(3):
        v = check_rate_limit(
            agent_id="x", tool="telegram_send",
            parameters_hash=f"hash-{i}",
            workspace=tmp_path, mode="enforce", now_s=1000.0 + i,
        )
        assert v.allowed is True, f"send #{i} unexpectedly denied: {v.reason}"

    # Fourth distinct hash — volume cap fires.
    v4 = check_rate_limit(
        agent_id="x", tool="telegram_send",
        parameters_hash="hash-4",
        workspace=tmp_path, mode="enforce", now_s=1003.0,
    )
    assert v4.allowed is False
    assert v4.kind == "volume_cap"
    assert v4.blocking is True


def test_volume_window_evicts_old_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sends from > WINDOW_S ago shouldn't count toward the cap."""
    monkeypatch.setenv("CLAWFORD_RATE_LIMIT_TELEGRAM_SEND_PER_HOUR", "2")
    check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash="old1",
        workspace=tmp_path, mode="enforce", now_s=1000.0,
    )
    check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash="old2",
        workspace=tmp_path, mode="enforce", now_s=1001.0,
    )
    # Now jump past the window.
    later = 1000.0 + WINDOW_S + 60
    v = check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash="fresh",
        workspace=tmp_path, mode="enforce", now_s=later,
    )
    assert v.allowed is True


def test_per_agent_isolation(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One agent hitting the cap doesn't impact another agent's quota."""
    monkeypatch.setenv("CLAWFORD_RATE_LIMIT_TELEGRAM_SEND_PER_HOUR", "2")
    ws_a = tmp_path_factory.mktemp("a-workspace")
    ws_b = tmp_path_factory.mktemp("b-workspace")

    for i in range(2):
        check_rate_limit(
            agent_id="a", tool="telegram_send", parameters_hash=f"a-{i}",
            workspace=ws_a, mode="enforce", now_s=1000.0 + i,
        )
    # a is now at the cap.
    v_a3 = check_rate_limit(
        agent_id="a", tool="telegram_send", parameters_hash="a-2",
        workspace=ws_a, mode="enforce", now_s=1003.0,
    )
    assert v_a3.allowed is False
    # b's first send is still fine — different workspace, different state.
    v_b1 = check_rate_limit(
        agent_id="b", tool="telegram_send", parameters_hash="b-0",
        workspace=ws_b, mode="enforce", now_s=1003.0,
    )
    assert v_b1.allowed is True


# ---------------------------------------------------------------------------
# Skip mode + kill switch
# ---------------------------------------------------------------------------


def test_skip_mode_passes_through(tmp_path: Path) -> None:
    h = "abc"
    for _ in range(100):
        v = check_rate_limit(
            agent_id="x", tool="telegram_send", parameters_hash=h,
            workspace=tmp_path, mode="skip", now_s=1000.0,
        )
        assert v.allowed is True
        assert v.kind == "skipped"


def test_kill_switch_engages_skip_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rate_limit._kill_switch_active", lambda: True)
    monkeypatch.setenv(MODE_ENV_VAR, "enforce")
    h = "abc"
    # Even with mode=enforce in env, the kill switch forces skip.
    for _ in range(50):
        v = check_rate_limit(
            agent_id="x", tool="telegram_send", parameters_hash=h,
            workspace=tmp_path, now_s=1000.0,
        )
        assert v.allowed is True
        assert v.kind == "skipped"


# ---------------------------------------------------------------------------
# State file resilience
# ---------------------------------------------------------------------------


def test_corrupt_state_file_falls_open(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "rate-limits.json").write_text("{not valid json", encoding="utf-8")
    v = check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash="abc",
        workspace=tmp_path, mode="enforce", now_s=1000.0,
    )
    assert v.allowed is True


def test_missing_state_file_starts_clean(tmp_path: Path) -> None:
    """No prior state → first call always allowed, then state is created."""
    v = check_rate_limit(
        agent_id="x", tool="telegram_send", parameters_hash="abc",
        workspace=tmp_path, mode="enforce", now_s=1000.0,
    )
    assert v.allowed is True
    assert (tmp_path / "cache" / "rate-limits.json").exists()
