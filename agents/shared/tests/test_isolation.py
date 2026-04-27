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


def test_bwrap_command_binds_brain_facts_rw(tmp_path: Path) -> None:
    """brain/facts/ must be RW-bound so fact miners running under bwrap
    can write to brain/facts/YYYY-MM.md. Pre-widening (2026-04-20) the
    default profile RO-bound all of brain/ except brain/agents/<id>/,
    which silently broke fact miners with EROFS and was the reason
    `daily-refresh` was hitting EROFS on brain/people/*.md."""
    brain = tmp_path / "brain"
    (brain / "facts").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    facts_dir = brain / "facts"
    assert _has_triple(cmd, "--bind-try", str(facts_dir), str(facts_dir))


def test_bwrap_command_binds_brain_people_rw(tmp_path: Path) -> None:
    """brain/people/ must be RW-bound so miners' recent-observations
    appends land. Regression for the 2026-04-18 EROFS on people/*.md."""
    brain = tmp_path / "brain"
    (brain / "people").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    people_dir = brain / "people"
    assert _has_triple(cmd, "--bind-try", str(people_dir), str(people_dir))


def test_bwrap_command_binds_brain_commitments_and_queues_rw(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    (brain / "commitments").mkdir(parents=True)
    (brain / "queues").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    for sub in ("commitments", "queues"):
        p = brain / sub
        assert _has_triple(cmd, "--bind-try", str(p), str(p)), (
            f"brain/{sub}/ must be RW-bound"
        )


def test_bwrap_command_binds_calendar_brain_ro(tmp_path: Path, monkeypatch) -> None:
    """``~/.clawford/calendar-brain/`` must be RO-bound so every
    bwrap'd brain-reading cron (morning-meeting-brief,
    morning-briefing, pre-meeting-alert, post-meeting-scan, etc.)
    can resolve the shared event cache inside its namespace.

    Regression target: 2026-04-23 — after the Phase 3 brain-only
    switch, the first morning's cron run under bwrap silently
    returned zero events from ``read_brain_if_fresh`` because the
    brain path was outside any bind the isolation profile provided.
    Both agents' morning briefs rendered 'No meetings today' despite
    a fully-populated brain."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain_dir = fake_home / ".clawford" / "calendar-brain"
    brain_dir.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cmd = isolation.bwrap_command(
        agent_id="meetings-coach", workspace=workspace, brain_root=brain,
    )
    assert _has_triple(
        cmd, "--ro-bind-try", str(brain_dir), str(brain_dir)
    )


def test_bwrap_command_binds_brain_status_rw(tmp_path: Path) -> None:
    """brain/status/ must be RW-bound. calendar-brain-build.py
    double-writes ``status/calendar-index.json`` atomically (tmpfile +
    os.replace) during the migration from the legacy
    calendar-index-build.py; without this bind the tmpfile write hits
    EROFS. See ``agents/shared/tests/bwrap_write_surfaces.md``."""
    brain = tmp_path / "brain"
    (brain / "status").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    status_dir = brain / "status"
    assert _has_triple(cmd, "--bind-try", str(status_dir), str(status_dir))


def test_bwrap_command_binds_brain_tasks_rw(tmp_path: Path) -> None:
    """brain/tasks/ must be RW-bound. brain_tasks.py's QUEUE_RELPATH
    resolves to ``tasks/queue.md`` and gcal-tasks-sync (in the bwrap
    allowlist) writes to it."""
    brain = tmp_path / "brain"
    (brain / "tasks").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    tasks_dir = brain / "tasks"
    assert _has_triple(cmd, "--bind-try", str(tasks_dir), str(tasks_dir))


def test_bwrap_command_binds_brain_notes_rw(tmp_path: Path) -> None:
    """brain/notes/ must be RW-bound. brain.py::append_inbox_note
    writes to ``notes/inbox.md`` (append mode); notes-triage.py
    (allowlisted via connector-inbox-triage) mutates the same file
    when marking entries triaged."""
    brain = tmp_path / "brain"
    (brain / "notes").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    notes_dir = brain / "notes"
    assert _has_triple(cmd, "--bind-try", str(notes_dir), str(notes_dir))


def test_bwrap_command_binds_fleet_health_json_rw(tmp_path: Path) -> None:
    """brain/fleet-health.json must be RW-bound at file-level.
    ops/scripts/fleet-health.py writes it; any future agent cron that
    patches it (partial status annotations) would hit EROFS without
    this. File-level ``--bind-try`` — defensive since the file may
    not exist on fresh deploys."""
    brain = tmp_path / "brain"
    brain.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    fh = brain / "fleet-health.json"
    assert _has_triple(cmd, "--bind-try", str(fh), str(fh))


def test_bwrap_command_brain_memory_stays_read_only(tmp_path: Path) -> None:
    """The RW whitelist is explicit: facts, people, commitments,
    queues, status, tasks, notes + fleet-health.json (file-level).
    Everything else under brain/ stays RO. brain/memory/ — where
    MEMORY.md and long-term context live — must NOT flip to RW; an
    agent's writes go to brain/agents/<agent_id>/MEMORY.md, not the
    fleet-level memory root."""
    brain = tmp_path / "brain"
    memory = brain / "memory"
    memory.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    assert not _has_triple(cmd, "--bind", str(memory), str(memory)), (
        "brain/memory/ must remain RO"
    )
    assert not _has_triple(cmd, "--bind-try", str(memory), str(memory)), (
        "brain/memory/ must not appear in the RW whitelist"
    )


def test_bwrap_command_binds_operator_config_readonly() -> None:
    """Operator identity at ~/.clawford/operator.json must be
    RO-bound; agents/shared/operator.py::load_operator fails with
    'Operator config not found' otherwise. Using --ro-bind-try so CI
    and fresh dev machines (no operator.json) don't crash."""
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=Path("/tmp/x"),
    )
    operator_json = str(Path.home() / ".clawford" / "operator.json")
    assert _has_triple(cmd, "--ro-bind-try", operator_json, operator_json)


