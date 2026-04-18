"""Tests for Safeguard 10's workspace-as-source fallback.

After the 2026-04-13 PII sanitization, real agent config files (SOUL.md,
IDENTITY.md, AGENTS.md, USER.md, MEMORY.md) are gitignored and live
*only* at ``<repo>/agents/<agent>/<file>``. A past ``git clean`` wiped
several of these from the VPS repo dir, which broke the next deploy —
the real files still existed in the deployed workspace, but the deploy
path requires them at the repo-source-dir location.

This fallback restores the repo copies from the workspace automatically,
turning a destructive accident into a transparent self-heal. The deploy
still refuses when *neither* the repo source nor the workspace copy has
the file (genuine first-deploy case).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_PY = REPO_ROOT / "agents" / "shared" / "deploy.py"


@pytest.fixture
def deploy():
    sys.path.insert(0, str(DEPLOY_PY.parent))
    for mod in list(sys.modules):
        if mod.startswith("deploy"):
            del sys.modules[mod]
    spec = importlib.util.spec_from_file_location("deploy_mod", DEPLOY_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _make_manifest(deploy, *, source_dir, workspace_dir, config_names):
    data = {
        "agent_id": "testagent",
        "display_name": "Test",
        "workspace": str(workspace_dir),
        "telegram": {"account": "t", "bot_token_env": "T_BOT_TOKEN"},
        "config_files": [{"src": n} for n in config_names],
        "scripts": ["scripts/heartbeat.py"],
        "state_files": [],
        "crons": [],
    }
    return deploy.load_manifest_from_dict(data, source_dir=source_dir)


def _seed_agent_dir(dir_path, names, *, with_examples=True, with_reals=True, real_body="real"):
    dir_path.mkdir(parents=True, exist_ok=True)
    for n in names:
        if with_examples:
            (dir_path / (n + ".example")).write_text("example", encoding="utf-8")
        if with_reals:
            (dir_path / n).write_text(real_body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Self-heal: missing repo source + present workspace copy → seed repo from workspace
# ---------------------------------------------------------------------------


def test_self_heals_when_workspace_has_real_file(deploy, tmp_path):
    source_dir = tmp_path / "repo" / "agents" / "testagent"
    workspace_dir = tmp_path / "workspace"
    _seed_agent_dir(source_dir, ["SOUL.md"], with_examples=True, with_reals=False)
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "SOUL.md").write_text("real SOUL content from workspace", encoding="utf-8")

    mf = _make_manifest(
        deploy, source_dir=source_dir, workspace_dir=workspace_dir, config_names=["SOUL.md"]
    )
    rc = deploy._ensure_config_sources_present(mf)

    assert rc == 0
    assert (source_dir / "SOUL.md").read_text(encoding="utf-8") == "real SOUL content from workspace"


def test_self_heals_multiple_files_in_one_pass(deploy, tmp_path):
    source_dir = tmp_path / "repo" / "agents" / "testagent"
    workspace_dir = tmp_path / "workspace"
    names = ["SOUL.md", "IDENTITY.md", "AGENTS.md"]
    _seed_agent_dir(source_dir, names, with_examples=True, with_reals=False)
    workspace_dir.mkdir(parents=True)
    for n in names:
        (workspace_dir / n).write_text(f"real {n}", encoding="utf-8")

    mf = _make_manifest(
        deploy, source_dir=source_dir, workspace_dir=workspace_dir, config_names=names
    )
    rc = deploy._ensure_config_sources_present(mf)

    assert rc == 0
    for n in names:
        assert (source_dir / n).read_text(encoding="utf-8") == f"real {n}"


# ---------------------------------------------------------------------------
# Still refuse when workspace copy also missing (genuine first-deploy case)
# ---------------------------------------------------------------------------


def test_still_fails_when_neither_repo_nor_workspace_has_real_file(deploy, tmp_path):
    source_dir = tmp_path / "repo" / "agents" / "testagent"
    workspace_dir = tmp_path / "workspace"
    _seed_agent_dir(source_dir, ["SOUL.md"], with_examples=True, with_reals=False)
    workspace_dir.mkdir(parents=True)  # empty workspace — no SOUL.md to seed from

    mf = _make_manifest(
        deploy, source_dir=source_dir, workspace_dir=workspace_dir, config_names=["SOUL.md"]
    )
    rc = deploy._ensure_config_sources_present(mf)

    assert rc == 5
    assert not (source_dir / "SOUL.md").exists()


# ---------------------------------------------------------------------------
# Sentinel-bearing workspace copy must NOT be used as a source of truth
# ---------------------------------------------------------------------------


def test_refuses_to_seed_from_workspace_bootstrap_sentinel(deploy, tmp_path):
    """A sentinel-bearing file is an unedited scaffold. Seeding the repo
    from it would re-propagate the unedited scaffold and re-trigger the
    next sentinel check — just fail cleanly the first time."""
    source_dir = tmp_path / "repo" / "agents" / "testagent"
    workspace_dir = tmp_path / "workspace"
    _seed_agent_dir(source_dir, ["SOUL.md"], with_examples=True, with_reals=False)
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "SOUL.md").write_text(
        f"<!-- {deploy.BOOTSTRAP_SENTINEL} -->\n\n# SOUL\n",
        encoding="utf-8",
    )

    mf = _make_manifest(
        deploy, source_dir=source_dir, workspace_dir=workspace_dir, config_names=["SOUL.md"]
    )
    rc = deploy._ensure_config_sources_present(mf)

    assert rc == 5
    assert not (source_dir / "SOUL.md").exists()


# ---------------------------------------------------------------------------
# No-op when repo copy already present
# ---------------------------------------------------------------------------


def test_no_op_when_repo_source_already_present(deploy, tmp_path):
    source_dir = tmp_path / "repo" / "agents" / "testagent"
    workspace_dir = tmp_path / "workspace"
    _seed_agent_dir(source_dir, ["SOUL.md"], with_examples=True, with_reals=True, real_body="pristine")
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "SOUL.md").write_text("stale workspace copy", encoding="utf-8")

    mf = _make_manifest(
        deploy, source_dir=source_dir, workspace_dir=workspace_dir, config_names=["SOUL.md"]
    )
    rc = deploy._ensure_config_sources_present(mf)

    assert rc == 0
    # Repo copy unchanged; workspace copy NOT pulled over the top
    assert (source_dir / "SOUL.md").read_text(encoding="utf-8") == "pristine"
