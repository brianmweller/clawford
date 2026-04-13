"""R2 probe() contract tests for fix-it/scripts/heartbeat.py.

Verifies the probe() function added in R2: pure (no .status.md side
effects), returns a dict shaped for fleet-health.py serialization.
fix-it's heartbeat is unique in that it depends on `openclaw agents
list` to know which agents to scrape — tests stub that out.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_heartbeat():
    path = SCRIPTS_DIR / "heartbeat.py"
    spec = importlib.util.spec_from_file_location("fixit_heartbeat", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fixit_heartbeat"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_brain(tmp_path, monkeypatch):
    """Create a fake brain dir with a few status.md files + stub
    get_registered_agents to return a known set."""
    brain = tmp_path / "openclaw-backup"
    status_dir = brain / "agents"
    status_dir.mkdir(parents=True)

    # Two fresh agent status files
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (status_dir / "shopping.status.md").write_text(
        f"# Shopping — Status\n\n- **last_heartbeat:** {now_str}\n- **status:** ok\n",
        encoding="utf-8",
    )
    (status_dir / "connector.status.md").write_text(
        f"# Connector — Status\n\n- **last_heartbeat:** {now_str}\n- **status:** ok\n",
        encoding="utf-8",
    )

    mod = _load_heartbeat()
    monkeypatch.setattr(mod, "BRAIN", str(brain))
    monkeypatch.setattr(mod, "STATUS_DIR", str(status_dir))
    monkeypatch.setattr(mod, "OUTPUT_FILE", str(status_dir / "fix-it.status.md"))
    # Stub the openclaw subprocess call
    monkeypatch.setattr(mod, "get_registered_agents", lambda: {"shopping", "connector"})

    return mod, brain, status_dir


def test_probe_returns_dict_with_status_and_checked(fake_brain):
    mod, brain, status_dir = fake_brain
    result = mod.probe()
    assert "status" in result
    assert result["status"] in ("ok", "degraded")
    assert "checked" in result
    assert "stale_count" in result
    assert result["checked"] == 2


def test_probe_does_not_write_status_md(fake_brain):
    mod, brain, status_dir = fake_brain
    sentinel = status_dir / "fix-it.status.md"
    assert not sentinel.exists()
    mod.probe()
    assert not sentinel.exists(), "probe() must not write status.md"


def test_probe_returns_degraded_when_an_agent_is_stale(fake_brain):
    mod, brain, status_dir = fake_brain
    # Backdate connector to 2 hours ago (stale threshold is 90 min)
    old_str = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M UTC")
    (status_dir / "connector.status.md").write_text(
        f"# Connector — Status\n\n- **last_heartbeat:** {old_str}\n- **status:** ok\n",
        encoding="utf-8",
    )
    result = mod.probe()
    assert result["status"] == "degraded"
    assert result["stale_count"] == 1
    assert "alert" in result


def test_run_writes_status_md_then_returns_probe_result(fake_brain):
    mod, brain, status_dir = fake_brain
    sentinel = status_dir / "fix-it.status.md"
    result = mod.run()
    assert sentinel.exists()
    text = sentinel.read_text(encoding="utf-8")
    assert "# Fix-It — Status" in text
    assert "- **status:** healthy" in text  # fix-it uses 'healthy' for the human-facing label
    assert result["status"] == "ok"
