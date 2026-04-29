#!/usr/bin/env python3
"""costco-tunnel-watchdog.py — health probe + auto-heal for Hilda's
SOCKS5 residential-egress tunnel.

Background
----------
costco-socks-tunnel.service runs `autossh` from the VPS to the operator's
ThinkPad over Tailscale, opening 127.0.0.1:1080 as a SOCKS5 proxy
that gives Costco's Akamai protection a residential-IP fingerprint.

Two failure modes have been observed:

  (a) autossh+ssh stuck after Tailscale path renegotiation —
      `systemctl restart costco-socks-tunnel` clears it.
  (b) Tailscale daemon itself stuck on a stale path —
      `systemctl restart tailscaled` clears it (autossh restart is
      a no-op until tailscaled refreshes its path table).

State machine
-------------
The watchdog tracks per-outage state, not just per-tick state, so it
alerts on transitions rather than spamming the operator every 15 min during
a sustained outage. Today (2026-04-29 morning) the v1 watchdog paged
five identical restart messages in 75 minutes; this rewrite holds to
one page per state-change moment.

  Healthy → Healthy            : silent
  Healthy → Outage (3 fails)   : restart tunnel, page INITIAL
  Outage  → Outage (subsequent): restart tunnel, silent
  Outage (3 restarts deep)     : restart tailscaled + tunnel, page ESCALATION
  Outage (post-escalation)     : silent — the operator's been told twice
  Outage  → Healthy            : page RECOVERY, reset state

Conforms to agents/shared/SCRIPT_CONTRACT.md: always exits 0, prints
one JSON envelope. Wired via ops/scripts/install-host-cron.sh as a
*/5 host cron with the SHOPPING_BOT_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

STATE_FILE = Path("/home/openclaw/.clawford/shopping-workspace/cache/tunnel-watchdog-state.json")
SOCKS_PORT_DEFAULT = 1080
PROBE_URL = "https://api.ipify.org"
PROBE_TIMEOUT_S = 8

FAILURE_THRESHOLD = 3            # restart after N consecutive failures
THROTTLE_SECONDS = 300           # min interval between any restart action
ESCALATE_AFTER_RESTARTS = 3      # tailscaled restart after this many tunnel restarts in one outage

TUNNEL_UNIT = "costco-socks-tunnel"
TAILSCALED_UNIT = "tailscaled"

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@dataclass
class Decision:
    """What the watchdog should do this tick.

    action ∈ {none, restart, restart_silent, escalate_tailscaled, recovery}.
    Pure outcome of the state-machine — main() turns this into actual
    restarts + envelope text.
    """
    action: str
    reason: str


def decide(
    *,
    probe_ok: bool,
    last_restart_age_s: float | None,
    prior_failures: int,
    restarts_in_outage: int,
    already_alerted: bool,
) -> Decision:
    """Pure state-transition. Tested in test_costco_tunnel_watchdog.py."""
    if probe_ok:
        if already_alerted:
            return Decision(action="recovery", reason="probe ok after paged outage")
        return Decision(action="none", reason="probe ok")

    consecutive = prior_failures + 1
    if consecutive < FAILURE_THRESHOLD:
        return Decision(
            action="none",
            reason=f"{consecutive} consecutive failures (need {FAILURE_THRESHOLD})",
        )

    # Below threshold cleared. Check throttle next.
    if last_restart_age_s is not None and last_restart_age_s < THROTTLE_SECONDS:
        return Decision(
            action="none",
            reason=f"throttled — last restart {int(last_restart_age_s)}s ago",
        )

    # Eligible for some restart action. Pick which.
    if restarts_in_outage == ESCALATE_AFTER_RESTARTS:
        return Decision(
            action="escalate_tailscaled",
            reason=f"{restarts_in_outage} tunnel restarts ineffective; kick tailscaled",
        )

    if restarts_in_outage > ESCALATE_AFTER_RESTARTS:
        # Already escalated. the operator's been told twice. Stop poking the
        # bear — further restarts amplify thrash without fixing.
        return Decision(
            action="none",
            reason="post-escalation; awaiting recovery or human intervention",
        )

    if not already_alerted:
        return Decision(action="restart", reason="first restart of outage")
    return Decision(action="restart_silent", reason="subsequent restart in same outage")


def probe_socks(*, socks_port: int = SOCKS_PORT_DEFAULT, timeout_s: int = PROBE_TIMEOUT_S) -> str | None:
    """Curl through the SOCKS5 proxy. Return the IP on success, else None."""
    try:
        result = subprocess.run(
            [
                "curl", "-s",
                "--max-time", str(timeout_s),
                "-x", f"socks5h://127.0.0.1:{socks_port}",
                PROBE_URL,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s + 2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    body = (result.stdout or "").strip()
    return body if _IP_RE.match(body) else None


def restart_tunnel() -> None:
    subprocess.run(
        ["sudo", "-n", "systemctl", "restart", TUNNEL_UNIT],
        check=False,
        capture_output=True,
    )


def restart_tailscaled() -> None:
    """Higher-blast-radius kick: also bounces every other Tailscale-
    routed connection on the box. Used only after autossh-only
    restarts have proven ineffective."""
    subprocess.run(
        ["sudo", "-n", "systemctl", "restart", TAILSCALED_UNIT],
        check=False,
        capture_output=True,
    )


_DEFAULT_STATE = {
    "consecutive_failures": 0,
    "last_restart_ts": None,
    "restarts_in_outage": 0,
    "already_alerted": False,
}


def load_state(path: Path) -> dict:
    """Read persisted state. Tolerates missing/corrupt files and
    older schemas (back-compat with the v1 two-field state file
    that may already be on disk)."""
    base = dict(_DEFAULT_STATE)
    if not path.exists():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict):
        return base
    return {
        "consecutive_failures": int(data.get("consecutive_failures") or 0),
        "last_restart_ts": data.get("last_restart_ts"),
        "restarts_in_outage": int(data.get("restarts_in_outage") or 0),
        "already_alerted": bool(data.get("already_alerted") or False),
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_envelope(
    *,
    probe_ok: bool,
    action: str,
    prior_failures: int,
    residential_ip: str | None,
) -> dict:
    """Compose the SCRIPT_CONTRACT envelope from the action chosen
    this tick. Alerts ONLY on state-change moments.

      restart            → INITIAL alert (first restart of outage)
      restart_silent     → no alert (already paged this outage)
      escalate_tailscaled→ ESCALATION alert (autossh wasn't enough)
      recovery           → RECOVERY alert (closure)
      none               → no alert
    """
    if action == "restart":
        return {
            "status": "error",
            "alert": (
                "\U0001F50C costco tunnel offline — restarted "
                f"{TUNNEL_UNIT} (auto-heal). Will escalate to "
                f"tailscaled if {ESCALATE_AFTER_RESTARTS} restarts "
                f"don't help."
            ),
            "consecutive_failures": prior_failures + 1,
            "summary": "tunnel restarted (initial)",
        }

    if action == "escalate_tailscaled":
        return {
            "status": "error",
            "alert": (
                "\U000026A1 costco tunnel still offline after "
                f"{ESCALATE_AFTER_RESTARTS} {TUNNEL_UNIT} restarts — "
                f"escalated to {TAILSCALED_UNIT} restart. Check "
                "ThinkPad if Telegram quiets."
            ),
            "consecutive_failures": prior_failures + 1,
            "summary": "escalated to tailscaled restart",
        }

    if action == "recovery":
        ip_hint = f" (egress IP {residential_ip})" if residential_ip else ""
        return {
            "status": "ok",
            "alert": f"\U00002705 costco tunnel recovered{ip_hint}.",
            "summary": "tunnel restored",
            "tunnel_ip": residential_ip,
        }

    # action ∈ {none, restart_silent}: no alert text
    if probe_ok:
        return {
            "status": "ok",
            "tunnel_ip": residential_ip,
            "summary": "tunnel healthy",
        }
    return {
        "status": "error",
        "consecutive_failures": prior_failures + 1,
        "summary": "tunnel unhealthy (silent — already paged or below threshold)",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socks-port", type=int, default=SOCKS_PORT_DEFAULT)
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    ip = probe_socks(socks_port=args.socks_port)
    probe_ok = ip is not None

    state = load_state(STATE_FILE)
    prior_failures = state["consecutive_failures"]
    last_restart_ts = state["last_restart_ts"]
    last_restart_age_s = (
        (time.time() - last_restart_ts) if last_restart_ts else None
    )

    decision = decide(
        probe_ok=probe_ok,
        last_restart_age_s=last_restart_age_s,
        prior_failures=prior_failures,
        restarts_in_outage=state["restarts_in_outage"],
        already_alerted=state["already_alerted"],
    )

    # Apply decision: side effects + new state.
    if decision.action == "recovery":
        new_state = dict(_DEFAULT_STATE)
    elif decision.action in ("restart", "restart_silent"):
        restart_tunnel()
        new_state = {
            "consecutive_failures": 0,  # give the restart a chance
            "last_restart_ts": time.time(),
            "restarts_in_outage": state["restarts_in_outage"] + 1,
            "already_alerted": True,
        }
    elif decision.action == "escalate_tailscaled":
        # Both kicks: tailscaled first to clear path table, then
        # autossh so it picks up the new path immediately.
        restart_tailscaled()
        restart_tunnel()
        new_state = {
            "consecutive_failures": 0,
            "last_restart_ts": time.time(),
            "restarts_in_outage": state["restarts_in_outage"] + 1,
            "already_alerted": True,
        }
    else:  # action == "none"
        if probe_ok:
            new_state = dict(_DEFAULT_STATE)
        else:
            new_state = {
                "consecutive_failures": prior_failures + 1,
                "last_restart_ts": last_restart_ts,
                "restarts_in_outage": state["restarts_in_outage"],
                "already_alerted": state["already_alerted"],
            }

    save_state(STATE_FILE, new_state)

    envelope = build_envelope(
        probe_ok=probe_ok,
        action=decision.action,
        prior_failures=prior_failures,
        residential_ip=ip,
    )
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
