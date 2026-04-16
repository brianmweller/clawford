"""rate_limit — outbound-action rate limiter (P1.3).

The deterministic backstop for the LLM-based outbound reviewer
(P0.1). Where the reviewer catches *semantic* anomalies ("Sergeant
Murphy proposing a payment doesn't fit his role"), the rate limiter
catches *quantitative* anomalies — the kind that produced the
2026-04-14 5x-resend incident, where Murphy's cron iterated over a
stale cache and sent the same body to the operator five times in twenty
minutes.

Two checks per outbound action:

  1. **Volume cap**: a per-(agent, tool) sliding window counts how
     many sends have happened in the last hour. Default 20/hour.
     Catches runaway crons that emit way more than usual.

  2. **Dedup window**: a per-(agent, tool, parameters_hash) record
     of the most recent send. Same hash within DEDUP_WINDOW_S
     blocks. Catches the 5x-resend class — at the second attempt
     the same `parameters_hash` lights up the dedup counter and the
     send is refused.

Both windows persist in `<workspace>/cache/rate-limits.json` so a
process restart doesn't reset the counters. State is best-effort
JSON; corruption falls open (allow).

Modes (CLAWFORD_RATE_LIMIT_MODE env var):

  - "warn" (default) — every breach logs to stderr but the call
    proceeds. Use during the rollout so a misjudged limit doesn't
    block real traffic.
  - "enforce" — breaches actually block (caller gets `allowed=False`).
  - "skip" — the limiter no-ops entirely. Emergency override.

Global kill switch: `~/.clawford/rate-limits-disabled` (any contents).
Touched once and the limiter behaves as if mode=skip until the file
is removed. Same file-based opt-out pattern as the rest of the fleet
(feedback_file_opt_out_pattern.md).

Usage from a wrapper:

    from agents.shared.rate_limit import check_rate_limit
    verdict = check_rate_limit(
        agent_id="meetings-coach",
        tool="telegram_send",
        parameters_hash=parameters_hash({"to": chat_id, "body": text}),
        workspace=Path("~/.clawford/meetings-coach-workspace").expanduser(),
    )
    if verdict.blocking:
        return False
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MODE_ENV_VAR = "CLAWFORD_RATE_LIMIT_MODE"
DEFAULT_MODE = "warn"
ALLOWED_MODES = ("warn", "enforce", "skip")

KILL_SWITCH_PATH = Path(os.path.expanduser("~/.clawford/rate-limits-disabled"))

# Per-tool overrides go via env var: CLAWFORD_RATE_LIMIT_<TOOL>_PER_HOUR.
DEFAULT_PER_HOUR_LIMIT = 20
# Same parameters_hash twice within this window = duplicate, block.
# 60 minutes covers the 5x-resend window with comfortable margin.
DEDUP_WINDOW_S = 3600
WINDOW_S = 3600  # the volume-cap window

# Trim per-(agent, tool) histories to this many recent timestamps so
# the JSON file stays small. ~24h of traffic at 20/hour is 480 entries;
# we keep 100 because anything older than the WINDOW_S cutoff is
# evicted on each check anyway.
MAX_HISTORY_ENTRIES = 100


def _kill_switch_active() -> bool:
    """Indirection so tests can monkeypatch the function rather than
    poke at the Path instance (which Python forbids)."""
    return KILL_SWITCH_PATH.exists()


def _current_mode() -> str:
    if _kill_switch_active():
        return "skip"
    raw = (os.environ.get(MODE_ENV_VAR) or DEFAULT_MODE).strip().lower()
    if raw not in ALLOWED_MODES:
        return DEFAULT_MODE
    return raw


def _per_hour_limit(tool: str) -> int:
    """Per-tool override via env var, e.g. CLAWFORD_RATE_LIMIT_TELEGRAM_SEND_PER_HOUR=50."""
    env_name = f"CLAWFORD_RATE_LIMIT_{tool.upper().replace('-', '_')}_PER_HOUR"
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return DEFAULT_PER_HOUR_LIMIT
    try:
        n = int(raw)
        return max(1, n)
    except ValueError:
        return DEFAULT_PER_HOUR_LIMIT


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass
class RateLimitVerdict:
    """Outcome of one check_rate_limit call.

    Attributes:
        allowed: True when the action should proceed.
        reason: Human-readable description on a non-allowed verdict
            (or empty string when allowed).
        kind: "ok" | "duplicate" | "volume_cap" | "skipped".
        mode: The resolved mode that was in effect.
        agent_id, tool, parameters_hash: Echoed for logging.

    `blocking` is True only when allowed=False AND mode=="enforce" —
    that's the single condition under which the caller short-circuits
    its action. In warn mode the verdict is reported but `blocking` is
    always False so the caller still proceeds (data-gathering posture).
    """

    allowed: bool
    reason: str = ""
    kind: str = "ok"
    mode: str = DEFAULT_MODE
    agent_id: str = ""
    tool: str = ""
    parameters_hash: str = ""

    @property
    def blocking(self) -> bool:
        return (not self.allowed) and self.mode == "enforce"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _state_path(workspace: Path) -> Path:
    return workspace / "cache" / "rate-limits.json"


def _load_state(workspace: Path) -> dict:
    """Load the per-workspace rate-limit state. Tolerates a missing or
    corrupted file by returning an empty dict (fail-open)."""
    path = _state_path(workspace)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(workspace: Path, state: dict) -> None:
    """Best-effort atomic write — failure to persist must NEVER raise
    out of check_rate_limit, so the caller's actual outbound flow is
    never broken by a disk error."""
    try:
        path = _state_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        print(f"[rate_limit] WARN: state write failed: {e}", file=sys.stderr)


def _record_key(agent_id: str, tool: str) -> str:
    return f"{agent_id}::{tool}"


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def check_rate_limit(
    *,
    agent_id: str,
    tool: str,
    parameters_hash: str,
    workspace: Path,
    now_s: Optional[float] = None,
    mode: Optional[str] = None,
) -> RateLimitVerdict:
    """Decide whether to allow one outbound action.

    Returns a RateLimitVerdict. Even on a deny, the call records the
    attempt in the history (so a subsequent call with the same
    parameters_hash counts as a "second attempt" that also denies).
    On allow, the timestamp + hash get recorded so the next call sees
    the right window.

    `now_s` and `mode` are injected for tests so the caller doesn't
    need to monkeypatch time or env vars.
    """
    active_mode = (mode or _current_mode()).lower()
    if active_mode not in ALLOWED_MODES:
        active_mode = DEFAULT_MODE

    base_verdict = RateLimitVerdict(
        allowed=True, kind="ok", mode=active_mode,
        agent_id=agent_id, tool=tool, parameters_hash=parameters_hash,
    )

    if active_mode == "skip":
        base_verdict.kind = "skipped"
        return base_verdict

    now = float(now_s) if now_s is not None else time.time()
    state = _load_state(workspace)
    key = _record_key(agent_id, tool)
    record = state.get(key) or {"history": []}
    history = record.get("history") or []

    # Evict anything older than WINDOW_S — keeps JSON bounded and the
    # volume cap accurate.
    cutoff = now - WINDOW_S
    history = [h for h in history if isinstance(h, dict) and h.get("ts", 0) >= cutoff]

    # Dedup check: same parameters_hash within DEDUP_WINDOW_S → block.
    dedup_cutoff = now - DEDUP_WINDOW_S
    duplicate = next(
        (h for h in history
         if h.get("parameters_hash") == parameters_hash
         and h.get("ts", 0) >= dedup_cutoff),
        None,
    )
    if duplicate:
        verdict = RateLimitVerdict(
            allowed=False,
            kind="duplicate",
            reason=(
                f"same parameters_hash sent {int(now - duplicate['ts'])}s ago "
                f"(within {DEDUP_WINDOW_S}s dedup window)"
            ),
            mode=active_mode, agent_id=agent_id, tool=tool,
            parameters_hash=parameters_hash,
        )
    else:
        # Volume check: hourly cap.
        per_hour = _per_hour_limit(tool)
        if len(history) >= per_hour:
            verdict = RateLimitVerdict(
                allowed=False,
                kind="volume_cap",
                reason=(
                    f"{len(history)} {tool} sends in last "
                    f"{WINDOW_S}s; cap is {per_hour}/hour"
                ),
                mode=active_mode, agent_id=agent_id, tool=tool,
                parameters_hash=parameters_hash,
            )
        else:
            verdict = base_verdict

    # Record the attempt — even denies count toward the next call's
    # window so a runaway loop doesn't sneak below the cap by burst-
    # retrying the same hash.
    history.append({
        "ts": now,
        "parameters_hash": parameters_hash,
        "allowed": verdict.allowed,
        "kind": verdict.kind,
    })
    # Keep only the most recent MAX_HISTORY_ENTRIES; older ones are
    # already past the WINDOW_S cutoff for the volume check.
    history = history[-MAX_HISTORY_ENTRIES:]
    state[key] = {"history": history}
    _save_state(workspace, state)

    return verdict
