"""Tests for agents/shared/heartbeat_base.py.

The HeartbeatProbe base class consolidates the scaffolding that every
per-agent heartbeat.py shares: pure `probe()` → dict, `run()` wrapping
probe with an atomic status.md write, and `main()` that always exits 0
and prints exactly one JSON line.

These tests exercise the base class via tiny in-file subclasses — the
per-agent subclasses (news-digest, connector, …) have their own tests
asserting their specific probe content.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.shared.heartbeat_base import HeartbeatProbe


# ─── Fixtures ────────────────────────────────────────────────────────


class _OkProbe(HeartbeatProbe):
    AGENT_ID = "dummy-ok"
    TITLE = "Dummy OK"
    EMOJI = "✅"

    def probe(self) -> dict:
        return {
            "status": "ok",
            "last_cron_run": "2026-04-14 12:00 UTC",
            "last_cron_name": "heartbeat",
            "last_cron_result": "all green",
            "error_log": "none",
        }

    def render_status_md(self, result: dict) -> str:
        return (
            f"# {self.TITLE} — Status\n\n"
            f"- **status:** {result['status']}\n"
            f"- **last_cron_run:** {result['last_cron_run']}\n"
        )


class _DegradedProbe(HeartbeatProbe):
    AGENT_ID = "dummy-degraded"
    TITLE = "Dummy Degraded"
    EMOJI = "🐛"

    def probe(self) -> dict:
        return {
            "status": "degraded",
            "missing_files": ["config.json"],
            "alert": "🐛 dummy degraded: missing config.json",
        }

    def render_status_md(self, result: dict) -> str:
        return f"# {self.TITLE} — Status\n\n- **status:** {result['status']}\n"


class _CrashingProbe(HeartbeatProbe):
    AGENT_ID = "dummy-crash"
    TITLE = "Dummy Crash"
    EMOJI = "💥"

    def probe(self) -> dict:
        raise RuntimeError("probe exploded")

    def render_status_md(self, result: dict) -> str:  # pragma: no cover
        return "unused"


@pytest.fixture
def brain_dir(tmp_path: Path) -> Path:
    """Isolated brain dir for status.md writes."""
    brain = tmp_path / "openclaw-backup"
    brain.mkdir()
    return brain


# ─── Base class contract ─────────────────────────────────────────────


def test_base_probe_raises_not_implemented():
    """The base class's probe() must be overridden."""
    probe = HeartbeatProbe()
    probe.AGENT_ID = "abstract"
    with pytest.raises(NotImplementedError):
        probe.probe()


def test_base_render_status_md_raises_not_implemented():
    probe = HeartbeatProbe()
    probe.AGENT_ID = "abstract"
    with pytest.raises(NotImplementedError):
        probe.render_status_md({"status": "ok"})


def test_output_file_path_derives_from_agent_id_and_brain_dir(brain_dir: Path):
    probe = _OkProbe(brain_dir=str(brain_dir))
    expected = str(brain_dir / "agents" / "dummy-ok.status.md")
    assert probe.output_file == expected


# ─── run() ──────────────────────────────────────────────────────────


def test_run_writes_status_md_atomically(brain_dir: Path):
    probe = _OkProbe(brain_dir=str(brain_dir))
    result = probe.run()

    assert result["status"] == "ok"
    status_file = brain_dir / "agents" / "dummy-ok.status.md"
    assert status_file.exists()
    text = status_file.read_text(encoding="utf-8")
    assert "# Dummy OK — Status" in text
    assert "- **status:** ok" in text

    # No leftover .tmp file
    tmp_file = brain_dir / "agents" / "dummy-ok.status.md.tmp"
    assert not tmp_file.exists()


def test_run_creates_parent_dir_if_missing(tmp_path: Path):
    # brain_dir does NOT have agents/ subdir yet
    brain = tmp_path / "brain-fresh"
    brain.mkdir()
    probe = _OkProbe(brain_dir=str(brain))
    probe.run()
    assert (brain / "agents" / "dummy-ok.status.md").exists()


def test_run_returns_probe_result_unchanged(brain_dir: Path):
    probe = _DegradedProbe(brain_dir=str(brain_dir))
    result = probe.run()
    assert result["status"] == "degraded"
    assert result["missing_files"] == ["config.json"]
    assert "alert" in result


def test_run_atomic_write_replaces_existing_file(brain_dir: Path):
    """Writing over an existing status.md should leave the new content
    only — no partial mix from a non-atomic write."""
    agents_dir = brain_dir / "agents"
    agents_dir.mkdir()
    existing = agents_dir / "dummy-ok.status.md"
    existing.write_text("stale content from previous run\n")

    probe = _OkProbe(brain_dir=str(brain_dir))
    probe.run()

    text = existing.read_text(encoding="utf-8")
    assert "stale content" not in text
    assert "# Dummy OK — Status" in text


# ─── main() wrapper ─────────────────────────────────────────────────


def test_main_returns_zero_on_happy_path(brain_dir: Path, capsys):
    probe = _OkProbe(brain_dir=str(brain_dir))
    rc = probe.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # Exactly one line of JSON
    assert out.count("\n") == 0
    payload = json.loads(out)
    assert payload["status"] == "ok"


def test_main_catches_probe_crash_and_returns_zero(brain_dir: Path, capsys):
    """A probe that raises must not break main() — the SCRIPT_CONTRACT
    requires exit 0 always, and an error JSON on stdout."""
    probe = _CrashingProbe(brain_dir=str(brain_dir))
    rc = probe.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "probe exploded" in payload["error"]
    assert "💥" in payload["alert"]
    assert "dummy-crash" in payload["alert"]
    assert "traceback" in payload
    # Traceback is limited to last few lines so the JSON stays small
    assert isinstance(payload["traceback"], list)
    assert len(payload["traceback"]) <= 3


def test_main_catches_status_md_write_failure(brain_dir: Path, capsys, monkeypatch):
    """If the status.md write fails (e.g. permission denied), main()
    should still emit JSON and exit 0, not crash."""
    probe = _OkProbe(brain_dir=str(brain_dir))

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(probe, "_write_status_md", boom)

    rc = probe.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "disk full" in payload["error"]


def test_main_prints_degraded_json_with_alert(brain_dir: Path, capsys):
    probe = _DegradedProbe(brain_dir=str(brain_dir))
    rc = probe.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "degraded"
    assert "alert" in payload
    assert "config.json" in payload["alert"]


# ─── SCRIPT_CONTRACT conformance ────────────────────────────────────


def test_main_output_is_single_line_json(brain_dir: Path, capsys):
    """The SCRIPT_CONTRACT requires the final non-blank line of stdout
    to be a JSON object. Emitting multiple lines is discouraged."""
    probe = _OkProbe(brain_dir=str(brain_dir))
    probe.main()
    out = capsys.readouterr().out
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert len(lines) == 1
    # The one line parses as JSON with a status
    payload = json.loads(lines[0])
    assert payload["status"] in ("ok", "degraded", "error")
