"""Tests for deploy.sync_manifest_structure — propagates structural changes
from manifest.json.example (tracked in git) to manifest.json (gitignored,
operator-specific) while preserving operator-private fields.

Context: manifest.json contains PII in cron prompts (real names, places,
calendar IDs). Only manifest.json.example is in git. When a structural
change needs to flow — adding/removing config_files entries, updating
scripts lists, etc. — it lands in .example via git, then operators run
this sync to apply it to their manifest.json.

Structural fields synced: config_files, scripts, state_files.
Operator-private fields preserved: crons (contains prompt PII),
approvals, agent_id, display_name, workspace, telegram.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


@pytest.fixture
def paths(tmp_path):
    """Fresh agent dir with both .example and real manifest."""
    agent_dir = tmp_path / "agents" / "testagent"
    agent_dir.mkdir(parents=True)
    return {
        "agent_dir": agent_dir,
        "example": agent_dir / "manifest.json.example",
        "actual": agent_dir / "manifest.json",
    }


def _example(config_files, scripts=None, state_files=None):
    return {
        "agent_id": "testagent",
        "display_name": "Test Agent",
        "workspace": "~/.clawford/testagent-workspace",
        "config_files": config_files,
        "scripts": scripts or ["scripts/foo.py"],
        "state_files": state_files or [],
        "crons": [
            {"name": "example-cron", "message": "GENERIC placeholder text"}
        ],
    }


def _operator(config_files, scripts=None, pii_cron_message="Dentist at 3pm Tuesday for the operator"):
    return {
        "agent_id": "testagent",
        "display_name": "Test Agent",
        "workspace": "~/.clawford/testagent-workspace",
        "config_files": config_files,
        "scripts": scripts or ["scripts/foo.py"],
        "state_files": [],
        "crons": [
            {"name": "real-cron", "message": pii_cron_message}
        ],
        "approvals": {"allowlist": ["/usr/bin/*"]},
    }


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_deploy_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("deploy", SHARED_DIR / "deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── sync_manifest_structure ─────────────────────────────────────


def test_sync_removes_entries_from_config_files(paths):
    """The core use case: .example drops TOOLS.md, sync removes it from
    the operator's manifest.json too."""
    _write(paths["example"], _example(config_files=[{"src": "SOUL.md"}, {"src": "USER.md"}]))
    _write(paths["actual"], _operator(config_files=[
        {"src": "SOUL.md"}, {"src": "TOOLS.md"}, {"src": "USER.md"}
    ]))

    deploy = _load_deploy_module()
    result = deploy.sync_manifest_structure(paths["actual"], paths["example"])

    assert result["status"] == "ok"
    updated = _read(paths["actual"])
    srcs = {cf["src"] for cf in updated["config_files"]}
    assert srcs == {"SOUL.md", "USER.md"}
    assert "TOOLS.md" not in srcs


def test_sync_preserves_pii_in_cron_messages(paths):
    """Cron prompts contain real names/places — MUST not be clobbered."""
    _write(paths["example"], _example(config_files=[{"src": "SOUL.md"}]))
    _write(paths["actual"], _operator(
        config_files=[{"src": "SOUL.md"}],
        pii_cron_message="Avery's dentist is at 3pm in Berkeley",
    ))

    deploy = _load_deploy_module()
    deploy.sync_manifest_structure(paths["actual"], paths["example"])

    updated = _read(paths["actual"])
    assert updated["crons"][0]["message"] == "Avery's dentist is at 3pm in Berkeley"
    assert updated["crons"][0]["name"] == "real-cron"


def test_sync_preserves_operator_approvals(paths):
    """Operator-specific allowlist should not be touched."""
    _write(paths["example"], _example(config_files=[{"src": "SOUL.md"}]))
    operator = _operator(config_files=[{"src": "SOUL.md"}])
    operator["approvals"] = {"allowlist": ["/usr/bin/*", "/opt/custom/*"]}
    _write(paths["actual"], operator)

    deploy = _load_deploy_module()
    deploy.sync_manifest_structure(paths["actual"], paths["example"])

    updated = _read(paths["actual"])
    assert updated["approvals"]["allowlist"] == ["/usr/bin/*", "/opt/custom/*"]


