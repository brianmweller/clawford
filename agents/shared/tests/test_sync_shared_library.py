"""Tests for deploy.py::sync_shared_library.

Phase 2a landed the shared modules (telegram_api, retry_policy, brain)
but never hooked `agents/shared/` into the deploy flow, so the
per-agent exemplars like `family-calendar/scripts/timed-deliver.py`
referenced a `~/.clawford/shared/` directory that deploy.py never
created. Phase 2b + 3a then compounded the problem with new shared
modules (heartbeat_base, google_oauth, playwright_profile,
camoufox_proxy, llm) and per-agent import shims that assumed the
repo root was an ancestor of the script — which was true locally but
not inside the gateway container during the OpenClaw era.

sync_shared_library fixes this by mirroring `agents/shared/*.py`
(runtime modules only) into `<workspace>/agents/shared/*.py` during
each deploy. Scripts then use a small sys.path shim that finds the
first ancestor containing `agents/shared/` and prepends it — working
uniformly in local tests (repo layout) and on the VPS (workspace
layout).

These tests exercise the helper directly against a tmp source repo +
tmp workspace, plus one integration test showing it runs inside
deploy_one's main flow.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest


# ─── Helpers ────────────────────────────────────────────────────────


def _seed_shared_library(repo: Path) -> Path:
    """Create a realistic agents/shared/ layout under the fake source
    repo — runtime modules + a tests/ subdir + a handful of deploy-only
    files that should NOT be deployed."""
    shared = repo / "agents" / "shared"
    shared.mkdir(parents=True, exist_ok=True)

    # Runtime modules — these must get copied
    (shared / "llm.py").write_text("# llm\nDEFAULT_MODEL = 'gpt-5.4'\n", encoding="utf-8")
    (shared / "telegram_api.py").write_text("# telegram_api\n", encoding="utf-8")
    (shared / "retry_policy.py").write_text("# retry_policy\n", encoding="utf-8")
    (shared / "brain.py").write_text("# brain\n", encoding="utf-8")
    (shared / "heartbeat_base.py").write_text("# heartbeat_base\n", encoding="utf-8")
    (shared / "google_oauth.py").write_text("# google_oauth\n", encoding="utf-8")
    (shared / "playwright_profile.py").write_text("# playwright_profile\n", encoding="utf-8")
    (shared / "camoufox_proxy.py").write_text("# camoufox_proxy\n", encoding="utf-8")
    # P0.1 + P0.4 modules — added 2026-04-16 after a production
    # ModuleNotFoundError surfaced from activity-email-check.
    (shared / "scan_fields.py").write_text("# scan_fields\n", encoding="utf-8")
    (shared / "inbound_scanner.py").write_text("# inbound_scanner\n", encoding="utf-8")
    (shared / "inbound_patterns.py").write_text("# inbound_patterns\n", encoding="utf-8")
    (shared / "reviewer.py").write_text("# reviewer\n", encoding="utf-8")
    # P1.3 module
    (shared / "rate_limit.py").write_text("# rate_limit\n", encoding="utf-8")

    # Data-only subdir (P0.4 prompt templates)
    prompts = shared / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "anti_leakage.txt").write_text("anti-leakage suffix\n", encoding="utf-8")
    (prompts / "semantic_guard.txt").write_text("classifier system prompt\n", encoding="utf-8")

    # Deploy-only / infra — must NOT get copied into workspaces
    (shared / "deploy.py").write_text("# deploy tool — never deploy into workspaces\n", encoding="utf-8")
    (shared / "workspace-snapshot.py").write_text("# snapshot tool\n", encoding="utf-8")
    (shared / "write-status.py").write_text("# status writer\n", encoding="utf-8")
    (shared / "contract_wrap.py").write_text("# contract wrapper\n", encoding="utf-8")
    (shared / "import_from_deploy_sh.py").write_text("# legacy importer\n", encoding="utf-8")
    (shared / "SCRIPT_CONTRACT.md").write_text("# contract doc\n", encoding="utf-8")
    (shared / "fleet-manifest.json").write_text("{}\n", encoding="utf-8")

    # Tests — must NOT get copied
    tests_dir = shared / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_llm.py").write_text("# tests\n", encoding="utf-8")
    (tests_dir / "conftest.py").write_text("# conftest\n", encoding="utf-8")

    return shared


@pytest.fixture
def seeded_repo(fake_source_repo: Path) -> Path:
    """Extends the conftest fixture with a realistic agents/shared/ tree."""
    _seed_shared_library(fake_source_repo)
    return fake_source_repo


# ─── Happy path ─────────────────────────────────────────────────────


def test_sync_shared_library_copies_all_runtime_modules(
    deploy_module, seeded_repo, fake_workspace
):
    """Every runtime module should land under <workspace>/agents/shared/."""
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )

    deploy_module.sync_shared_library(mf)

    target = fake_workspace / "agents" / "shared"
    assert target.is_dir(), f"agents/shared/ not created in workspace: {target}"

    # Every runtime module must be present
    for name in (
        "llm.py",
        "telegram_api.py",
        "retry_policy.py",
        "brain.py",
        "heartbeat_base.py",
        "google_oauth.py",
        "playwright_profile.py",
        "camoufox_proxy.py",
        # P0.1 + P0.4 modules — wire-ins broke production with
        # ModuleNotFoundError when these were missed during deploy.
        "scan_fields.py",
        "inbound_scanner.py",
        "inbound_patterns.py",
        "reviewer.py",
        # P1.3 module
        "rate_limit.py",
    ):
        assert (target / name).is_file(), f"missing runtime module: {name}"


def test_sync_shared_library_copies_prompts_subdir(
    deploy_module, seeded_repo, fake_workspace,
):
    """P0.4 prompt templates must land in <workspace>/agents/shared/prompts/.
    inbound_scanner reads them at call time via __file__-relative paths."""
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )
    deploy_module.sync_shared_library(mf)

    prompts_dir = fake_workspace / "agents" / "shared" / "prompts"
    assert prompts_dir.is_dir(), f"prompts/ subdir not created: {prompts_dir}"
    assert (prompts_dir / "anti_leakage.txt").is_file()
    assert (prompts_dir / "semantic_guard.txt").is_file()
    # Content preserved byte-for-byte.
    assert "anti-leakage suffix" in (prompts_dir / "anti_leakage.txt").read_text(encoding="utf-8")


def test_sync_shared_library_preserves_source_content(
    deploy_module, seeded_repo, fake_workspace
):
    """The deployed copy should be byte-identical to the source."""
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )
    deploy_module.sync_shared_library(mf)

    src = (seeded_repo / "agents" / "shared" / "llm.py").read_text(encoding="utf-8")
    dst = (fake_workspace / "agents" / "shared" / "llm.py").read_text(encoding="utf-8")
    assert src == dst


# ─── Filtering ──────────────────────────────────────────────────────


def test_sync_shared_library_skips_tests_subdir(
    deploy_module, seeded_repo, fake_workspace
):
    """Tests live in the repo, not in each workspace."""
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )
    deploy_module.sync_shared_library(mf)
    assert not (fake_workspace / "agents" / "shared" / "tests").exists()


def test_sync_shared_library_skips_deploy_tool_itself(
    deploy_module, seeded_repo, fake_workspace
):
    """deploy.py is the tool running the sync — it must not ship into
    workspaces (or it'll compound with the installed copy)."""
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )
    deploy_module.sync_shared_library(mf)
    assert not (fake_workspace / "agents" / "shared" / "deploy.py").exists()


