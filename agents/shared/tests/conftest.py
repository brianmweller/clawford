"""Shared fixtures for deploy.py safeguard tests.

Each test gets its own fake workspace, fake source git repo, and can stub
out the `oc` wrapper so we never actually talk to a Docker daemon.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Make deploy.py importable as a module.
SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _run(cmd, cwd):
    """Run a subprocess command, fail the test if it errors."""
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        f"{' '.join(cmd)} failed: {result.stderr or result.stdout}"
    )
    return result


@pytest.fixture
def fake_source_repo(tmp_path: Path) -> Path:
    """A tiny git repo that looks like the Clawford layout, with one agent.

    Structure:
        <tmp>/source-repo/
          .git/
          agents/
            testagent/
              SOUL.md
              TOOLS.md
              scripts/
                hello.py
              manifest.json

    The repo has exactly one commit so HEAD is defined and `git status`
    reports clean.
    """
    repo = tmp_path / "source-repo"
    agent_dir = repo / "agents" / "testagent"
    (agent_dir / "scripts").mkdir(parents=True)

    (agent_dir / "SOUL.md").write_text("# testagent soul\n", encoding="utf-8")
    (agent_dir / "TOOLS.md").write_text("# testagent tools\n", encoding="utf-8")
    (agent_dir / "scripts" / "hello.py").write_text(
        "print('hello v1')\n", encoding="utf-8"
    )

    manifest = {
        "agent_id": "testagent",
        "display_name": "Test Agent",
        "workspace": str(tmp_path / "fake-workspace"),
        "status_file": str(tmp_path / "fake-brain" / "testagent.status.md"),
        "telegram": {"account": "testagent", "bot_token_env": "TEST_BOT_TOKEN"},
        "config_files": [
            {"src": "SOUL.md", "immutable": False},
            {"src": "TOOLS.md"},
        ],
        "scripts": ["scripts/hello.py"],
        "state_files": [],
        "approvals": {"allowlist": []},
        "crons": [],
    }
    (agent_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "test"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "initial"], cwd=repo)
    return repo


@pytest.fixture
def fake_workspace(tmp_path: Path) -> Path:
    """An empty workspace directory the deploy tool will populate."""
    ws = tmp_path / "fake-workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def prepopulated_workspace(fake_workspace: Path) -> Path:
    """A workspace that already has some content from a hypothetical prior
    deploy — so UPDATE/BACKUP paths can be exercised."""
    (fake_workspace / "SOUL.md").write_text(
        "# testagent soul PRIOR\n", encoding="utf-8"
    )
    (fake_workspace / "TOOLS.md").write_text(
        "# testagent tools PRIOR\n", encoding="utf-8"
    )
    (fake_workspace / "scripts").mkdir(exist_ok=True)
    (fake_workspace / "scripts" / "hello.py").write_text(
        "print('hello v0-PRIOR')\n", encoding="utf-8"
    )
    return fake_workspace


@pytest.fixture
def deploy_module(fake_source_repo: Path, monkeypatch):
    """Import deploy.py fresh and point REPO_ROOT at the fake repo."""
    # Force reimport to pick up any code changes in this session.
    for mod in list(sys.modules):
        if mod == "deploy" or mod.startswith("deploy."):
            del sys.modules[mod]
    import deploy  # type: ignore
    monkeypatch.setattr(deploy, "REPO_ROOT", fake_source_repo)
    # Stub out the oc() wrapper so we never call docker.
    def fake_oc(*args, **kwargs):
        result = subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return result
    monkeypatch.setattr(deploy, "oc", fake_oc)
    def fake_oc_json(*args, **kwargs):
        # Route by subcommand so the config-validate safety gate (Safeguard 7)
        # passes in fixture setup without each test having to re-mock it.
        if args[:2] == ("config", "validate"):
            return {"valid": True, "path": "/fake/openclaw.json"}
        return {"jobs": []}
    monkeypatch.setattr(deploy, "oc_json", fake_oc_json)
    return deploy
