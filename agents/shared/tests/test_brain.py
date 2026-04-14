"""Tests for agents/shared/brain.py — shared-brain filesystem helpers.

Tests use tmp_path + env-var overrides to point the brain module at
a sandbox instead of touching the real Dropbox-synced or git-tracked
locations. Atomic writes, parent directory creation, and append-line
semantics are exercised here so the contract is pinned.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload_brain():
    for mod in list(sys.modules):
        if mod == "brain" or mod.startswith("brain."):
            del sys.modules[mod]
    import brain  # type: ignore
    return brain


@pytest.fixture
def sandboxed_brain(tmp_path, monkeypatch):
    """Point the brain module at tmp_path for both Dropbox and git roots."""
    dropbox_root = tmp_path / "dropbox-brain"
    git_root = tmp_path / "git-brain"
    dropbox_root.mkdir()
    git_root.mkdir()
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(dropbox_root))
    monkeypatch.setenv("CLAWFORD_BRAIN_GIT_ROOT", str(git_root))
    return {"dropbox": dropbox_root, "git": git_root}


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def test_dropbox_brain_root_from_env(sandboxed_brain):
    brain = _reload_brain()
    assert brain.dropbox_brain_root() == sandboxed_brain["dropbox"]


def test_dropbox_brain_root_default_is_expanduser(monkeypatch, tmp_path):
    """Without CLAWFORD_BRAIN_DROPBOX_ROOT, fall back to
    Path.home() / Dropbox / openclaw-backup (the convention used by
    every existing brain-aware script in the fleet)."""
    monkeypatch.delenv("CLAWFORD_BRAIN_DROPBOX_ROOT", raising=False)
    brain = _reload_brain()
    root = brain.dropbox_brain_root()
    assert root.name == "openclaw-backup"
    assert root.parent.name == "Dropbox"


def test_git_brain_root_from_env(sandboxed_brain):
    brain = _reload_brain()
    assert brain.git_brain_root() == sandboxed_brain["git"]


def test_git_brain_root_default_resolves_repo_ops_brain(monkeypatch):
    """Default git_brain_root is agents/shared/brain.py → ../../ops/brain."""
    monkeypatch.delenv("CLAWFORD_BRAIN_GIT_ROOT", raising=False)
    brain = _reload_brain()
    root = brain.git_brain_root()
    # Should end in ops/brain relative to the repo root
    parts = root.parts
    assert "ops" in parts
    assert parts[parts.index("ops") + 1] == "brain"


# ---------------------------------------------------------------------------
# Text read / write / append
# ---------------------------------------------------------------------------


def test_read_text_success(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["dropbox"] / "facts" / "life.md").parent.mkdir()
    (sandboxed_brain["dropbox"] / "facts" / "life.md").write_text("42", encoding="utf-8")

    assert brain.read_text("facts/life.md") == "42"


def test_read_text_raises_on_missing(sandboxed_brain):
    brain = _reload_brain()
    with pytest.raises(FileNotFoundError):
        brain.read_text("nope/missing.md")


def test_write_text_creates_parent_dirs(sandboxed_brain):
    brain = _reload_brain()
    brain.write_text("deep/nested/dir/file.md", "hello")
    assert (sandboxed_brain["dropbox"] / "deep" / "nested" / "dir" / "file.md").read_text() == "hello"


def test_write_text_overwrites_existing(sandboxed_brain):
    brain = _reload_brain()
    brain.write_text("foo.md", "first")
    brain.write_text("foo.md", "second")
    assert (sandboxed_brain["dropbox"] / "foo.md").read_text() == "second"


def test_append_text_creates_file_if_missing(sandboxed_brain):
    brain = _reload_brain()
    brain.append_text("queues/events.log", "event-1")
    assert (sandboxed_brain["dropbox"] / "queues" / "events.log").read_text() == "event-1\n"


def test_append_text_appends_to_existing(sandboxed_brain):
    brain = _reload_brain()
    brain.append_text("queues/events.log", "event-1")
    brain.append_text("queues/events.log", "event-2")
    content = (sandboxed_brain["dropbox"] / "queues" / "events.log").read_text()
    assert content == "event-1\nevent-2\n"


def test_append_text_preserves_existing_trailing_newline(sandboxed_brain):
    """If the caller passes a line that already has a trailing newline,
    don't add a second one."""
    brain = _reload_brain()
    brain.append_text("q.log", "line-with-newline\n")
    assert (sandboxed_brain["dropbox"] / "q.log").read_text() == "line-with-newline\n"


# ---------------------------------------------------------------------------
# JSON read / write (atomic)
# ---------------------------------------------------------------------------


def test_read_json_success(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["dropbox"] / "data.json").write_text('{"x": 1}', encoding="utf-8")

    assert brain.read_json("data.json") == {"x": 1}


def test_write_json_happy_path(sandboxed_brain):
    brain = _reload_brain()
    brain.write_json("out.json", {"a": 1, "b": [2, 3]})

    loaded = json.loads((sandboxed_brain["dropbox"] / "out.json").read_text())
    assert loaded == {"a": 1, "b": [2, 3]}


def test_write_json_is_atomic_no_tmp_leftover(sandboxed_brain):
    """Atomic write means: after success, only the target file exists,
    not a .tmp sibling. A crashed write should leave the target intact
    (not tested here — but we verify no tmp leakage on success)."""
    brain = _reload_brain()
    brain.write_json("state.json", {"version": 2})

    dropbox_files = list(sandboxed_brain["dropbox"].iterdir())
    tmp_files = [f for f in dropbox_files if f.suffix == ".tmp" or ".json.tmp" in f.name]
    assert tmp_files == []


def test_write_json_creates_parent_dirs(sandboxed_brain):
    brain = _reload_brain()
    brain.write_json("deep/nested/report.json", {"ok": True})
    assert (sandboxed_brain["dropbox"] / "deep" / "nested" / "report.json").exists()


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def test_read_fleet_health_uses_canonical_path(sandboxed_brain):
    """fleet-health.json lives at the dropbox brain root — not in a
    subdirectory. Matches existing convention in ops/scripts/fleet-health.py."""
    brain = _reload_brain()
    (sandboxed_brain["dropbox"] / "fleet-health.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )
    data = brain.read_fleet_health()
    assert data == {"agents": {}}


def test_write_fleet_health_uses_canonical_path(sandboxed_brain):
    brain = _reload_brain()
    brain.write_fleet_health({"agents": {"fix-it": {"status": "ok"}}})
    loaded = json.loads(
        (sandboxed_brain["dropbox"] / "fleet-health.json").read_text()
    )
    assert loaded["agents"]["fix-it"]["status"] == "ok"


def test_agent_status_path_returns_per_agent_dir(sandboxed_brain):
    brain = _reload_brain()
    path = brain.agent_status_path("news-digest")
    assert path == sandboxed_brain["dropbox"] / "agents" / "news-digest"


# ---------------------------------------------------------------------------
# Git-side reads
# ---------------------------------------------------------------------------


def test_read_git_text_uses_git_root(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["git"] / "rules" / "core.md").parent.mkdir()
    (sandboxed_brain["git"] / "rules" / "core.md").write_text("# rules", encoding="utf-8")

    assert brain.read_git_text("rules/core.md") == "# rules"


def test_read_git_json_uses_git_root(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["git"] / "schemas.json").write_text('{"v": 1}', encoding="utf-8")

    assert brain.read_git_json("schemas.json") == {"v": 1}