def test_bwrap_command_binds_codex_auth_readonly() -> None:
    """Codex OAuth token lives at ~/.codex/auth.json; bwrap must bind
    it RO so agents/shared/llm.py::infer can read it. Missing pre-
    widening (2026-04-20); every bwrap'd LLM call failed with
    'auth.json not found' until this bind was added. Using --ro-bind-try
    so the flag is safe on a machine where Codex isn't installed
    (CI, fresh dev laptop)."""
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=Path("/tmp/x"),
    )
    codex = str(Path.home() / ".codex")
    assert _has_triple(cmd, "--ro-bind-try", codex, codex)


def test_bwrap_command_provides_tmpfs_for_dev_shm() -> None:
    """SysV shared memory lives at /dev/shm. Camoufox/Playwright/Firefox
    allocate shm segments there for IPC between the browser master
    process and its workers. Without a /dev/shm mount inside the
    namespace, the browser crashes immediately on launch. Per-invocation
    tmpfs keeps shm segments isolated and auto-cleans on namespace
    exit. Regression cover for the 'DO NOT add browser-driven crons'
    pre-widening allowlist comment."""
    cmd = isolation.bwrap_command(
        agent_id="x", workspace=Path("/tmp/x"),
    )
    assert _has_pair(cmd, "--tmpfs", "/dev/shm")


def test_bwrap_command_agents_dir_is_read_only(tmp_path: Path) -> None:
    """Post status.md retirement, the only writes inside <brain>/agents/
    come from each agent's own subdir (MEMORY.md via memory_writer,
    per-agent config edits). No agent writes sibling files at the
    <brain>/agents/ level anymore, so the whole directory inherits
    the brain-root RO bind. The per-agent subdir still flips to RW
    via the separate --bind <brain>/agents/<agent_id> below.

    Regression guard for the 2026-04-17 cleanup: earlier profiles
    RW-bound the full <brain>/agents/ directory so heartbeat_base
    could atomic-rename-write sibling .status.md files. That write
    path is gone; the RW bind would just be residual attack surface
    (a compromised agent could overwrite another agent's .status.md
    if it still existed)."""
    brain = tmp_path / "brain"
    agents_dir = brain / "agents"
    agents_dir.mkdir(parents=True)
    workspace = tmp_path / "shopping-workspace"
    workspace.mkdir()
    cmd = isolation.bwrap_command(
        agent_id="shopping", workspace=workspace, brain_root=brain,
    )
    # No explicit RW --bind on the agents/ directory itself — it
    # inherits the brain-root RO bind.
    assert not _has_triple(cmd, "--bind", str(agents_dir), str(agents_dir)), (
        "<brain>/agents/ must NOT be RW-bound post status.md retirement"
    )


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


def test_bwrap_command_binds_camoufox_cache_readonly(
    tmp_path: Path, monkeypatch
) -> None:
    """``~/.cache/camoufox/`` (~1.3 GB browser binary) must be RO-bound
    so bwrap'd Camoufox launches read the cached binary instead of
    forcing the GitHub fetch path on every run.

    Regression target: 2026-04-27 — connector-gmessages-mine alerted
    with ``🐛 gmessages scrape: No matching release found for lin
    x86_64 in the supported range: (>=beta.19, <1)``. The cached
    binary on the host satisfied the range, but the bwrap profile
    didn't expose ``~/.cache/`` so Camoufox's pkgman fell through to
    a GitHub Releases fetch that hit a transient empty/rate-limited
    response and surfaced as 'no matching release.' Binding the
    cache RO removes the fetch path from the hot path entirely.
    Using ``--ro-bind-try`` so CI/dev hosts without a populated
    cache don't crash."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cache_dir = fake_home / ".cache" / "camoufox"
    cache_dir.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cmd = isolation.bwrap_command(
        agent_id="connector", workspace=workspace,
    )
    assert _has_triple(
        cmd, "--ro-bind-try", str(cache_dir), str(cache_dir)
    )


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
