"""Tests for the docker-compose.yml drift safeguard.

The runtime lives at ~/openclaw/docker-compose.yml on the VPS, but
the canonical source is ops/docker-compose.yml in git. Before Phase
3b-followup these could drift silently — I bind-mounted .codex into
the VPS copy without touching git for hours before noticing.

check_compose_yml_drift() refuses to deploy when the two copies
disagree. Allowed states:
  - runtime path doesn't exist (fresh install; user will
    bootstrap it)
  - runtime path is a symlink resolving to the tracked copy
    (the preferred post-followup-2 shape)
  - runtime path is a regular file byte-identical to the
    tracked copy (grace period for legacy installs that
    haven't run the symlink migration yet)

Refused: runtime path is a regular file whose content drifts.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_drift_check_passes_when_runtime_path_missing(deploy_module, tmp_path, monkeypatch):
    runtime = tmp_path / "openclaw" / "docker-compose.yml"
    tracked = tmp_path / "repo" / "ops" / "docker-compose.yml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("services: {}\n")

    monkeypatch.setattr(deploy_module, "COMPOSE_RUNTIME_PATH", runtime)
    monkeypatch.setattr(deploy_module, "COMPOSE_TRACKED_PATH", tracked)

    errors = deploy_module.check_compose_yml_drift()
    assert errors == []


def test_drift_check_passes_when_runtime_matches_tracked_bytes(
    deploy_module, tmp_path, monkeypatch
):
    content = "services:\n  openclaw-gateway:\n    image: test\n"
    runtime = tmp_path / "openclaw" / "docker-compose.yml"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(content)
    tracked = tmp_path / "repo" / "ops" / "docker-compose.yml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text(content)

    monkeypatch.setattr(deploy_module, "COMPOSE_RUNTIME_PATH", runtime)
    monkeypatch.setattr(deploy_module, "COMPOSE_TRACKED_PATH", tracked)

    errors = deploy_module.check_compose_yml_drift()
    assert errors == []


def test_drift_check_fails_when_runtime_differs_from_tracked(
    deploy_module, tmp_path, monkeypatch
):
    runtime = tmp_path / "openclaw" / "docker-compose.yml"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("services:\n  stale: old\n")
    tracked = tmp_path / "repo" / "ops" / "docker-compose.yml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("services:\n  openclaw-gateway:\n    image: test\n")

    monkeypatch.setattr(deploy_module, "COMPOSE_RUNTIME_PATH", runtime)
    monkeypatch.setattr(deploy_module, "COMPOSE_TRACKED_PATH", tracked)

    errors = deploy_module.check_compose_yml_drift()
    assert errors
    assert any("differs" in e for e in errors)


def test_drift_check_passes_when_runtime_is_symlink_to_tracked(
    deploy_module, tmp_path, monkeypatch
):
    """Preferred post-followup-2 shape: the runtime path is a symlink
    into the git checkout so edits can't diverge."""
    if not hasattr(os, "symlink"):
        pytest.skip("no symlink support")

    tracked = tmp_path / "repo" / "ops" / "docker-compose.yml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("services: {}\n")

    runtime = tmp_path / "openclaw" / "docker-compose.yml"
    runtime.parent.mkdir(parents=True)
    try:
        os.symlink(str(tracked), str(runtime))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not supported in this env")

    monkeypatch.setattr(deploy_module, "COMPOSE_RUNTIME_PATH", runtime)
    monkeypatch.setattr(deploy_module, "COMPOSE_TRACKED_PATH", tracked)

    errors = deploy_module.check_compose_yml_drift()
    assert errors == []


def test_drift_check_fails_when_symlink_targets_wrong_file(
    deploy_module, tmp_path, monkeypatch
):
    if not hasattr(os, "symlink"):
        pytest.skip("no symlink support")

    tracked = tmp_path / "repo" / "ops" / "docker-compose.yml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("services: {}\n")

    other = tmp_path / "openclaw" / "some-other-compose.yml"
    other.parent.mkdir(parents=True)
    other.write_text("services:\n  wrong: file\n")

    runtime = tmp_path / "openclaw" / "docker-compose.yml"
    try:
        os.symlink(str(other), str(runtime))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not supported in this env")

    monkeypatch.setattr(deploy_module, "COMPOSE_RUNTIME_PATH", runtime)
    monkeypatch.setattr(deploy_module, "COMPOSE_TRACKED_PATH", tracked)

    errors = deploy_module.check_compose_yml_drift()
    assert errors
    assert any("symlink" in e.lower() for e in errors)


def test_drift_check_fails_when_tracked_copy_is_missing(
    deploy_module, tmp_path, monkeypatch
):
    """The git tracked copy must exist — if it doesn't, something
    is very wrong with the repo checkout, refuse to deploy."""
    runtime = tmp_path / "openclaw" / "docker-compose.yml"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("services: {}\n")
    tracked = tmp_path / "repo" / "ops" / "docker-compose.yml"  # never created

    monkeypatch.setattr(deploy_module, "COMPOSE_RUNTIME_PATH", runtime)
    monkeypatch.setattr(deploy_module, "COMPOSE_TRACKED_PATH", tracked)

    errors = deploy_module.check_compose_yml_drift()
    assert errors
    assert any("tracked" in e.lower() or "missing" in e.lower() for e in errors)