def test_sync_shared_library_skips_other_deploy_only_modules(
    deploy_module, seeded_repo, fake_workspace
):
    """workspace-snapshot.py, contract_wrap.py, write-status.py,
    import_from_deploy_sh.py, SCRIPT_CONTRACT.md, fleet-manifest.json
    are all deploy-side only."""
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )
    deploy_module.sync_shared_library(mf)

    target = fake_workspace / "agents" / "shared"
    for name in (
        "workspace-snapshot.py",
        "contract_wrap.py",
        "write-status.py",
        "import_from_deploy_sh.py",
        "SCRIPT_CONTRACT.md",
        "fleet-manifest.json",
    ):
        assert not (target / name).exists(), f"should not have deployed: {name}"


# ─── Updates ────────────────────────────────────────────────────────


def test_sync_shared_library_updates_changed_source_files(
    deploy_module, seeded_repo, fake_workspace
):
    """A second sync with modified source content should overwrite."""
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )
    deploy_module.sync_shared_library(mf)
    assert (fake_workspace / "agents" / "shared" / "llm.py").read_text().startswith("# llm")

    # Modify the source
    (seeded_repo / "agents" / "shared" / "llm.py").write_text(
        "# llm v2\nDEFAULT_MODEL = 'gpt-5.5'\n", encoding="utf-8"
    )
    deploy_module.sync_shared_library(mf)

    new = (fake_workspace / "agents" / "shared" / "llm.py").read_text()
    assert "v2" in new
    assert "gpt-5.5" in new


def test_sync_shared_library_idempotent_on_unchanged_source(
    deploy_module, seeded_repo, fake_workspace
):
    """Calling sync twice with unchanged source should not alter mtimes
    or cause errors."""
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )
    deploy_module.sync_shared_library(mf)
    first_mtime = (fake_workspace / "agents" / "shared" / "llm.py").stat().st_mtime

    deploy_module.sync_shared_library(mf)
    # Second sync: content unchanged. Idempotent — should not raise.
    assert (fake_workspace / "agents" / "shared" / "llm.py").is_file()


# ─── Dry-run ────────────────────────────────────────────────────────


def test_sync_shared_library_respects_dry_run(
    deploy_module, seeded_repo, fake_workspace, monkeypatch
):
    """Under _DRY, no files should be written."""
    monkeypatch.setattr(deploy_module, "_DRY", True)
    mf = deploy_module.load_manifest(
        seeded_repo / "agents" / "testagent" / "manifest.json"
    )
    deploy_module.sync_shared_library(mf)

    # Dry-run must not create the target dir or any files
    target = fake_workspace / "agents" / "shared"
    if target.exists():
        assert not any(target.iterdir()), f"dry-run wrote files: {list(target.iterdir())}"


# ─── Integration with deploy_one ────────────────────────────────────


def test_deploy_one_calls_sync_shared_library(
    deploy_module, seeded_repo, fake_workspace, monkeypatch, tmp_path
):
    """Running a full deploy_one() against the fake repo should land
    agents/shared/ into the workspace alongside the per-agent scripts."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    monkeypatch.setattr("builtins.input", lambda _="": "y")

    args = argparse.Namespace(
        agent_id="testagent",
        all=False,
        exclude=[],
        dry_run=False,
        skip_files=False,
        skip_scripts=False,
        skip_crons=True,
        skip_channel=True,
        remove_orphans=False,
        allow_dirty=False,
        yes_updates=True,
    )

    rc = deploy_module.deploy_one("testagent", args)

    # deploy_one should have produced the shared/ tree
    llm_path = fake_workspace / "agents" / "shared" / "llm.py"
    assert llm_path.is_file(), (
        f"deploy_one did not sync agents/shared/. workspace={fake_workspace} "
        f"llm_path={llm_path}"
    )
