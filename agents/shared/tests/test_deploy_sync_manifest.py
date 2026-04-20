"""sync_manifest_structure — silent-delete guard.

Context: on 2026-04-20 a `deploy.py connector --sync-manifest` silently
dropped three legitimate scripts (facts_scope_augment_lib.py,
facts-scope-augment.py, inbound_act_lib.py) from the live manifest
because they were missing from manifest.json.example. No warning, no
confirmation — the list was just overwritten. The fix: when the sync
would remove one or more scripts/config_files/state_files, refuse to
write unless --force-delete is passed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import deploy  # type: ignore


def _write_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def test_sync_bootstraps_when_actual_missing(tmp_path: Path) -> None:
    """Bootstrap path: no manifest.json yet → copy structure from
    .example. Still works post-guard (no removals to guard against)."""
    example = tmp_path / "manifest.json.example"
    actual = tmp_path / "manifest.json"
    _write_manifest(example, {"scripts": ["a.py"], "config_files": [], "state_files": []})

    result = deploy.sync_manifest_structure(actual, example)

    assert result["status"] == "ok"
    assert result.get("bootstrapped") is True
    assert actual.exists()


def test_sync_adds_without_removals_proceeds(tmp_path: Path) -> None:
    """Additive sync: example adds one script, manifest is otherwise in
    sync. No removals → silent proceed is still the right call."""
    example = tmp_path / "manifest.json.example"
    actual = tmp_path / "manifest.json"
    _write_manifest(actual, {"scripts": ["a.py"], "config_files": [], "state_files": []})
    _write_manifest(example, {"scripts": ["a.py", "b.py"], "config_files": [], "state_files": []})

    result = deploy.sync_manifest_structure(actual, example)

    assert result["status"] == "ok"
    assert result.get("scripts_added") == ["b.py"]
    assert result.get("scripts_removed", []) == []
    with open(actual, encoding="utf-8") as f:
        assert json.load(f)["scripts"] == ["a.py", "b.py"]


def test_sync_blocks_silent_script_delete_without_force(tmp_path: Path) -> None:
    """Regression for the 2026-04-20 incident: example is missing a
    script that's in the live manifest. Without --force-delete, sync
    MUST refuse to write and return status=blocked with the removal
    list so the operator can audit."""
    example = tmp_path / "manifest.json.example"
    actual = tmp_path / "manifest.json"
    _write_manifest(
        actual,
        {"scripts": ["a.py", "legitimate_lib.py"], "config_files": [], "state_files": []},
    )
    _write_manifest(example, {"scripts": ["a.py"], "config_files": [], "state_files": []})

    result = deploy.sync_manifest_structure(actual, example)

    assert result["status"] == "blocked"
    assert "legitimate_lib.py" in result.get("scripts_removed", [])
    # The on-disk manifest must be UNCHANGED — the whole point is to
    # not silently destroy data.
    with open(actual, encoding="utf-8") as f:
        assert "legitimate_lib.py" in json.load(f)["scripts"]


def test_sync_force_delete_allows_script_removal(tmp_path: Path) -> None:
    """Escape hatch: operator inspected the removal list, decided it's
    correct, re-runs with --force-delete. Sync proceeds and the manifest
    ends up matching .example."""
    example = tmp_path / "manifest.json.example"
    actual = tmp_path / "manifest.json"
    _write_manifest(
        actual,
        {"scripts": ["a.py", "deprecated.py"], "config_files": [], "state_files": []},
    )
    _write_manifest(example, {"scripts": ["a.py"], "config_files": [], "state_files": []})

    result = deploy.sync_manifest_structure(actual, example, force_delete=True)

    assert result["status"] == "ok"
    assert result.get("scripts_removed") == ["deprecated.py"]
    with open(actual, encoding="utf-8") as f:
        assert json.load(f)["scripts"] == ["a.py"]


def test_sync_blocks_on_config_file_removal(tmp_path: Path) -> None:
    """Same guard applies to config_files removals — a silently dropped
    config reference is just as destructive as a silently dropped
    script."""
    example = tmp_path / "manifest.json.example"
    actual = tmp_path / "manifest.json"
    _write_manifest(
        actual,
        {
            "scripts": [],
            "config_files": [{"src": "keep.json"}, {"src": "drop.json"}],
            "state_files": [],
        },
    )
    _write_manifest(
        example,
        {"scripts": [], "config_files": [{"src": "keep.json"}], "state_files": []},
    )

    result = deploy.sync_manifest_structure(actual, example)

    assert result["status"] == "blocked"
    assert "drop.json" in result.get("config_files_removed", [])


def test_sync_blocks_on_state_file_removal(tmp_path: Path) -> None:
    example = tmp_path / "manifest.json.example"
    actual = tmp_path / "manifest.json"
    _write_manifest(
        actual,
        {
            "scripts": [],
            "config_files": [],
            "state_files": [{"path": "keep.json"}, {"path": "drop.json"}],
        },
    )
    _write_manifest(
        example,
        {"scripts": [], "config_files": [], "state_files": [{"path": "keep.json"}]},
    )

    result = deploy.sync_manifest_structure(actual, example)

    assert result["status"] == "blocked"
    assert "drop.json" in result.get("state_files_removed", [])


def test_sync_blocked_status_includes_all_removal_fields(tmp_path: Path) -> None:
    """When the block fires, the diff should surface every removal
    field so the operator sees the full scope in one log line."""
    example = tmp_path / "manifest.json.example"
    actual = tmp_path / "manifest.json"
    _write_manifest(
        actual,
        {
            "scripts": ["drop.py"],
            "config_files": [{"src": "drop.json"}],
            "state_files": [{"path": "drop.state"}],
        },
    )
    _write_manifest(
        example,
        {"scripts": [], "config_files": [], "state_files": []},
    )

    result = deploy.sync_manifest_structure(actual, example)

    assert result["status"] == "blocked"
    assert "drop.py" in result["scripts_removed"]
    assert "drop.json" in result["config_files_removed"]
    assert "drop.state" in result["state_files_removed"]
