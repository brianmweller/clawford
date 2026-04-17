"""Tests for news-digest/scripts/heartbeat.py.

Verifies the NewsDigestProbe subclass of HeartbeatProbe: persistent
LinkedIn profile dir, preferences/model.json, ranked-<today>.json
freshness (gated by time-of-day — missing is only degraded after
11:00 UTC when morning-edition should have written it), and the
SCRIPT_CONTRACT main() wrapper.

The fleet-health.py orchestrator invokes probe() via probe-agent.py
and writes the aggregated result to <brain>/fleet-health.json; per-agent
status.md writes were retired.

Run: cd agents/news-digest && python3 -m pytest tests/test_heartbeat.py -v
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"news_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def probe_setup(tmp_path):
    workspace = tmp_path / "news-digest-workspace"
    workspace.mkdir()
    (workspace / "cache").mkdir()
    (workspace / "preferences").mkdir()
    (workspace / "linkedin-profile").mkdir()  # persistent Chromium profile dir

    # Required state files
    (workspace / "preferences" / "model.json").write_text(json.dumps({
        "topics": {},
        "sources": {},
    }))

    hb = _load_script("heartbeat.py")
    instance = hb.NewsDigestProbe(workspace=str(workspace))
    return {
        "hb": hb,
        "instance": instance,
        "workspace": workspace,
        "profile_dir": workspace / "linkedin-profile",
        "model_file": workspace / "preferences" / "model.json",
        "cache_dir": workspace / "cache",
    }


def _write_ranked_today(cache_dir: Path, num_items: int = 15) -> Path:
    """Create a ranked-<today>.json file matching morning-edition output."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = cache_dir / f"ranked-{today}.json"
    path.write_text(json.dumps({"items": [{"id": i} for i in range(num_items)]}))
    return path


# ─── probe() — happy path ────────────────────────────────────────────


def test_probe_returns_ok_when_profile_model_and_ranked_present(probe_setup):
    _write_ranked_today(probe_setup["cache_dir"], num_items=18)
    result = probe_setup["instance"].probe()
    assert result["status"] == "ok"
    assert result["linkedin_profile"] == "ok"
    assert result["morning_edition"] in ("ok", "pending")


def test_probe_returns_degraded_when_linkedin_profile_missing(probe_setup):
    import shutil
    shutil.rmtree(probe_setup["profile_dir"])
    result = probe_setup["instance"].probe()
    assert result["status"] == "degraded"
    assert result["linkedin_profile"] == "missing"
    assert "alert" in result


def test_probe_returns_degraded_when_preferences_model_missing(probe_setup):
    probe_setup["model_file"].unlink()
    result = probe_setup["instance"].probe()
    assert result["status"] == "degraded"
    assert "preferences/model.json" in result["missing_files"]


# ─── morning_edition freshness (time-gated) ─────────────────────────


def test_probe_morning_edition_pending_before_11am_utc(probe_setup):
    """If current UTC time < 11:00 and ranked-today is missing, that's
    'pending' not 'missing' — morning-edition hasn't run yet."""
    fake_now = datetime.now(timezone.utc).replace(hour=10, minute=45, second=0, microsecond=0)
    with patch.object(probe_setup["hb"], "_utcnow", return_value=fake_now):
        result = probe_setup["instance"].probe()
    assert result["morning_edition"] == "pending"
    assert result["status"] == "ok"  # pending is not degraded


def test_probe_morning_edition_missing_after_11am_utc(probe_setup):
    """After 11:00 UTC, missing ranked-today means morning-edition
    did not produce its cache file — that's a real problem."""
    fake_now = datetime.now(timezone.utc).replace(hour=12, minute=30, second=0, microsecond=0)
    with patch.object(probe_setup["hb"], "_utcnow", return_value=fake_now):
        result = probe_setup["instance"].probe()
    assert result["morning_edition"] == "missing"
    assert result["status"] == "degraded"


def test_probe_morning_edition_ok_when_ranked_present_after_11am(probe_setup):
    _write_ranked_today(probe_setup["cache_dir"], num_items=20)
    fake_now = datetime.now(timezone.utc).replace(hour=12, minute=30, second=0, microsecond=0)
    with patch.object(probe_setup["hb"], "_utcnow", return_value=fake_now):
        result = probe_setup["instance"].probe()
    assert result["morning_edition"] == "ok"
    assert result["items_count"] == 20


# ─── main() wrapper ──────────────────────────────────────────────────


def test_main_prints_one_json_line_and_exits_zero(probe_setup, capsys):
    rc = probe_setup["instance"].main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    payload = json.loads(out)
    assert payload["status"] in ("ok", "degraded", "error")


def test_main_emits_error_json_when_probe_crashes(probe_setup, capsys, monkeypatch):
    def boom(self):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(
        probe_setup["hb"].NewsDigestProbe,
        "probe",
        boom,
    )
    rc = probe_setup["instance"].main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "alert" in payload
    assert "🐛" in payload["alert"]


# ─── Module-level probe() for fleet-health.py ───────────────────────


def test_module_level_probe_function_exists(probe_setup):
    """fleet-health.py / probe-agent.py calls `from heartbeat import probe;
    probe()`. That thin wrapper must exist."""
    hb = probe_setup["hb"]
    assert callable(hb.probe)
