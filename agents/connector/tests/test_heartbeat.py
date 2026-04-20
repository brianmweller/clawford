"""Tests for connector/scripts/heartbeat.py.

Deterministic probe: verify required workspace files exist, prune
stale cache files (>14 days) and stale triage entries (>48h), return
SCRIPT_CONTRACT JSON.

`probe()` is pure — reads state, returns dict. The fleet-health.py
orchestrator invokes it via probe-agent.py and aggregates into
<brain>/fleet-health.json; per-agent <id>.status.md writes were
retired along with `run()` / `_write_status_md()`.

Run: cd agents/connector && python3 -m pytest tests/test_heartbeat.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    """Import a hyphenated or underscored script as a fresh module."""
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"conn_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def stub_workspace(tmp_path, monkeypatch):
    """Create a fake connector workspace + brain layout and point the
    heartbeat module at it."""
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir()
    (workspace / "cache").mkdir()

    # Write both required files by default — individual tests can
    # unlink one to simulate missing config.
    (workspace / "connector-config.json").write_text(json.dumps({"agent": "huckle"}))
    (workspace / "pending-triage.json").write_text(json.dumps([]))

    # Re-import heartbeat under the stubbed paths. We need to rewrite
    # WORKSPACE at the module level so any os.path.join results it
    # computed at import time are regenerated.
    hb = _load_script("heartbeat.py")
    monkeypatch.setattr(hb, "WORKSPACE", str(workspace))
    monkeypatch.setattr(hb, "CONFIG_FILE", str(workspace / "connector-config.json"))
    monkeypatch.setattr(hb, "TRIAGE_FILE", str(workspace / "pending-triage.json"))
    monkeypatch.setattr(hb, "CACHE_DIR", str(workspace / "cache"))

    return types.SimpleNamespace(
        hb=hb,
        workspace=workspace,
        config_file=workspace / "connector-config.json",
        triage_file=workspace / "pending-triage.json",
        cache_dir=workspace / "cache",
    )


# ─── probe() — happy path ────────────────────────────────────────────


def test_probe_returns_ok_when_all_files_present(stub_workspace):
    result = stub_workspace.hb.probe()
    assert result["status"] == "ok"
    assert result["missing_files"] == []


def test_probe_returns_degraded_when_config_missing(stub_workspace):
    stub_workspace.config_file.unlink()
    result = stub_workspace.hb.probe()
    assert result["status"] == "degraded"
    assert "connector-config.json" in result["missing_files"]
    assert "alert" in result


def test_probe_returns_degraded_when_triage_missing(stub_workspace):
    stub_workspace.triage_file.unlink()
    result = stub_workspace.hb.probe()
    assert result["status"] == "degraded"
    assert "pending-triage.json" in result["missing_files"]


# ─── triage prune ────────────────────────────────────────────────────


def test_probe_prunes_stale_triage_entries(stub_workspace):
    """pending-triage.json entries with created_at > 48h old should be
    removed. Fresh entries (< 48h) stay."""
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=12)).isoformat()
    stale = (now - timedelta(hours=72)).isoformat()
    entries = [
        {"id": "a", "created_at": fresh, "text": "fresh"},
        {"id": "b", "created_at": stale, "text": "stale"},
        {"id": "c", "created_at": fresh, "text": "also fresh"},
    ]
    stub_workspace.triage_file.write_text(json.dumps(entries))

    result = stub_workspace.hb.probe()

    assert result["pruned_triage"] == 1
    remaining = json.loads(stub_workspace.triage_file.read_text())
    remaining_ids = [e["id"] for e in remaining]
    assert "a" in remaining_ids
    assert "c" in remaining_ids
    assert "b" not in remaining_ids


def test_probe_keeps_fresh_triage_entries(stub_workspace):
    now = datetime.now(timezone.utc)
    fresh_entries = [
        {"id": str(i), "created_at": (now - timedelta(hours=i)).isoformat()}
        for i in range(5)
    ]
    stub_workspace.triage_file.write_text(json.dumps(fresh_entries))

    result = stub_workspace.hb.probe()

    assert result["pruned_triage"] == 0
    remaining = json.loads(stub_workspace.triage_file.read_text())
    assert len(remaining) == 5


def test_probe_tolerates_non_list_triage(stub_workspace):
    """If pending-triage.json contains a dict or other non-list, the
    prune must not crash — it just leaves the file alone."""
    stub_workspace.triage_file.write_text(json.dumps({"legacy": "format"}))
    result = stub_workspace.hb.probe()
    assert result["status"] == "ok"
    assert result["pruned_triage"] == 0


# ─── cache prune ─────────────────────────────────────────────────────


def test_probe_prunes_stale_cache_files(stub_workspace):
    """Files in cache/ older than 14 days should be unlinked."""
    fresh = stub_workspace.cache_dir / "fresh.json"
    fresh.write_text("{}")
    stale = stub_workspace.cache_dir / "stale.json"
    stale.write_text("{}")
    # Backdate stale file to 20 days ago
    old_ts = time.time() - (20 * 86400)
    os.utime(str(stale), (old_ts, old_ts))

    result = stub_workspace.hb.probe()

    assert result["pruned_cache"] == 1
    assert fresh.exists()
    assert not stale.exists()


def test_probe_pruned_cache_zero_when_all_fresh(stub_workspace):
    (stub_workspace.cache_dir / "one.json").write_text("{}")
    (stub_workspace.cache_dir / "two.txt").write_text("hi")
    result = stub_workspace.hb.probe()
    assert result["pruned_cache"] == 0


# ─── main() SCRIPT_CONTRACT wrapper ──────────────────────────────────


def test_main_prints_one_json_line_and_exits_zero(stub_workspace, capsys):
    rc = stub_workspace.hb.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # Must be exactly one JSON line
    assert out.count("\n") == 0
    payload = json.loads(out)
    assert payload["status"] in ("ok", "degraded", "error")


def test_main_emits_error_json_when_probe_crashes(stub_workspace, capsys, monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(stub_workspace.hb, "probe", boom)

    rc = stub_workspace.hb.main()
    assert rc == 0  # SCRIPT_CONTRACT: always exit 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "alert" in payload


# ─── gmail push listener probe ───────────────────────────────────────


def test_probe_omits_gmail_push_when_watch_state_absent(stub_workspace):
    """No watch-state file = listener not configured = no push block."""
    result = stub_workspace.hb.probe()
    assert "gmail_push" not in result


def test_probe_reports_fresh_watch_state(stub_workspace, monkeypatch):
    watch_state = stub_workspace.cache_dir / "gmail-watch-state.json"
    far_future_ms = int(time.time() * 1000) + 6 * 86400_000  # 6 days
    watch_state.write_text(json.dumps({
        "history_id": "42",
        "expiration_ms": far_future_ms,
        "topic": "projects/p/topics/t",
    }))
    monkeypatch.setattr(stub_workspace.hb, "WATCH_STATE_FILE", str(watch_state))
    # Stub systemctl to say "active" so we don't get a listener alert
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: types.SimpleNamespace(
        stdout="active\n", stderr="", returncode=0,
    ))

    result = stub_workspace.hb.probe()
    assert result["status"] == "ok"
    assert "gmail_push" in result
    assert result["gmail_push"]["listener_service"] == "active"
    assert result["gmail_push"]["watch_expires_in_hours"] > 24


def test_probe_alerts_when_watch_expires_soon(stub_workspace, monkeypatch):
    watch_state = stub_workspace.cache_dir / "gmail-watch-state.json"
    soon_ms = int(time.time() * 1000) + 12 * 3600_000  # 12h from now
    watch_state.write_text(json.dumps({
        "history_id": "42", "expiration_ms": soon_ms, "topic": "t",
    }))
    monkeypatch.setattr(stub_workspace.hb, "WATCH_STATE_FILE", str(watch_state))
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: types.SimpleNamespace(
        stdout="active\n", stderr="", returncode=0,
    ))
    result = stub_workspace.hb.probe()
    assert result["gmail_push"]["watch_state"] == "expiring_soon"


def test_probe_alerts_when_listener_inactive(stub_workspace, monkeypatch):
    watch_state = stub_workspace.cache_dir / "gmail-watch-state.json"
    far_future_ms = int(time.time() * 1000) + 6 * 86400_000
    watch_state.write_text(json.dumps({
        "history_id": "1", "expiration_ms": far_future_ms, "topic": "t",
    }))
    monkeypatch.setattr(stub_workspace.hb, "WATCH_STATE_FILE", str(watch_state))
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: types.SimpleNamespace(
        stdout="failed\n", stderr="", returncode=3,
    ))
    result = stub_workspace.hb.probe()
    assert "alert" in result
    assert "listener" in result["alert"].lower()
    # missing_files still empty so top-level status remains "ok"
    assert result["status"] == "ok"


def test_probe_tolerates_missing_systemctl(stub_workspace, monkeypatch):
    watch_state = stub_workspace.cache_dir / "gmail-watch-state.json"
    far_future_ms = int(time.time() * 1000) + 6 * 86400_000
    watch_state.write_text(json.dumps({
        "history_id": "1", "expiration_ms": far_future_ms, "topic": "t",
    }))
    monkeypatch.setattr(stub_workspace.hb, "WATCH_STATE_FILE", str(watch_state))
    import subprocess

    def raise_not_found(*_a, **_kw):
        raise FileNotFoundError("no systemctl here")
    monkeypatch.setattr(subprocess, "run", raise_not_found)
    result = stub_workspace.hb.probe()
    # Dev-machine heartbeat must still succeed
    assert result["status"] == "ok"
    assert result["gmail_push"]["listener_service"] == "unavailable"
