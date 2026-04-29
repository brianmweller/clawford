"""Tests for ops/scripts/costco-tunnel-watchdog.py.

The watchdog probes the SOCKS5 endpoint that costco-socks-tunnel.service
opens (port 1080) and restarts the unit on sustained failure. It
exists because the Costco refresh daemon already detects "tunnel
offline" but only logs and skips — fleet-health then surfaces it as
a misleading "JWT expired" alert that points the operator at the wrong fix.

The decision function (`decide`) is pure: given the recent probe
history and last-restart timestamp, return whether to restart now.
Tests pin the decision logic without spawning curl or systemctl.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "scripts" / "costco-tunnel-watchdog.py"


def _import_module():
    spec = importlib.util.spec_from_file_location("costco_tunnel_watchdog", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["costco_tunnel_watchdog"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── decide() — pure restart-policy logic ────────────────────────────


def test_decide_no_restart_when_probe_healthy():
    m = _import_module()
    d = m.decide(probe_ok=True, last_restart_age_s=10_000, prior_failures=0)
    assert d.restart is False


def test_decide_no_restart_on_first_failure():
    """One transient failure shouldn't trigger a restart — that'd
    thrash the daemon during normal Tailscale path-renegotiation."""
    m = _import_module()
    d = m.decide(probe_ok=False, last_restart_age_s=10_000, prior_failures=0)
    assert d.restart is False


def test_decide_no_restart_on_second_failure():
    """Two failures still inside the noise band; wait for the third."""
    m = _import_module()
    d = m.decide(probe_ok=False, last_restart_age_s=10_000, prior_failures=1)
    assert d.restart is False


def test_decide_restart_after_third_consecutive_failure():
    """Three in a row at 5-min cadence = ~15 min outage. Restart."""
    m = _import_module()
    d = m.decide(probe_ok=False, last_restart_age_s=10_000, prior_failures=2)
    assert d.restart is True
    assert "3" in d.reason or "consecutive" in d.reason.lower()


def test_decide_skips_restart_when_recently_restarted():
    """Don't thrash. Min interval between restarts is 5 min."""
    m = _import_module()
    d = m.decide(probe_ok=False, last_restart_age_s=60, prior_failures=10)
    assert d.restart is False
    assert "recently" in d.reason.lower() or "throttle" in d.reason.lower()


def test_decide_restart_eligible_after_throttle_window():
    m = _import_module()
    # 6 minutes since last restart, 3 consecutive failures
    d = m.decide(probe_ok=False, last_restart_age_s=360, prior_failures=2)
    assert d.restart is True


def test_decide_no_restart_when_never_restarted_but_healthy():
    """last_restart_age_s = None means the watchdog never restarted yet.
    With probe_ok, do nothing."""
    m = _import_module()
    d = m.decide(probe_ok=True, last_restart_age_s=None, prior_failures=0)
    assert d.restart is False


def test_decide_can_restart_when_never_restarted_and_unhealthy():
    """No prior restart should not block the first restart action."""
    m = _import_module()
    d = m.decide(probe_ok=False, last_restart_age_s=None, prior_failures=2)
    assert d.restart is True


# ─── Envelope shape ──────────────────────────────────────────────────


def test_envelope_silent_when_healthy():
    m = _import_module()
    env = m.build_envelope(
        probe_ok=True,
        restarted=False,
        prior_failures=0,
        residential_ip="162.237.73.13",
    )
    assert env["status"] == "ok"
    assert "alert" not in env or not env.get("alert")


def test_envelope_alerts_when_restart_fired():
    """the operator wants to know when the watchdog actually intervenes —
    that signals an unattended Tailscale flap or the laptop sleeping."""
    m = _import_module()
    env = m.build_envelope(
        probe_ok=False,
        restarted=True,
        prior_failures=3,
        residential_ip=None,
    )
    assert env["status"] == "error"
    assert env["alert"]
    assert "tunnel" in env["alert"].lower()
    assert "restart" in env["alert"].lower()


