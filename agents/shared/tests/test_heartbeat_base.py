"""Tests for agents/shared/heartbeat_base.py.

HeartbeatProbe is a thin SCRIPT_CONTRACT wrapper around each agent's
`probe()` function. It owns nothing but `main()` — the fleet-health
orchestrator (`ops/scripts/probe-agent.py`) is what actually invokes
`probe()` in production, and fleet-health.json is the single
authoritative health source (per-agent `<id>.status.md` writes were
retired).

These tests exercise the base class via tiny in-file subclasses — the
per-agent subclasses have their own tests asserting their specific
probe content.
"""
from __future__ import annotations

import json

import pytest

from agents.shared.heartbeat_base import HeartbeatProbe


# ─── Fixtures ────────────────────────────────────────────────────────


class _OkProbe(HeartbeatProbe):
    AGENT_ID = "dummy-ok"
    TITLE = "Dummy OK"
    EMOJI = "\u2705"

    def probe(self) -> dict:
        return {
            "status": "ok",
            "last_cron_run": "2026-04-14 12:00 UTC",
            "last_cron_name": "heartbeat",
            "last_cron_result": "all green",
            "error_log": "none",
        }


class _DegradedProbe(HeartbeatProbe):
    AGENT_ID = "dummy-degraded"
    TITLE = "Dummy Degraded"
    EMOJI = "\U0001f41b"

    def probe(self) -> dict:
        return {
            "status": "degraded",
            "missing_files": ["config.json"],
            "alert": "\U0001f41b dummy degraded: missing config.json",
        }


class _CrashingProbe(HeartbeatProbe):
    AGENT_ID = "dummy-crash"
    TITLE = "Dummy Crash"
    EMOJI = "\U0001f4a5"

    def probe(self) -> dict:
        raise RuntimeError("probe exploded")


# ─── Base class contract ─────────────────────────────────────────────


def test_base_probe_raises_not_implemented():
    """The base class's probe() must be overridden."""
    probe = HeartbeatProbe()
    probe.AGENT_ID = "abstract"
    with pytest.raises(NotImplementedError):
        probe.probe()


def test_base_class_carries_no_write_surface():
    """Regression guard: post-R6 the base class holds no status-file
    write path. run(), _write_status_md, render_status_md, and
    output_file were retired with fleet-health.json becoming the
    authoritative source of per-agent health."""
    for attr in ("run", "_write_status_md", "render_status_md", "output_file"):
        assert not hasattr(HeartbeatProbe, attr), (
            f"HeartbeatProbe must not expose {attr!r} post-R6 cleanup"
        )


# ─── main() wrapper ─────────────────────────────────────────────────


def test_main_returns_zero_on_happy_path(capsys):
    probe = _OkProbe()
    rc = probe.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    payload = json.loads(out)
    assert payload["status"] == "ok"


def test_main_catches_probe_crash_and_returns_zero(capsys):
    """A probe that raises must not break main() — the SCRIPT_CONTRACT
    requires exit 0 always, and an error JSON on stdout."""
    probe = _CrashingProbe()
    rc = probe.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "probe exploded" in payload["error"]
    assert "\U0001f4a5" in payload["alert"]
    assert "dummy-crash" in payload["alert"]
    assert "traceback" in payload
    assert isinstance(payload["traceback"], list)
    assert len(payload["traceback"]) <= 3


def test_main_prints_degraded_json_with_alert(capsys):
    probe = _DegradedProbe()
    rc = probe.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "degraded"
    assert "alert" in payload
    assert "config.json" in payload["alert"]


def test_main_output_is_single_line_json(capsys):
    """SCRIPT_CONTRACT requires the final non-blank line of stdout to
    be a JSON object."""
    probe = _OkProbe()
    probe.main()
    out = capsys.readouterr().out
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] in ("ok", "degraded", "error")
