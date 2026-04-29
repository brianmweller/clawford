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
import time
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


def test_decide_action_none_when_probe_healthy():
    m = _import_module()
    d = m.decide(
        probe_ok=True,
        last_restart_age_s=10_000,
        prior_failures=0,
        restarts_in_outage=0,
        already_alerted=False,
    )
    assert d.action == "none"


def test_decide_recovery_when_probe_healthy_after_alerted_outage():
    """Probe ok after we already paged about an outage = send recovery."""
    m = _import_module()
    d = m.decide(
        probe_ok=True,
        last_restart_age_s=600,
        prior_failures=0,
        restarts_in_outage=1,
        already_alerted=True,
    )
    assert d.action == "recovery"


def test_decide_action_none_on_first_failure():
    """One transient failure shouldn't trigger a restart — that'd
    thrash the daemon during normal Tailscale path-renegotiation."""
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=10_000,
        prior_failures=0,
        restarts_in_outage=0,
        already_alerted=False,
    )
    assert d.action == "none"


def test_decide_action_none_on_second_failure():
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=10_000,
        prior_failures=1,
        restarts_in_outage=0,
        already_alerted=False,
    )
    assert d.action == "none"


def test_decide_restart_after_third_consecutive_failure():
    """Three in a row at 5-min cadence = ~15 min outage. First restart
    of the outage → alert."""
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=10_000,
        prior_failures=2,
        restarts_in_outage=0,
        already_alerted=False,
    )
    assert d.action == "restart"


def test_decide_silent_restart_on_subsequent_outage_tick():
    """When the outage has already paged a restart, don't re-alert on
    the next 3-failure cycle. Restart still happens (might help), but
    silently — fleet-health is the surface for sustained outages."""
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=600,
        prior_failures=2,
        restarts_in_outage=1,
        already_alerted=True,
    )
    assert d.action == "restart_silent"


def test_decide_escalate_to_tailscaled_after_three_failed_restarts():
    """If the autossh restart didn't fix it after 3 attempts, escalate
    to also restarting tailscaled. Today's outage (2026-04-29 morning)
    needed exactly this — autossh restart was a no-op; only tailscaled
    restart cleared the stuck path."""
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=600,
        prior_failures=2,
        restarts_in_outage=3,
        already_alerted=True,
    )
    assert d.action == "escalate_tailscaled"


def test_decide_no_more_restarts_after_escalation_silent_until_recovery():
    """After the tailscaled escalation, give up auto-actions and stay
    silent. the operator's been notified; further automation makes things
    worse, not better."""
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=600,
        prior_failures=2,
        restarts_in_outage=4,  # already escalated
        already_alerted=True,
    )
    assert d.action == "none"


def test_decide_throttled_when_recently_restarted():
    """Still don't thrash. 5-min minimum between any restart actions."""
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=60,
        prior_failures=10,
        restarts_in_outage=0,
        already_alerted=False,
    )
    assert d.action == "none"
    assert "throttle" in d.reason.lower() or "recently" in d.reason.lower()


def test_decide_eligible_after_throttle_window_clears():
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=360,
        prior_failures=2,
        restarts_in_outage=0,
        already_alerted=False,
    )
    assert d.action == "restart"


def test_decide_can_restart_when_never_restarted_and_unhealthy():
    m = _import_module()
    d = m.decide(
        probe_ok=False,
        last_restart_age_s=None,
        prior_failures=2,
        restarts_in_outage=0,
        already_alerted=False,
    )
    assert d.action == "restart"


# ─── Envelope shape ──────────────────────────────────────────────────


def test_envelope_silent_when_healthy_and_no_action():
    m = _import_module()
    env = m.build_envelope(
        probe_ok=True,
        action="none",
        prior_failures=0,
        residential_ip="162.237.73.13",
    )
    assert env["status"] == "ok"
    assert "alert" not in env or not env.get("alert")


def test_envelope_alerts_on_first_restart_of_outage():
    """First restart of an outage pages the operator — signals the watchdog
    has started intervening."""
    m = _import_module()
    env = m.build_envelope(
        probe_ok=False,
        action="restart",
        prior_failures=3,
        residential_ip=None,
    )
    assert env["status"] == "error"
    assert env["alert"]
    assert "tunnel" in env["alert"].lower()
    assert "restart" in env["alert"].lower()


