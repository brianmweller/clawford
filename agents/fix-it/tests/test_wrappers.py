"""Batched tests for the 6 fix-it wrapper orchestrators.

Phase 4 SCRIPT_CONTRACT wrappers — minimal coverage per script:
silent-on-success or alert-on-failure path, plus main()-exits-0.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "agents" / "fix-it" / "scripts"


def _load(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _patch_basics(mod, tmp_path, monkeypatch, last_run_name: str):
    workspace = tmp_path / "fix-it-workspace"
    (workspace / "cache").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / last_run_name
    )
    return workspace


# ─── conflict-scan ───────────────────────────────────────────────────


def test_conflict_scan_silent_when_no_conflicts(tmp_path, monkeypatch):
    mod = _load("conflict-scan")
    scan_root = tmp_path / "brain"
    scan_root.mkdir()
    (scan_root / "facts").mkdir()
    (scan_root / "facts" / "2026-04.md").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(mod, "SCAN_ROOT", scan_root)
    _patch_basics(mod, tmp_path, monkeypatch, "last-conflict-scan.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["status"] == "ok"
    assert result["conflicts"] == 0
    assert sent == []


def test_conflict_scan_alerts_on_conflict_files(tmp_path, monkeypatch):
    mod = _load("conflict-scan")
    scan_root = tmp_path / "brain"
    (scan_root / "facts").mkdir(parents=True)
    (scan_root / "facts" / "2026-04 (operator's MacBook's conflicted copy 2026-04-12).md").write_text(
        "x", encoding="utf-8"
    )
    monkeypatch.setattr(mod, "SCAN_ROOT", scan_root)
    _patch_basics(mod, tmp_path, monkeypatch, "last-conflict-scan.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["conflicts"] == 1
    assert result["sent"] == 1
    assert "conflicted copy" in sent[0]


def test_conflict_scan_ignores_backup_and_archive_trees(tmp_path, monkeypatch):
    """Conflicted copies inside deploy-backups, workspace-snapshots,
    or archive folders are not actionable — skip them."""
    mod = _load("conflict-scan")
    scan_root = tmp_path / "brain"
    (scan_root / "deploy-backups").mkdir(parents=True)
    (scan_root / "deploy-backups" / "shopping (conflicted copy 2026-04-12).tar.gz").write_text(
        "x", encoding="utf-8"
    )
    (scan_root / "workspace-snapshots").mkdir()
    (scan_root / "workspace-snapshots" / "fix-it (conflicted copy 2026-04-12).tar.gz").write_text(
        "x", encoding="utf-8"
    )
    (scan_root / "archive" / "2026-04").mkdir(parents=True)
    (scan_root / "archive" / "2026-04" / "facts (conflicted copy).md").write_text(
        "x", encoding="utf-8"
    )
    monkeypatch.setattr(mod, "SCAN_ROOT", scan_root)
    _patch_basics(mod, tmp_path, monkeypatch, "last-conflict-scan.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["conflicts"] == 0
    assert sent == []


def test_conflict_scan_main_exits_zero(monkeypatch, capsys):
    mod = _load("conflict-scan")
    monkeypatch.setattr(mod, "run", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    rc = mod.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert payload["status"] == "error"


# ─── file-size-monitor ──────────────────────────────────────────────


def test_file_size_silent_when_all_small(tmp_path, monkeypatch):
    mod = _load("file-size-monitor")
    scan_root = tmp_path / "brain"
    scan_root.mkdir()
    (scan_root / "small.md").write_text("x" * 1000, encoding="utf-8")
    monkeypatch.setattr(mod, "SCAN_ROOT", scan_root)
    _patch_basics(mod, tmp_path, monkeypatch, "last-file-size-monitor.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["large_files"] == 0
    assert sent == []


def test_file_size_alerts_when_oversized(tmp_path, monkeypatch):
    mod = _load("file-size-monitor")
    scan_root = tmp_path / "brain"
    scan_root.mkdir()
    (scan_root / "big.md").write_text("x" * (600 * 1024), encoding="utf-8")
    monkeypatch.setattr(mod, "SCAN_ROOT", scan_root)
    _patch_basics(mod, tmp_path, monkeypatch, "last-file-size-monitor.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["large_files"] == 1
    assert result["sent"] == 1
    assert "big.md" in sent[0]


def test_file_size_ignores_backups_snapshots_archives(tmp_path, monkeypatch):
    """Mr Fixit must not alert on files inside deploy-backups,
    workspace-snapshots, or archive trees — those are legitimately
    large by design. Same for *.tar.gz / *.zip suffixes anywhere."""
    mod = _load("file-size-monitor")
    scan_root = tmp_path / "brain"
    scan_root.mkdir()
    big_payload = "x" * (600 * 1024)

    (scan_root / "deploy-backups").mkdir()
    (scan_root / "deploy-backups" / "shopping-2026.tar.gz").write_text(
        big_payload, encoding="utf-8"
    )
    (scan_root / "workspace-snapshots").mkdir()
    (scan_root / "workspace-snapshots" / "fix-it-2026.tar.gz").write_text(
        big_payload, encoding="utf-8"
    )
    (scan_root / "archive" / "2026-04").mkdir(parents=True)
    (scan_root / "archive" / "2026-04" / "old-facts.md").write_text(
        big_payload, encoding="utf-8"
    )
    # A bare tar.gz at the root should also be ignored by suffix
    (scan_root / "loose.tar.gz").write_text(big_payload, encoding="utf-8")

    monkeypatch.setattr(mod, "SCAN_ROOT", scan_root)
    _patch_basics(mod, tmp_path, monkeypatch, "last-file-size-monitor.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["large_files"] == 0
    assert sent == []


def test_file_size_still_alerts_on_unexpected_large_brain_file(tmp_path, monkeypatch):
    """An oversized file outside the ignored trees still triggers —
    e.g. a runaway facts/2026-04.md or a stray cache."""
    mod = _load("file-size-monitor")
    scan_root = tmp_path / "brain"
    (scan_root / "facts").mkdir(parents=True)
    (scan_root / "facts" / "2026-04.md").write_text("x" * (700 * 1024), encoding="utf-8")
    # And a normal tarball that should be ignored
    (scan_root / "deploy-backups").mkdir()
    (scan_root / "deploy-backups" / "shopping.tar.gz").write_text(
        "x" * (600 * 1024), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "SCAN_ROOT", scan_root)
    _patch_basics(mod, tmp_path, monkeypatch, "last-file-size-monitor.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["large_files"] == 1
    assert result["sent"] == 1
    assert "facts/2026-04.md" in sent[0] or "facts\\2026-04.md" in sent[0]


# ─── brain-validation-check ─────────────────────────────────────────


def test_brain_validation_silent_on_success(tmp_path, monkeypatch):
    mod = _load("brain-validation-check")
    monkeypatch.setattr(mod, "_run_validate", lambda: (0, "all good", ""))
    _patch_basics(mod, tmp_path, monkeypatch, "last-brain-validation.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["status"] == "ok"
    assert result["sent"] == 0


def test_brain_validation_alerts_on_failure(tmp_path, monkeypatch):
    mod = _load("brain-validation-check")
    monkeypatch.setattr(
        mod, "_run_validate", lambda: (1, "schema mismatch on facts/2026-04.md", "")
    )
    _patch_basics(mod, tmp_path, monkeypatch, "last-brain-validation.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["sent"] == 1
    assert "schema mismatch" in sent[0]


# ─── security-audit-alert ───────────────────────────────────────────


def test_security_audit_alert_forwards_stdout_on_success(tmp_path, monkeypatch):
    mod = _load("security-audit-alert")
    monkeypatch.setattr(mod, "_run_audit", lambda: (0, "audit report content"))
    _patch_basics(mod, tmp_path, monkeypatch, "last-security-audit.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["status"] == "ok"
    assert result["sent"] == 1
    assert sent[0] == "audit report content"


def test_security_audit_alert_marks_degraded_on_error(tmp_path, monkeypatch):
    mod = _load("security-audit-alert")
    monkeypatch.setattr(mod, "_run_audit", lambda: (1, "openclaw not found"))
    _patch_basics(mod, tmp_path, monkeypatch, "last-security-audit.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["status"] == "degraded"
    assert result["sent"] == 1
    assert "ERROR" in sent[0]


# ─── obsidian-briefing-check ────────────────────────────────────────


def test_obsidian_briefing_silent_on_success(tmp_path, monkeypatch):
    mod = _load("obsidian-briefing-check")
    monkeypatch.setattr(mod, "_run_generate", lambda: (0, "wrote /path/briefing.md"))
    _patch_basics(mod, tmp_path, monkeypatch, "last-obsidian-briefing.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["status"] == "ok"
    assert result["sent"] == 0


def test_obsidian_briefing_alerts_on_failure(tmp_path, monkeypatch):
    mod = _load("obsidian-briefing-check")
    monkeypatch.setattr(
        mod, "_run_generate", lambda: (1, "ImportError: no module named obsidian")
    )
    _patch_basics(mod, tmp_path, monkeypatch, "last-obsidian-briefing.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["sent"] == 1
    assert "ImportError" in sent[0]


# ─── workspace-snapshot-check ───────────────────────────────────────


def test_workspace_snapshot_silent_on_success(tmp_path, monkeypatch):
    mod = _load("workspace-snapshot-check")
    monkeypatch.setattr(mod, "_run_snapshot", lambda: (0, "snapshots written: 6"))
    _patch_basics(mod, tmp_path, monkeypatch, "last-workspace-snapshot.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["status"] == "ok"
    assert result["sent"] == 0


def test_workspace_snapshot_alerts_on_failure(tmp_path, monkeypatch):
    mod = _load("workspace-snapshot-check")
    monkeypatch.setattr(mod, "_run_snapshot", lambda: (2, "tar exited 2"))
    _patch_basics(mod, tmp_path, monkeypatch, "last-workspace-snapshot.json")

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    result = mod.run()
    assert result["sent"] == 1
    assert "tar exited 2" in sent[0]
