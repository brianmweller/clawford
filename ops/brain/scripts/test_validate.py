"""Tests for ops/brain/scripts/validate.py.

Covers the R6 migration: per-agent .status.md files are no longer
authoritative (fleet-health.json is), so check_agent_files must not
fail on non-canonical headers — in fact it is retired entirely.
Also: deploy-backups/ and workspace-snapshots/ contain large tarballs
by design and must be excluded from the file-size warning.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_validate():
    script_path = Path(__file__).parent / "validate.py"
    spec = importlib.util.spec_from_file_location("brain_validate", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


validate = _load_validate()


def _seed_minimal_brain(root: Path) -> None:
    for d in ["people", "facts", "commitments", "tasks", "notes", "agents", "archive"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# OpenClaw Shared Brain\n", encoding="utf-8")
    (root / "people" / "_template.md").write_text("# {Full Name}\n", encoding="utf-8")
    (root / "commitments" / "active.md").write_text("# Commitments \u2014 Active\n", encoding="utf-8")
    (root / "tasks" / "queue.md").write_text("# Tasks \u2014 Queue\n", encoding="utf-8")
    (root / "notes" / "inbox.md").write_text("# Notes \u2014 Inbox\n", encoding="utf-8")
    (root / "facts" / "2026-04.md").write_text("# Facts \u2014 April 2026\n", encoding="utf-8")


# ── check_file_sizes exclusions ────────────────────────────────────

def test_file_sizes_ignores_deploy_backups(tmp_path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "deploy-backups").mkdir()
    (tmp_path / "deploy-backups" / "news-digest-20260413T162422Z.tar.gz").write_bytes(
        b"x" * (600 * 1024)
    )
    results = validate.check_file_sizes(tmp_path)
    warns = [r for r in results if r[0] == "WARN"]
    assert warns == [], f"deploy-backups/ must not produce size warnings, got: {warns}"


def test_file_sizes_ignores_workspace_snapshots(tmp_path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "workspace-snapshots").mkdir()
    (tmp_path / "workspace-snapshots" / "news-digest-2026-04-14.tar.gz").write_bytes(
        b"x" * (600 * 1024)
    )
    results = validate.check_file_sizes(tmp_path)
    warns = [r for r in results if r[0] == "WARN"]
    assert warns == [], f"workspace-snapshots/ must not produce size warnings, got: {warns}"


def test_file_sizes_still_flags_oversized_facts_file(tmp_path):
    _seed_minimal_brain(tmp_path)
    (tmp_path / "facts" / "2026-05.md").write_bytes(b"x" * (600 * 1024))
    results = validate.check_file_sizes(tmp_path)
    warns = [r for r in results if r[0] == "WARN"]
    assert len(warns) == 1
    assert "2026-05.md" in warns[0][1]


def test_file_sizes_ignores_nested_deploy_backups(tmp_path):
    """A large file under a nested deploy-backups/ path is still ignored."""
    _seed_minimal_brain(tmp_path)
    nested = tmp_path / "deploy-backups" / "subdir"
    nested.mkdir(parents=True)
    (nested / "blob.bin").write_bytes(b"x" * (600 * 1024))
    results = validate.check_file_sizes(tmp_path)
    warns = [r for r in results if r[0] == "WARN"]
    assert warns == []


# ── check_agent_files retirement ──────────────────────────────────

def test_check_agent_files_is_retired():
    """Per-agent .status.md header checks were retired in R6 — fleet-health.json
    is now authoritative, and status files are written freely by agent LLMs."""
    assert not hasattr(validate, "check_agent_files"), (
        "check_agent_files() must be removed; fleet-health.json is the authority post-R6"
    )


def test_main_passes_on_non_canonical_status_header(tmp_path, monkeypatch):
    """Integration: a brain with a bad-header .status.md must not cause main() to exit non-zero.

    This was the failure mode reported by brain-validation at 2026-04-14 12:03 UTC
    when family-calendar's LLM wrote '# family-calendar status' instead of
    '# Family Calendar — Status'.
    """
    _seed_minimal_brain(tmp_path)
    (tmp_path / "agents" / "family-calendar.status.md").write_text(
        "# family-calendar status\n\n- status: ok\n"
    )
    monkeypatch.setattr(validate, "BRAIN_ROOT", tmp_path)
    with pytest.raises(SystemExit) as exc:
        validate.main()
    assert exc.value.code == 0


def test_main_passes_with_large_backup_tarball(tmp_path, monkeypatch):
    """Integration: deploy-backups/ tarballs must not trip main() into WARN-noise."""
    _seed_minimal_brain(tmp_path)
    (tmp_path / "deploy-backups").mkdir()
    (tmp_path / "deploy-backups" / "shopping-20260413T015941Z.tar.gz").write_bytes(
        b"x" * (70 * 1024 * 1024)  # 70MB, mirrors real-world shopping backups
    )
    monkeypatch.setattr(validate, "BRAIN_ROOT", tmp_path)
    with pytest.raises(SystemExit) as exc:
        validate.main()
    assert exc.value.code == 0
