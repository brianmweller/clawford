#!/usr/bin/env python3
"""costco-tunnel-watchdog.py — health probe + auto-restart for the
SOCKS5 residential-egress tunnel that Hilda's Costco refresh depends
on.

Background
----------
costco-socks-tunnel.service runs `autossh` from the VPS to the operator's
ThinkPad over Tailscale, opening 127.0.0.1:1080 as a SOCKS5 proxy
that gives Costco's Akamai protection a residential-IP fingerprint.

The Costco refresh daemon already detects "tunnel offline" via its
`[safety-net]` branch, but only logs and skips. fleet-health then
surfaces the cascade as "shopping degraded: costco JWT expired" —
which sends the operator to the wrong fix (the tunnel is the actual
problem, not the JWT).

Observed failure mode (2026-04-29): autossh stays alive but its SSH
child times out repeatedly for hours after a Tailscale path re-
negotiation. A `systemctl restart costco-socks-tunnel` clears it
instantly. This watchdog automates that restart.

Policy
------
- Probe SOCKS by curling api.ipify.org through 127.0.0.1:1080.
- Healthy = HTTP 200 with an IP-shaped response body (any IP — we
  don't try to fingerprint "residential" because that's brittle).
- 3 consecutive probe failures → restart the systemd unit.
- 5-minute throttle between restarts (no thrashing).
- Page the operator only when a restart actually fires; silent otherwise
  (including silent during a sustained outage between restarts —
  fleet-health and Costco's own alerts will still surface it).

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON envelope.
Wired via ops/scripts/install-host-cron.sh as a */5 host cron.
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

FAILURE_THRESHOLD = 3   # restart after N consecutive failures
THROTTLE_SECONDS = 300  # min interval between restarts (5 min)

SERVICE_UNIT = "costco-socks-tunnel"

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@dataclass
class Decision:
    restart: bool
    reason: str


def decide(
    *,
    probe_ok: bool,
    last_restart_age_s: float | None,
    prior_failures: int,
) -> Decision:
    """Pure restart-policy. Tested in test_costco_tunnel_watchdog.py.

    `prior_failures` is the count BEFORE this tick (i.e., the number
    of consecutive failures observed so far). The current tick adds
    one more if probe_ok is False, which is what makes the threshold
    "third consecutive failure" trigger correctly.
    """
    if probe_ok:
        return Decision(restart=False, reason="probe ok")

    consecutive = prior_failures + 1

    if consecutive < FAILURE_THRESHOLD:
        return Decision(
            restart=False,
            reason=f"only {consecutive} consecutive failures (need {FAILURE_THRESHOLD})",
        )

    if last_restart_age_s is not None and last_restart_age_s < THROTTLE_SECONDS:
        return Decision(
            restart=False,
            reason=f"throttled — last restart {int(last_restart_age_s)}s ago, recently restarted",
        )

    return Decision(
        restart=True,
        reason=f"{consecutive} consecutive failures and throttle window cleared",
    )


def probe_socks(*, socks_port: int = SOCKS_PORT_DEFAULT, timeout_s: int = PROBE_TIMEOUT_S) -> str | None:
    """Curl through the SOCKS5 proxy. Return the IP on success, else None."""
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
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
    if not _IP_RE.match(body):
        return None
    return body


def restart_tunnel() -> None:
    """systemctl restart the unit (passwordless sudo configured)."""
    subprocess.run(
        ["sudo", "-n", "systemctl", "restart", SERVICE_UNIT],
        check=False,
        capture_output=True,
    )


def load_state(path: Path) -> dict:
    """Read the persisted watchdog state. Tolerates missing/corrupt files."""
    if not path.exists():
        return {"consecutive_failures": 0, "last_restart_ts": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"consecutive_failures": 0, "last_restart_ts": None}
    return {
        "consecutive_failures": int(data.get("consecutive_failures") or 0),
        "last_restart_ts": data.get("last_restart_ts"),
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_envelope(
    *,
    probe_ok: bool,
    restarted: bool,
    prior_failures: int,
    residential_ip: str | None,
) -> dict:
    """Compose the SCRIPT_CONTRACT envelope.

    Page only on the moment of restart. Silent during sustained
    outages (Costco's own alerts + fleet-health already cover that).
    """
    if probe_ok:
        return {
            "status": "ok",
            "tunnel_ip": residential_ip,
            "summary": "tunnel healthy",
        }

    if restarted:
        return {
            "status": "error",
            "alert": (
                "\U0001F50C costco tunnel unhealthy after "
                f"{prior_failures + 1} consecutive probes — restarted "
                f"{SERVICE_UNIT}. Check ThinkPad if Telegram quiets."
            ),
            "consecutive_failures": prior_failures + 1,
            "summary": "restarted on threshold",
        }

    return {
        "status": "error",
        "consecutive_failures": prior_failures + 1,
        "summary": "tunnel unhealthy (below threshold or throttled)",
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
    )

    restarted = False
    if decision.restart:
        restart_tunnel()
        restarted = True
        # On restart, clear the failure counter — the next probe will
        # tell us whether the restart helped.
        new_state = {
            "consecutive_failures": 0,
            "last_restart_ts": time.time(),
        }
    elif probe_ok:
        new_state = {"consecutive_failures": 0, "last_restart_ts": last_restart_ts}
    else:
        new_state = {
            "consecutive_failures": prior_failures + 1,
            "last_restart_ts": last_restart_ts,
        }
    save_state(STATE_FILE, new_state)

    envelope = build_envelope(
        probe_ok=probe_ok,
        restarted=restarted,
        prior_failures=prior_failures,
        residential_ip=ip,
    )
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
