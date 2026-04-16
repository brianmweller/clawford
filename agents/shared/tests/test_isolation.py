"""P1.2 — bubblewrap-based inter-agent isolation.

Tests for agents/shared/isolation.py. The module composes a `bwrap`
argv prefix that wraps an agent script's subprocess invocation so
the script can't read another agent's workspace files at the OS
layer.

Mr Fixit explicitly opts out at the resolve_isolation_mode boundary
— per feedback_fixit_bubblewrap_exempt.md, the fleet operator role
breaks under restrictive isolation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import isolation  # type: ignore


# ---------------------------------------------------------------------------
# resolve_isolation_mode — including Mr Fixit's exemption
# ---------------------------------------------------------------------------


def test_default_when_manifest_silent() -> None:
    assert isolation.resolve_isolation_mode(
        agent_id="shopping", manifest_mode=None,
    ) == "none"


def test_explicit_bwrap_mode_resolves() -> None:
    assert isolation.resolve_isolation_mode(
        agent_id="shopping", manifest_mode="bwrap",
    ) == "bwrap"


def test_invalid_mode_falls_back_to_default() -> None:
    """A typo in the manifest shouldn't sneak the agent into a
    surprising mode — fall back to the conservative default."""
    assert isolation.resolve_isolation_mode(
        agent_id="shopping", manifest_mode="loud-and-proud",
    ) == "none"


def test_fix_it_is_always_isolation_none() -> None:
    """fix-it MUST run unrestricted regardless of what its manifest
    says — Mr Fixit reads every other workspace + the brain + runs
    validate.py + proposes remediations across the fleet."""
    for attempted in ("bwrap", "BWRAP", "  bwrap  ", "anything", None):
        assert isolation.resolve_isolation_mode(
            agent_id="fix-it", manifest_mode=attempted,
        ) == "none", f"fix-it must NEVER be isolated; manifest_mode={attempted!r}"


def test_other_agents_are_not_in_exempt_set() -> None:
    """Sanity check: only fix-it is exempt. If we ever add another
    operator agent we'll add it here explicitly."""
    assert "fix-it" in isolation.ISOLATION_EXEMPT_AGENTS
    assert "shopping" not in isolation.ISOLATION_EXEMPT_AGENTS
    assert "news-digest" not in isolation.ISOLATION_EXEMPT_AGENTS
    assert "family-calendar" not in isolation.ISOLATION_EXEMPT_AGENTS
    assert "meetings-coach" not in isolation.ISOLATION_EXEMPT_AGENTS
    assert "connector" not in isolation.ISOLATION_EXEMPT_AGENTS


# ---------------------------------------------------------------------------
# bwrap_command — argv shape
# ---------------------------------------------------------------------------


def test_bwrap_command_starts_with_bwrap_binary() -> None:
    cmd = isolation.bwrap_command(
        agent_id="shopping",
        workspace=Path("/tmp/fake-workspace"),
    )
    assert cmd[0] == "bwrap"


def test_bwrap_command_uses_custom_binary_path() -> None:
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=Path("/tmp/x"), bwrap_bin="/usr/local/bin/bwrap",
    )
    assert cmd[0] == "/usr/local/bin/bwrap"


def test_bwrap_command_keeps_network() -> None:
    """--share-net is mandatory: agents need Telegram + LLM + browser
    network access. The chapter explicitly calls out theoretical
    sandboxing that breaks browser automation as the anti-pattern."""
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=Path("/tmp/x"),
    )
    assert "--share-net" in cmd


def test_bwrap_command_does_not_unshare_pid_or_ipc() -> None:
    """Camoufox/Firefox use SysV shared memory; --unshare-pid /
    --unshare-ipc would crash them."""
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=Path("/tmp/x"),
    )
    assert "--unshare-pid" not in cmd
    assert "--unshare-ipc" not in cmd


def test_bwrap_command_dies_with_parent() -> None:
    """A wrapper crash shouldn't leave a sandboxed orphan process
    chewing CPU."""
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=Path("/tmp/x"),
    )
    assert "--die-with-parent" in cmd