def test_envelope_silent_on_subsequent_restart_in_same_outage():
    """Don't spam every 15 min during a sustained outage — the operator got
    five identical pages this morning from the same outage. The fleet-
    health alert is the user-facing surface for sustained issues; the
    watchdog only pages on state-change moments."""
    m = _import_module()
    env = m.build_envelope(
        probe_ok=False,
        action="restart_silent",
        prior_failures=3,
        residential_ip=None,
    )
    assert env["status"] == "error"
    assert not env.get("alert")


def test_envelope_alerts_on_escalation_to_tailscaled_restart():
    """After N ineffective tunnel restarts, the watchdog tries kicking
    Tailscale instead. That's a higher-blast-radius action — page so
    the operator knows."""
    m = _import_module()
    env = m.build_envelope(
        probe_ok=False,
        action="escalate_tailscaled",
        prior_failures=3,
        residential_ip=None,
    )
    assert env["status"] == "error"
    assert env["alert"]
    assert "tailscale" in env["alert"].lower()


def test_envelope_alerts_on_recovery():
    """When the tunnel comes back after a paged outage, send a recovery
    notice so the operator knows the issue resolved."""
    m = _import_module()
    env = m.build_envelope(
        probe_ok=True,
        action="recovery",
        prior_failures=0,
        residential_ip="162.237.73.13",
    )
    assert env["status"] == "ok"
    assert env["alert"]
    assert "recover" in env["alert"].lower() or "restored" in env["alert"].lower()


def test_envelope_silent_on_routine_health_tick():
    """Healthy tick after no prior outage — silent."""
    m = _import_module()
    env = m.build_envelope(
        probe_ok=True,
        action="none",
        prior_failures=0,
        residential_ip="162.237.73.13",
    )
    assert env["status"] == "ok"
    assert not env.get("alert")


def test_envelope_silent_when_unhealthy_below_threshold():
    """First failures, no restart yet — silent."""
    m = _import_module()
    env = m.build_envelope(
        probe_ok=False,
        action="none",
        prior_failures=1,
        residential_ip=None,
    )
    assert env["status"] == "error"
    assert not env.get("alert")


# ─── State persistence (consecutive-failures counter) ────────────────


def test_state_load_handles_missing_file(tmp_path):
    m = _import_module()
    state = m.load_state(tmp_path / "nope.json")
    assert state["consecutive_failures"] == 0
    assert state["last_restart_ts"] is None
    assert state["restarts_in_outage"] == 0
    assert state["already_alerted"] is False


def test_state_save_round_trips(tmp_path):
    m = _import_module()
    p = tmp_path / "state.json"
    m.save_state(p, {
        "consecutive_failures": 7,
        "last_restart_ts": 1700000000.0,
        "restarts_in_outage": 2,
        "already_alerted": True,
    })
    state = m.load_state(p)
    assert state["consecutive_failures"] == 7
    assert state["last_restart_ts"] == 1700000000.0
    assert state["restarts_in_outage"] == 2
    assert state["already_alerted"] is True


def test_state_load_handles_corrupt_json(tmp_path):
    m = _import_module()
    p = tmp_path / "state.json"
    p.write_text("not-json", encoding="utf-8")
    state = m.load_state(p)
    assert state["consecutive_failures"] == 0
    assert state["restarts_in_outage"] == 0
    assert state["already_alerted"] is False


def test_state_load_back_compat_with_old_two_field_state(tmp_path):
    """Existing state files on the VPS only have consecutive_failures
    and last_restart_ts. Don't crash on the migration."""
    m = _import_module()
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "consecutive_failures": 5,
        "last_restart_ts": 1700000000.0,
    }), encoding="utf-8")
    state = m.load_state(p)
    assert state["consecutive_failures"] == 5
    assert state["last_restart_ts"] == 1700000000.0
    assert state["restarts_in_outage"] == 0
    assert state["already_alerted"] is False


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


def _stub_restart_calls(monkeypatch, m):
    calls = {"tunnel": 0, "tailscaled": 0}
    monkeypatch.setattr(m, "restart_tunnel", lambda: calls.__setitem__("tunnel", calls["tunnel"] + 1))
    monkeypatch.setattr(m, "restart_tailscaled", lambda: calls.__setitem__("tailscaled", calls["tailscaled"] + 1))
    return calls


