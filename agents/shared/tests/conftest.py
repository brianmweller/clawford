"""Shared fixtures for deploy.py safeguard tests.

Each test gets its own fake workspace and fake source git repo. Post-Phase-7,
deploy.py no longer defines any OpenClaw helpers so there's nothing to stub —
the fixture just imports the module and redirects REPO_ROOT.
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
def fake_brain_root(tmp_path: Path, monkeypatch) -> Path:
    """Set up a fake Dropbox brain and point modules at it via
    CLAWFORD_BRAIN_DROPBOX_ROOT. Returns the brain root path."""
    brain_root = tmp_path / "fake-brain"
    brain_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))
    return brain_root


@pytest.fixture
def fake_source_repo(tmp_path: Path, fake_brain_root: Path) -> Path:
    """A tiny git repo that looks like the Clawford layout.

    manifest.json still lives repo-side (hasn't been migrated).
    Config docs (SOUL/IDENTITY/etc.) live in fake_brain_root.

    Structure:
        <tmp>/source-repo/
          .git/
          agents/testagent/
            manifest.json
            scripts/hello.py
            scripts/heartbeat.py

        <tmp>/fake-brain/agents/testagent/
          SOUL.md
          IDENTITY.md
    """
    repo = tmp_path / "source-repo"
    agent_dir = repo / "agents" / "testagent"
    (agent_dir / "scripts").mkdir(parents=True)

    (agent_dir / "scripts" / "hello.py").write_text(
        "print('hello v1')\n", encoding="utf-8"
    )
    (agent_dir / "scripts" / "heartbeat.py").write_text(
        "print('{\"status\": \"ok\"}')\n", encoding="utf-8"
    )

    # Dropbox brain: config docs
    brain_agent_dir = fake_brain_root / "agents" / "testagent"
    brain_agent_dir.mkdir(parents=True)
    (brain_agent_dir / "SOUL.md").write_text("# testagent soul\n", encoding="utf-8")
    (brain_agent_dir / "IDENTITY.md").write_text("# testagent identity\n", encoding="utf-8")

    manifest = {
        "agent_id": "testagent",
        "display_name": "Test Agent",
        "workspace": str(tmp_path / "fake-workspace"),
        "status_file": str(fake_brain_root / "testagent.status.md"),
        "telegram": {"account": "testagent", "bot_token_env": "TEST_BOT_TOKEN"},
        "config_files": [],
        "scripts": ["scripts/hello.py", "scripts/heartbeat.py"],
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
    """Import deploy.py fresh and point REPO_ROOT at the fake repo.

    Post-Phase-7 deploy.py has no OpenClaw helpers to stub out; the fixture
    just imports the module clean.
    """
    # Force reimport to pick up any code changes in this session.
    for mod in list(sys.modules):
        if mod == "deploy" or mod.startswith("deploy."):
            del sys.modules[mod]
    import deploy  # type: ignore
    monkeypatch.setattr(deploy, "REPO_ROOT", fake_source_repo)
    return deploy