def test_bwrap_command_provides_tmpfs_for_tmp() -> None:
    """Per-invocation /tmp + /var/tmp keep Playwright profile dirs
    isolated and auto-clean when the namespace exits."""
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=Path("/tmp/x"),
    )
    assert _has_pair(cmd, "--tmpfs", "/tmp")
    assert _has_pair(cmd, "--tmpfs", "/var/tmp")


def test_bwrap_command_binds_workspace_rw(tmp_path: Path) -> None:
    workspace = tmp_path / "shopping-workspace"
    workspace.mkdir()
    cmd = isolation.bwrap_command(agent_id="shopping", workspace=workspace)
    # `--bind` (not --ro-bind) → workspace is read-write inside the namespace.
    assert _has_triple(cmd, "--bind", str(workspace), str(workspace))


def test_bwrap_command_chdir_into_workspace(tmp_path: Path) -> None:
    """Working dir inside the namespace is the workspace so cwd-sensitive
    code (relative imports, Path('.').resolve()) lands in the right
    place."""
    workspace = tmp_path / "shopping-workspace"
    workspace.mkdir()
    cmd = isolation.bwrap_command(agent_id="shopping", workspace=workspace)
    assert _has_pair(cmd, "--chdir", str(workspace))


def test_bwrap_command_brain_root_is_read_only(tmp_path: Path) -> None:
    """Other agents' brain dirs are read-only — agents can read the
    fleet brain but only write to their own per-agent subdir."""
    brain = tmp_path / "brain"
    brain.mkdir()
    workspace = tmp_path / "shopping-workspace"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    assert _has_triple(cmd, "--ro-bind", str(brain), str(brain))


def test_bwrap_command_own_brain_subdir_is_read_write(tmp_path: Path) -> None:
    """The agent's own brain subdir flips to RW so memory_writer can
    append to MEMORY.md from inside the namespace."""
    brain = tmp_path / "brain"
    agent_brain = brain / "agents" / "shopping"
    agent_brain.mkdir(parents=True)
    workspace = tmp_path / "shopping-workspace"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    assert _has_triple(cmd, "--bind", str(agent_brain), str(agent_brain))


def test_bwrap_command_agents_dir_is_read_write(tmp_path: Path) -> None:
    """heartbeat_base uses an atomic-rename pattern: writes
    `<id>.status.md.tmp` (sibling of target), then os.replace(). The
    .tmp file lands in <brain>/agents/, so that directory must be
    RW-bound — bwrap can't make a single file writable inside an RO
    parent. The acceptable tradeoff: agents under bwrap can overwrite
    OTHER agents' .status.md files (non-secret monitoring data); the
    real isolation goal (protecting workspace cache with tokens) is
    preserved because per-agent brain subdirs are still RO unless
    explicitly listed as the agent's own."""
    brain = tmp_path / "brain"
    agents_dir = brain / "agents"
    agents_dir.mkdir(parents=True)
    workspace = tmp_path / "shopping-workspace"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    assert _has_triple(cmd, "--bind", str(agents_dir), str(agents_dir))


def test_bwrap_command_includes_user_local_bin(tmp_path: Path) -> None:
    """install-host-deps.sh installs pip packages to ~/.local/ via
    `pip install --user --break-system-packages`. The default profile
    RO-binds it so google-auth, pip-audit, camoufox, and every other
    user-installed dep resolves inside the namespace. Regression
    cover for a 2026-04-16 smoke test that crashed gcal-fetch with
    'google-auth not installed' even though it was installed on the
    host."""
    workspace = tmp_path / "shopping-workspace"
    workspace.mkdir()
    cmd = isolation.bwrap_command(agent_id="shopping", workspace=workspace)
    user_local = str(Path.home() / ".local")
    if not Path(user_local).is_dir():
        pytest.skip(f"{user_local} not present on this machine")
    assert _has_triple(cmd, "--ro-bind", user_local, user_local)


def test_bwrap_command_repo_root_is_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = tmp_path / "shopping-workspace"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, repo_root=repo,
    )
    assert _has_triple(cmd, "--ro-bind", str(repo), str(repo))


