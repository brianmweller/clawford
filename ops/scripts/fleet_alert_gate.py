#!/usr/bin/env python3
"""fleet_alert_gate.py — Telegram notification gate for fleet-health.

Replaces the "send every 15 min while degraded" behavior with:
  - transitions (new alert / alert-text change / recovery) → SEND
  - same alert persisting → SEND at 1h / 4h / 24h reminders, then silent

Invoked from ops/scripts/fleet-health-host.sh just before the
curl-to-Telegram call. Reads fleet-health.py's SCRIPT_CONTRACT
summary JSON from argv[1], consults the state file, updates it,
and prints the message to send (or empty string if suppressed).

Exit 0 = always (reservations about stdout).
Stdout:
    <message-text>\n   if a Telegram send should fire
    (empty)            if suppressed

The shell wrapper sends iff stdout is non-empty.

State file (default ~/.clawford/fleet-alert-state.json):
    {
      "status":            "ok" | "degraded" | "error",
      "alert":             "<full alert text>" | null,
      "first_seen_utc":    "2026-04-19T20:00:00Z",
      "last_notified_utc": "2026-04-19T20:00:00Z",
      "reminder_index":    0..3
    }
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Reminder ladder, in seconds from first_seen.
# Index 0 = initial alert (always sent when state changes).
# Index 1 = 1h reminder; 2 = 4h; 3 = 24h. Past 3, go silent.
REMINDER_THRESHOLDS_S = {1: 3600, 2: 4 * 3600, 3: 24 * 3600}

DEFAULT_STATE_FILE = Path.home() / ".clawford" / "fleet-alert-state.json"


@dataclass
class Decision:
    send: bool
    message: str


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _humanize_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


def _load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _extract_alert(summary: dict) -> str:
    return summary.get("alert") or summary.get("error") or ""


def decide(summary: dict, state_path: Path, now_iso: str | None = None) -> Decision:
    """Gate decision: should we send a Telegram for this fleet summary?

    Args:
        summary:    SCRIPT_CONTRACT JSON from fleet-health.py stdout.
        state_path: path to the persistent state file.
        now_iso:    override current time (tests). ISO-8601 Z string.

    Returns:
        Decision(send, message). If send is True, caller ships `message`
        to Telegram. Either way, the state file is updated as a side
        effect to reflect the current observation.
    """
    now_iso = now_iso or _now_iso()
    now = _parse_iso(now_iso)
    cur_status = summary.get("status", "error")
    cur_alert = _extract_alert(summary)
    state = _load_state(state_path)

    # Branch A: fleet is currently healthy.
    if cur_status == "ok":
        if state is None or state.get("status") == "ok":
            return Decision(send=False, message="")
        # Recovery from a previously-degraded state.
        prev_alert = state.get("alert") or "(unknown)"
        first_seen = state.get("first_seen_utc")
        duration_s = int((now - _parse_iso(first_seen)).total_seconds()) if first_seen else 0
        msg = (
            f"✅ fleet recovered after {_humanize_duration(duration_s)} "
            f"(was: {prev_alert})"
        )
        _save_state(state_path, {
            "status": "ok",
            "alert": None,
            "first_seen_utc": now_iso,
            "last_notified_utc": now_iso,
            "reminder_index": 0,
        })
        return Decision(send=True, message=msg)

    # Branch B: fleet is degraded/errored.
    # Two sub-cases: a brand-new alert (or first observation), or
    # the same alert continuing from a prior tick.
    if state is None or state.get("status") == "ok" or state.get("alert") != cur_alert:
        _save_state(state_path, {
            "status": cur_status,
            "alert": cur_alert,
            "first_seen_utc": now_iso,
            "last_notified_utc": now_iso,
            "reminder_index": 0,
        })
        return Decision(send=True, message=cur_alert)

    # Same alert persisting — apply reminder ladder.
    first_seen = _parse_iso(state["first_seen_utc"])
    elapsed = int((now - first_seen).total_seconds())
    reminder_index = int(state.get("reminder_index", 0))
    next_idx = reminder_index + 1
    threshold = REMINDER_THRESHOLDS_S.get(next_idx)

    if threshold is None or elapsed < threshold:
        # Either past the last ladder rung or not yet time for the next one.
        return Decision(send=False, message="")

    msg = (
        f"🔁 still degraded after {_humanize_duration(elapsed)}: {cur_alert}"
    )
    state["reminder_index"] = next_idx
    state["last_notified_utc"] = now_iso
    _save_state(state_path, state)
    return Decision(send=True, message=msg)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("", end="")
        return 0
    summary_arg = argv[1]
    try:
        summary = json.loads(summary_arg)
    except (json.JSONDecodeError, TypeError):
        print("", end="")
        return 0
    state_path = Path(os.environ.get("FLEET_ALERT_STATE_FILE", str(DEFAULT_STATE_FILE)))
    decision = decide(summary, state_path)
    if decision.send:
        print(decision.message)
    else:
        print("", end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