def test_envelope_silent_when_unhealthy_but_throttled():
    """Don't page on every probe-fail tick if we've already restarted
    recently — avoid Telegram spam during a longer outage."""
    m = _import_module()
    env = m.build_envelope(
        probe_ok=False,
        restarted=False,
        prior_failures=5,
        residential_ip=None,
    )
    # status=error so it's visible in fleet logs, but no alert text
    # so the wrapper doesn't relay to Telegram repeatedly.
    assert env["status"] in ("ok", "error")
    assert not env.get("alert")


# ─── State persistence (consecutive-failures counter) ────────────────


def test_state_load_handles_missing_file(tmp_path):
    m = _import_module()
    state = m.load_state(tmp_path / "nope.json")
    assert state["consecutive_failures"] == 0
    assert state["last_restart_ts"] is None


def test_state_save_round_trips(tmp_path):
    m = _import_module()
    p = tmp_path / "state.json"
    m.save_state(p, {"consecutive_failures": 7, "last_restart_ts": 1700000000.0})
    state = m.load_state(p)
    assert state["consecutive_failures"] == 7
    assert state["last_restart_ts"] == 1700000000.0


def test_state_load_handles_corrupt_json(tmp_path):
    m = _import_module()
    p = tmp_path / "state.json"
    p.write_text("not-json", encoding="utf-8")
    state = m.load_state(p)
    assert state["consecutive_failures"] == 0


# ─── Probe with stubbed subprocess ───────────────────────────────────


def test_probe_socks_returns_ip_on_success(monkeypatch):
    m = _import_module()

    def fake_run(cmd, **kwargs):
        out = MagicMock()
        out.returncode = 0
        out.stdout = "162.237.73.13\n"
        return out

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    ip = m.probe_socks(socks_port=1080, timeout_s=8)
    assert ip == "162.237.73.13"


def test_probe_socks_returns_none_on_curl_failure(monkeypatch):
    m = _import_module()

    def fake_run(cmd, **kwargs):
        out = MagicMock()
        out.returncode = 28  # curl timeout exit code
        out.stdout = ""
        return out

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m.probe_socks() is None


def test_probe_socks_returns_none_when_response_isnt_ip(monkeypatch):
    """Belt-and-suspenders: if curl returns 0 but the body isn't an
    IP (e.g., HTML error page), treat it as unhealthy."""
    m = _import_module()

    def fake_run(cmd, **kwargs):
        out = MagicMock()
        out.returncode = 0
        out.stdout = "<html>error</html>"
        return out

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m.probe_socks() is None


# ─── End-to-end main() with full stubbing ────────────────────────────


def test_main_emits_ok_envelope_when_healthy(tmp_path, monkeypatch, capsys):
    m = _import_module()

    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(m, "probe_socks", lambda **kw: "162.237.73.13")
    monkeypatch.setattr(m, "restart_tunnel", lambda: pytest.fail("should not restart"))

    rc = m.main([])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    env = json.loads(out)
    assert env["status"] == "ok"


def test_main_restarts_on_third_failure(tmp_path, monkeypatch, capsys):
    m = _import_module()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")

    # Pre-seed two prior failures
    m.save_state(m.STATE_FILE, {
        "consecutive_failures": 2,
        "last_restart_ts": None,
    })

    monkeypatch.setattr(m, "probe_socks", lambda **kw: None)

    restarted = {"v": False}
    def fake_restart():
        restarted["v"] = True
    monkeypatch.setattr(m, "restart_tunnel", fake_restart)

    rc = m.main([])
    assert rc == 0
    assert restarted["v"] is True

    out = capsys.readouterr().out.strip().splitlines()[-1]
    env = json.loads(out)
    assert env["status"] == "error"
    assert env["alert"]


def test_main_resets_failure_counter_on_recovery(tmp_path, monkeypatch, capsys):
    m = _import_module()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")

    # Seed with 2 failures
    m.save_state(m.STATE_FILE, {
        "consecutive_failures": 2,
        "last_restart_ts": None,
    })

    monkeypatch.setattr(m, "probe_socks", lambda **kw: "162.237.73.13")

    rc = m.main([])
    assert rc == 0

    state = m.load_state(m.STATE_FILE)
    assert state["consecutive_failures"] == 0