def test_bwrap_command_omits_bindings_for_missing_paths(tmp_path: Path) -> None:
    """Don't bind paths that don't exist on the host — bwrap would
    error out otherwise. Tests on a stripped container shouldn't
    crash because /lib64 is missing."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    nonexistent_brain = tmp_path / "nope-brain"
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=workspace, brain_root=nonexistent_brain,
    )
    # No --ro-bind for the missing brain.
    assert str(nonexistent_brain) not in cmd


# ---------------------------------------------------------------------------
# is_bwrap_available — tests bypass when bwrap isn't installed
# ---------------------------------------------------------------------------


def test_is_bwrap_available_returns_bool() -> None:
    """Just check the contract — actual availability depends on
    test environment."""
    result = isolation.is_bwrap_available()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_pair(cmd: list[str], flag: str, value: str) -> bool:
    """True if cmd contains [..., flag, value, ...] in that order."""
    for i, x in enumerate(cmd):
        if x == flag and i + 1 < len(cmd) and cmd[i + 1] == value:
            return True
    return False


def _has_triple(cmd: list[str], flag: str, src: str, dst: str) -> bool:
    """True if cmd contains [..., flag, src, dst, ...] in that order."""
    for i, x in enumerate(cmd):
        if (
            x == flag
            and i + 2 < len(cmd)
            and cmd[i + 1] == src
            and cmd[i + 2] == dst
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# contract_wrap._build_subprocess_argv — env-var-driven wire-in
# ---------------------------------------------------------------------------


import contract_wrap  # type: ignore  # noqa: E402


def test_subprocess_argv_unwrapped_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(contract_wrap.ISOLATION_MODE_ENV_VAR, raising=False)
    target = tmp_path / "scripts" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('ok')\n", encoding="utf-8")

    argv = contract_wrap._build_subprocess_argv(
        agent_id="shopping", target_path=target, target_args=[],
    )
    # First element is the python interpreter, NOT bwrap.
    assert "bwrap" not in argv[0]


def test_subprocess_argv_wraps_when_env_var_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With CLAWFORD_ISOLATION_MODE=bwrap and bwrap available, the
    subprocess argv begins with bwrap …."""
    monkeypatch.setenv(contract_wrap.ISOLATION_MODE_ENV_VAR, "bwrap")
    monkeypatch.setattr("isolation.is_bwrap_available", lambda: True)
    target = tmp_path / "scripts" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('ok')\n", encoding="utf-8")

    argv = contract_wrap._build_subprocess_argv(
        agent_id="shopping", target_path=target, target_args=[],
    )
    assert argv[0] == "bwrap"
    # The unwrapped python invocation is still in there as the suffix.
    py_idx = argv.index(str(target))
    assert py_idx > 0
    # `python3 <target>` immediately precedes (well, target is preceded
    # by sys.executable).
    assert argv[py_idx - 1].endswith("python3") or "python" in argv[py_idx - 1]


def test_subprocess_argv_falls_back_when_bwrap_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env requested isolation but bwrap isn't installed → run
    unwrapped + log a warning, never block."""
    monkeypatch.setenv(contract_wrap.ISOLATION_MODE_ENV_VAR, "bwrap")
    monkeypatch.setattr("isolation.is_bwrap_available", lambda: False)
    target = tmp_path / "scripts" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('ok')\n", encoding="utf-8")

    argv = contract_wrap._build_subprocess_argv(
        agent_id="shopping", target_path=target, target_args=[],
    )
    assert "bwrap" not in argv[0]


def test_subprocess_argv_fix_it_never_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with CLAWFORD_ISOLATION_MODE=bwrap and bwrap available,
    Mr Fixit runs unwrapped — fleet operator must keep visibility into
    every workspace + the brain."""
    monkeypatch.setenv(contract_wrap.ISOLATION_MODE_ENV_VAR, "bwrap")
    monkeypatch.setattr("isolation.is_bwrap_available", lambda: True)
    target = tmp_path / "scripts" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('ok')\n", encoding="utf-8")

    argv = contract_wrap._build_subprocess_argv(
        agent_id="fix-it", target_path=target, target_args=[],
    )
    assert "bwrap" not in argv[0], (
        "Mr Fixit must NEVER be wrapped — see "
        "feedback_fixit_bubblewrap_exempt.md"
    )