def test_main_emits_ok_envelope_when_healthy(tmp_path, monkeypatch, capsys):
    m = _import_module()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(m, "probe_socks", lambda **kw: "162.237.73.13")
    calls = _stub_restart_calls(monkeypatch, m)

    rc = m.main([])
    assert rc == 0
    assert calls["tunnel"] == 0 and calls["tailscaled"] == 0

    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env["status"] == "ok"
    assert "alert" not in env or not env["alert"]


def test_main_restarts_with_alert_on_first_outage_threshold(tmp_path, monkeypatch, capsys):
    m = _import_module()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    m.save_state(m.STATE_FILE, {
        "consecutive_failures": 2,
        "last_restart_ts": None,
        "restarts_in_outage": 0,
        "already_alerted": False,
    })
    monkeypatch.setattr(m, "probe_socks", lambda **kw: None)
    calls = _stub_restart_calls(monkeypatch, m)

    rc = m.main([])
    assert rc == 0
    assert calls["tunnel"] == 1
    assert calls["tailscaled"] == 0

    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env["status"] == "error"
    assert env["alert"]
    state = m.load_state(m.STATE_FILE)
    assert state["already_alerted"] is True
    assert state["restarts_in_outage"] == 1


def test_main_silent_restart_on_subsequent_threshold_in_same_outage(tmp_path, monkeypatch, capsys):
    """The spam-fix: second threshold-hit in the same outage restarts
    silently. the operator was woken up 5 times this morning; one is enough."""
    m = _import_module()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    m.save_state(m.STATE_FILE, {
        "consecutive_failures": 2,
        "last_restart_ts": time.time() - 600,  # 10 min ago, throttle clear
        "restarts_in_outage": 1,
        "already_alerted": True,
    })
    monkeypatch.setattr(m, "probe_socks", lambda **kw: None)
    calls = _stub_restart_calls(monkeypatch, m)

    rc = m.main([])
    assert rc == 0
    assert calls["tunnel"] == 1, "should still restart, just silently"

    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env["status"] == "error"
    assert not env.get("alert"), "no Telegram noise on subsequent restart"


def test_main_escalates_to_tailscaled_after_three_failed_restarts(tmp_path, monkeypatch, capsys):
    m = _import_module()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    m.save_state(m.STATE_FILE, {
        "consecutive_failures": 2,
        "last_restart_ts": time.time() - 600,
        "restarts_in_outage": 3,
        "already_alerted": True,
    })
    monkeypatch.setattr(m, "probe_socks", lambda **kw: None)
    calls = _stub_restart_calls(monkeypatch, m)

    rc = m.main([])
    assert rc == 0
    assert calls["tailscaled"] == 1
    assert calls["tunnel"] == 1, "kick autossh after kicking tailscaled"

    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env["status"] == "error"
    assert env["alert"]
    assert "tailscale" in env["alert"].lower()


def test_main_emits_recovery_alert_after_paged_outage(tmp_path, monkeypatch, capsys):
    m = _import_module()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    m.save_state(m.STATE_FILE, {
        "consecutive_failures": 0,
        "last_restart_ts": time.time() - 600,
        "restarts_in_outage": 1,
        "already_alerted": True,
    })
    monkeypatch.setattr(m, "probe_socks", lambda **kw: "162.237.73.13")
    calls = _stub_restart_calls(monkeypatch, m)

    rc = m.main([])
    assert rc == 0
    assert calls["tunnel"] == 0 and calls["tailscaled"] == 0

    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env["status"] == "ok"
    assert env["alert"]
    assert "recover" in env["alert"].lower() or "restored" in env["alert"].lower()

    # Recovery resets the per-outage state
    state = m.load_state(m.STATE_FILE)
    assert state["already_alerted"] is False
    assert state["restarts_in_outage"] == 0
    assert state["consecutive_failures"] == 0


def test_main_silent_recovery_when_no_alert_was_sent(tmp_path, monkeypatch, capsys):
    """Probe ok after a non-paged blip — silent. Don't say 'recovered'
    if we never said 'down' in the first place."""
    m = _import_module()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    m.save_state(m.STATE_FILE, {
        "consecutive_failures": 1,
        "last_restart_ts": None,
        "restarts_in_outage": 0,
        "already_alerted": False,
    })
    monkeypatch.setattr(m, "probe_socks", lambda **kw: "162.237.73.13")
    _stub_restart_calls(monkeypatch, m)

    rc = m.main([])
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env["status"] == "ok"
    assert not env.get("alert")
