"""Tests for agents/fix-it/scripts/monthly-archival.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:monthly-archival`. Pure-Python deterministic implementation of
the confidence decay archival logic the operator wrote into the cron prompt.

Confidence decay formula:
    effective_confidence = original * 0.5 ** (days_since_recorded / half_life)

Category half-lives:
    identity     never  (never decays)
    established  365
    situation    90
    preference   180
    plan         30
    logistics    7
    rumor        14

Archival rules:
    - Facts: effective_confidence < 0.2 AND recorded_at > 90 days ago
    - Tasks: status == 'done' AND completed_at > 90 days ago

Destination: ~/Dropbox/openclaw-backup/archive/YYYY-MM/{facts|tasks}-...md
"""
from __future__ import annotations

import importlib.util
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "fix-it" / "scripts" / "monthly-archival.py"
FIXTURES = Path(__file__).parent / "fixtures" / "monthly-archival"


def _load():
    spec = importlib.util.spec_from_file_location("monthly_archival", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


# ─── effective_confidence ────────────────────────────────────────────


def test_effective_confidence_identity_never_decays(mod):
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    recorded = datetime(2020, 1, 1, tzinfo=timezone.utc)  # 6 years ago
    assert mod.effective_confidence(0.9, "identity", recorded, now) == 0.9


def test_effective_confidence_one_half_life_halves_value(mod):
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    # 90 days ago → exactly one half-life for "situation"
    recorded = datetime(2026, 1, 31, tzinfo=timezone.utc)
    eff = mod.effective_confidence(0.8, "situation", recorded, now)
    assert math.isclose(eff, 0.4, abs_tol=1e-3)


def test_effective_confidence_unknown_category_uses_default(mod):
    """Unknown categories fall back to a sensible mid-range half-life
    rather than crashing or treating as never-decay."""
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    recorded = datetime(2025, 11, 1, tzinfo=timezone.utc)  # 6 months ago
    eff = mod.effective_confidence(0.8, "weird-cat", recorded, now)
    assert 0.0 <= eff <= 0.8


def test_effective_confidence_recent_entry_barely_decays(mod):
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    recorded = datetime(2026, 4, 25, tzinfo=timezone.utc)  # 6 days ago
    eff = mod.effective_confidence(0.6, "preference", recorded, now)
    assert eff > 0.55  # barely moved


# ─── parse_facts_file ────────────────────────────────────────────────


def test_parse_facts_file_yields_entries(mod):
    facts = mod.parse_facts_file(FIXTURES / "facts-2026-04.md")
    ids = [e["id"] for e in facts]
    assert "connector-2026-04-12-001" in ids
    assert "old-rumor-2025-10-15-001" in ids
    assert "old-plan-2025-09-01-001" in ids
    assert "strong-established-2025-08-01-001" in ids


def test_parse_facts_file_extracts_confidence_and_category(mod):
    facts = mod.parse_facts_file(FIXTURES / "facts-2026-04.md")
    by_id = {e["id"]: e for e in facts}
    assert by_id["connector-2026-04-12-001"]["confidence"] == 0.9
    assert by_id["connector-2026-04-12-001"]["category"] == "identity"
    assert by_id["old-rumor-2025-10-15-001"]["category"] == "rumor"


# ─── archival selection ──────────────────────────────────────────────


def test_select_facts_to_archive_picks_stale_low_confidence(mod):
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    facts = mod.parse_facts_file(FIXTURES / "facts-2026-04.md")
    archival, kept = mod.select_facts_to_archive(facts, now)
    archival_ids = {e["id"] for e in archival}
    kept_ids = {e["id"] for e in kept}

    # rumor from Oct 2025 with conf 0.4 → after 6.5 months ≈ 12 half-lives
    # → effective ≈ 0.4 * 0.5**(196/14) ≈ ~0.0001 → archived
    assert "old-rumor-2025-10-15-001" in archival_ids

    # plan from Sep 2025 with conf 0.6 → 8 months ≈ ~8 half-lives →
    # effective ~0.002 → archived
    assert "old-plan-2025-09-01-001" in archival_ids

    # identity is never decayed
    assert "connector-2026-04-12-001" in kept_ids

    # strong established (half-life 365 days, 9 months age) → effective
    # ~0.95 * 0.5**(273/365) ≈ 0.57 → kept
    assert "strong-established-2025-08-01-001" in kept_ids


def test_select_facts_does_not_archive_recent_low_confidence(mod):
    """The 90-day floor protects fresh-but-low-confidence entries from
    immediate archival even if their effective confidence is low."""
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    facts = [
        {
            "id": "fresh-rumor",
            "confidence": 0.3,
            "category": "rumor",
            "recorded_at_dt": datetime(2026, 4, 15, tzinfo=timezone.utc),
            "raw": "...",
        }
    ]
    archival, kept = mod.select_facts_to_archive(facts, now)
    assert archival == []
    assert len(kept) == 1


# ─── tasks selection ─────────────────────────────────────────────────


def test_select_tasks_to_archive_picks_old_done(mod):
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    tasks = mod.parse_tasks_file(FIXTURES / "tasks-queue.md")
    archival, kept = mod.select_tasks_to_archive(tasks, now)
    archival_ids = {t["id"] for t in archival}
    kept_ids = {t["id"] for t in kept}

    # done in Dec 2025 → 5 months ago → archive
    assert "meetings-coach-2025-12-01-002" in archival_ids
    # done Apr 5 → less than 90 days → keep
    assert "connector-2026-04-01-005" in kept_ids
    # open task → keep regardless
    assert "connector-2026-04-10-001" in kept_ids


# ─── run() orchestration ─────────────────────────────────────────────


def test_run_writes_archive_and_removes_from_source(
    mod, tmp_path, monkeypatch
):
    brain = tmp_path / "openclaw-backup"
    facts_dir = brain / "facts"
    tasks_dir = brain / "tasks"
    archive_root = brain / "archive"
    facts_dir.mkdir(parents=True)
    tasks_dir.mkdir()

    facts_src = (FIXTURES / "facts-2026-04.md").read_text(encoding="utf-8")
    (facts_dir / "2026-04.md").write_text(facts_src, encoding="utf-8")
    tasks_src = (FIXTURES / "tasks-queue.md").read_text(encoding="utf-8")
    (tasks_dir / "queue.md").write_text(tasks_src, encoding="utf-8")

    monkeypatch.setattr(mod, "BRAIN_DIR", brain)
    monkeypatch.setattr(mod, "FACTS_DIR", facts_dir)
    monkeypatch.setattr(mod, "TASKS_FILE", tasks_dir / "queue.md")
    monkeypatch.setattr(mod, "ARCHIVE_ROOT", archive_root)
    workspace = tmp_path / "fix-it-workspace"
    (workspace / "cache").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-monthly-archival.json"
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 1, 3, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["facts_archived"] >= 2  # rumor + plan
    assert result["tasks_archived"] >= 1  # Dec 2025 done

    # Archive directory has files
    archive_dir = archive_root / "2026-05"
    assert archive_dir.exists()
    archive_files = list(archive_dir.glob("*.md"))
    assert len(archive_files) >= 1

    # Manifest written
    manifest = archive_dir / "manifest.json"
    assert manifest.exists()
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["facts_archived"] >= 2

    # Source facts file no longer contains the archived ids
    src_after = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "old-rumor-2025-10-15-001" not in src_after
    assert "old-plan-2025-09-01-001" not in src_after
    # but kept entries remain
    assert "connector-2026-04-12-001" in src_after
    assert "strong-established-2025-08-01-001" in src_after

    # Telegram report sent
    assert len(sent) == 1
    assert "archived" in sent[0].lower()


def test_run_silent_archive_when_nothing_to_move(
    mod, tmp_path, monkeypatch
):
    """If both facts and tasks have nothing eligible, the cron still
    writes a Telegram report (announce=true). It's a monthly summary —
    silence would be confusing."""
    brain = tmp_path / "openclaw-backup"
    facts_dir = brain / "facts"
    tasks_dir = brain / "tasks"
    facts_dir.mkdir(parents=True)
    tasks_dir.mkdir()
    (facts_dir / "2026-04.md").write_text("# Facts — 2026-04\n", encoding="utf-8")
    (tasks_dir / "queue.md").write_text("# Tasks — Queue\n", encoding="utf-8")

    monkeypatch.setattr(mod, "BRAIN_DIR", brain)
    monkeypatch.setattr(mod, "FACTS_DIR", facts_dir)
    monkeypatch.setattr(mod, "TASKS_FILE", tasks_dir / "queue.md")
    monkeypatch.setattr(mod, "ARCHIVE_ROOT", brain / "archive")
    workspace = tmp_path / "fix-it-workspace"
    (workspace / "cache").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-monthly-archival.json"
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 1, 3, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["facts_archived"] == 0
    assert result["tasks_archived"] == 0
    # Report still sent (announce=true cron)
    assert len(sent) == 1
    assert "0" in sent[0]


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
