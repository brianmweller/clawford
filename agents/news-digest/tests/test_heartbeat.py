"""Tests for news-digest/scripts/heartbeat.py.

Replaces the LLM-native heartbeat cron with a deterministic Python
probe: verify linkedin-profile/ directory exists (persistent auth
profile), verify preferences/model.json exists, check freshness of
today's ranked-<date>.json (gated by time-of-day — missing is only
degraded after 11:00 UTC when morning-edition should have written it),
write news-digest.status.md, emit SCRIPT_CONTRACT JSON.

Run: cd agents/news-digest && python3 -m pytest tests/test_heartbeat.py -v
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
def stub_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "news-digest-workspace"
    workspace.mkdir()
    (workspace / "cache").mkdir()
    (workspace / "preferences").mkdir()
    (workspace / "linkedin-profile").mkdir()  # persistent Chromium profile dir

    brain = tmp_path / "openclaw-backup"
    (brain / "agents").mkdir(parents=True)

    # Required state files
    (workspace / "preferences" / "model.json").write_text(json.dumps({
        "topics": {},
        "sources": {},
    }))

    hb = _load_script("heartbeat.py")
    monkeypatch.setattr(hb, "WORKSPACE", str(workspace))
    monkeypatch.setattr(hb, "BRAIN", str(brain))
    monkeypatch.setattr(hb, "OUTPUT_FILE", str(brain / "agents" / "news-digest.status.md"))
    monkeypatch.setattr(hb, "LINKEDIN_PROFILE_DIR", str(workspace / "linkedin-profile"))
    monkeypatch.setattr(hb, "PREFERENCES_MODEL", str(workspace / "preferences" / "model.json"))
    monkeypatch.setattr(hb, "CACHE_DIR", str(workspace / "cache"))

    return types.SimpleNamespace(
        hb=hb,
        workspace=workspace,
        brain=brain,
        profile_dir=workspace / "linkedin-profile",
        model_file=workspace / "preferences" / "model.json",
        cache_dir=workspace / "cache",
        status_file=brain / "agents" / "news-digest.status.md",
    )


def _write_ranked_today(cache_dir: Path, num_items: int = 15) -> Path:
    """Create a ranked-<today>.json file matching morning-edition output."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = cache_dir / f"ranked-{today}.json"
    path.write_text(json.dumps({"items": [{"id": i} for i in range(num_items)]}))
    return path


# ─── probe() — happy path ────────────────────────────────────────────


def test_probe_returns_ok_when_profile_model_and_ranked_present(stub_workspace):
    _write_ranked_today(stub_workspace.cache_dir, num_items=18)
    result = stub_workspace.hb.probe()
    assert result["status"] == "ok"
    assert result["linkedin_profile"] == "ok"
    assert result["morning_edition"] in ("ok", "pending")


def test_probe_returns_degraded_when_linkedin_profile_missing(stub_workspace):
    import shutil
    shutil.rmtree(stub_workspace.profile_dir)
    result = stub_workspace.hb.probe()
    assert result["status"] == "degraded"
    assert result["linkedin_profile"] == "missing"
    assert "alert" in result


def test_probe_returns_degraded_when_preferences_model_missing(stub_workspace):
    stub_workspace.model_file.unlink()
    result = stub_workspace.hb.probe()
    assert result["status"] == "degraded"
    assert "preferences/model.json" in result["missing_files"]


# ─── morning_edition freshness (time-gated) ─────────────────────────


def test_probe_morning_edition_pending_before_11am_utc(stub_workspace):
    """If current UTC time < 11:00 and ranked-today is missing, that's
    'pending' not 'missing' — morning-edition hasn't run yet."""
    # Force now to 10:45 UTC today
    fake_now = datetime.now(timezone.utc).replace(hour=10, minute=45, second=0, microsecond=0)
    with patch.object(stub_workspace.hb, "_utcnow", return_value=fake_now):
        result = stub_workspace.hb.probe()
    assert result["morning_edition"] == "pending"
    assert result["status"] == "ok"  # pending is not degraded


def test_probe_morning_edition_missing_after_11am_utc(stub_workspace):
    """After 11:00 UTC, missing ranked-today means morning-edition
    did not produce its cache file — that's a real problem."""
    fake_now = datetime.now(timezone.utc).replace(hour=12, minute=30, second=0, microsecond=0)
    with patch.object(stub_workspace.hb, "_utcnow", return_value=fake_now):
        result = stub_workspace.hb.probe()
    assert result["morning_edition"] == "missing"
    assert result["status"] == "degraded"


def test_probe_morning_edition_ok_when_ranked_present_after_11am(stub_workspace):
    _write_ranked_today(stub_workspace.cache_dir, num_items=20)
    fake_now = datetime.now(timezone.utc).replace(hour=12, minute=30, second=0, microsecond=0)
    with patch.object(stub_workspace.hb, "_utcnow", return_value=fake_now):
        result = stub_workspace.hb.probe()
    assert result["morning_edition"] == "ok"
    assert result["items_count"] == 20


# ─── status.md write ─────────────────────────────────────────────────


def test_run_writes_status_md_with_core_fields(stub_workspace):
    _write_ranked_today(stub_workspace.cache_dir, num_items=15)
    stub_workspace.hb.run()
    text = stub_workspace.status_file.read_text(encoding="utf-8")
    assert "# News Digest — Status" in text
    assert "- **last_heartbeat:**" in text
    assert "- **status:** ok" in text
    assert "- **linkedin_profile:** ok" in text
    assert "- **morning_edition:**" in text


def test_run_marks_degraded_when_profile_missing(stub_workspace):
    import shutil
    shutil.rmtree(stub_workspace.profile_dir)
    stub_workspace.hb.run()
    text = stub_workspace.status_file.read_text(encoding="utf-8")
    assert "- **status:** degraded" in text
    assert "- **linkedin_profile:** missing" in text


# ─── main() wrapper ──────────────────────────────────────────────────


def test_main_prints_one_json_line_and_exits_zero(stub_workspace, capsys):
    rc = stub_workspace.hb.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    payload = json.loads(out)
    assert payload["status"] in ("ok", "degraded", "error")


def test_main_emits_error_json_when_probe_crashes(stub_workspace, capsys, monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(stub_workspace.hb, "probe", boom)
    rc = stub_workspace.hb.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "alert" in payload