def test_sync_updates_scripts_list(paths):
    """scripts list is structural — should sync."""
    _write(paths["example"], _example(
        config_files=[{"src": "SOUL.md"}],
        scripts=["scripts/new.py", "scripts/existing.py"],
    ))
    _write(paths["actual"], _operator(
        config_files=[{"src": "SOUL.md"}],
        scripts=["scripts/existing.py", "scripts/old.py"],
    ))

    deploy = _load_deploy_module()
    deploy.sync_manifest_structure(paths["actual"], paths["example"])

    updated = _read(paths["actual"])
    assert set(updated["scripts"]) == {"scripts/new.py", "scripts/existing.py"}


def test_sync_updates_state_files(paths):
    """state_files list is structural — should sync."""
    _write(paths["example"], _example(
        config_files=[{"src": "SOUL.md"}],
        state_files=[{"path": "new-state.json"}],
    ))
    op = _operator(config_files=[{"src": "SOUL.md"}])
    op["state_files"] = [{"path": "old-state.json"}]
    _write(paths["actual"], op)

    deploy = _load_deploy_module()
    deploy.sync_manifest_structure(paths["actual"], paths["example"])

    updated = _read(paths["actual"])
    assert updated["state_files"] == [{"path": "new-state.json"}]


def test_sync_is_idempotent(paths):
    """Running sync twice should produce the same result as once."""
    _write(paths["example"], _example(config_files=[{"src": "SOUL.md"}]))
    _write(paths["actual"], _operator(config_files=[{"src": "SOUL.md"}, {"src": "TOOLS.md"}]))

    deploy = _load_deploy_module()
    deploy.sync_manifest_structure(paths["actual"], paths["example"])
    first = _read(paths["actual"])

    deploy.sync_manifest_structure(paths["actual"], paths["example"])
    second = _read(paths["actual"])

    assert first == second


def test_sync_reports_what_changed(paths):
    """Return value describes the structural diff applied."""
    _write(paths["example"], _example(
        config_files=[{"src": "SOUL.md"}],
        scripts=["a.py", "b.py"],
    ))
    _write(paths["actual"], _operator(
        config_files=[{"src": "SOUL.md"}, {"src": "TOOLS.md"}, {"src": "HEARTBEAT.md"}],
        scripts=["a.py"],
    ))

    deploy = _load_deploy_module()
    result = deploy.sync_manifest_structure(paths["actual"], paths["example"])

    assert result["status"] == "ok"
    assert result["config_files_removed"] == ["TOOLS.md", "HEARTBEAT.md"] or \
           set(result["config_files_removed"]) == {"TOOLS.md", "HEARTBEAT.md"}
    assert "b.py" in result["scripts_added"]


def test_sync_missing_example_returns_error(paths):
    """If .example doesn't exist, can't sync."""
    _write(paths["actual"], _operator(config_files=[{"src": "SOUL.md"}]))

    deploy = _load_deploy_module()
    result = deploy.sync_manifest_structure(paths["actual"], paths["example"])
    assert result["status"] == "error"


def test_sync_missing_actual_creates_it(paths):
    """If manifest.json doesn't exist, bootstrap it from .example."""
    _write(paths["example"], _example(config_files=[{"src": "SOUL.md"}]))
    # no actual file
    assert not paths["actual"].exists()

    deploy = _load_deploy_module()
    result = deploy.sync_manifest_structure(paths["actual"], paths["example"])

    assert result["status"] == "ok"
    assert paths["actual"].exists()
    updated = _read(paths["actual"])
    assert updated["config_files"] == [{"src": "SOUL.md"}]


def test_sync_preserves_cron_enabled_flags(paths):
    """Operator may have enabled/disabled specific crons — preserve that."""
    _write(paths["example"], _example(config_files=[{"src": "SOUL.md"}]))
    op = _operator(config_files=[{"src": "SOUL.md"}])
    op["crons"] = [
        {"name": "c1", "enabled": False, "message": "disabled"},
        {"name": "c2", "enabled": True, "message": "running"},
    ]
    _write(paths["actual"], op)

    deploy = _load_deploy_module()
    deploy.sync_manifest_structure(paths["actual"], paths["example"])

    updated = _read(paths["actual"])
    enabled_by_name = {c["name"]: c.get("enabled") for c in updated["crons"]}
    assert enabled_by_name == {"c1": False, "c2": True}
