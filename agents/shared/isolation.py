"""isolation — bubblewrap-based inter-agent sandboxing (P1.2).

Each cron-invoked script can opt into running inside an unprivileged
user namespace so a compromised agent can't read another agent's
workspace files at the OS layer. Closes the "no sandboxing between
agents" gap that the security chapter explicitly calls out as
unaddressed.

Per-agent opt-in via manifest.json `isolation` field:

    "isolation": "bwrap"  → wrap subprocess in `bwrap` with the
                            agent profile (default profile masks
                            every other workspace + only RW-binds
                            this agent's own workspace and brain
                            subdir)
    "isolation": "none"   → run unwrapped (back-compat default)

Mr Fixit MUST keep "none". He's the fleet operator and reads every
other agent's brain + workspace + runs validate.py + proposes
remediations across the fleet. A locked-down profile breaks all of
that. See feedback_fixit_bubblewrap_exempt.md.

bwrap availability:
  - On the VPS, installed via apt (ops/scripts/install-host-system-
    deps.sh) and baked into ops/Dockerfile for parity.
  - On a developer laptop, bwrap is Linux-only. Tests bypass via the
    is_bwrap_available() check.

Browser-automation note:
  - Default profile keeps --share-net (Telegram, LLM, browser
    network access all work).
  - Default profile does NOT use --unshare-pid or --unshare-ipc
    because Camoufox/Firefox use SysV shared-memory and would crash.
    The chapter calls out "theoretical sandboxing that breaks browser
    automation" as the anti-pattern to avoid.
  - /tmp is a tmpfs per invocation — Playwright's per-run profile
    dirs land there and clean up automatically when the namespace
    exits.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

# Mode names accepted in manifest.json's `isolation` field.
ISOLATION_NONE = "none"
ISOLATION_BWRAP = "bwrap"
ALLOWED_ISOLATION_MODES = (ISOLATION_NONE, ISOLATION_BWRAP)
DEFAULT_ISOLATION = ISOLATION_NONE

# Agents that are NEVER allowed to use restricted isolation. Mr Fixit
# is the fleet operator — he reads every workspace, every brain dir,
# proposes remediations against other agents. A masked filesystem view
# would silently break all of those. Hard-coded here so a manifest
# typo can't sneak fix-it into a broken-but-running state.
ISOLATION_EXEMPT_AGENTS = frozenset({"fix-it"})


def is_bwrap_available() -> bool:
    """Return True iff `bwrap` is on PATH. Used by tests and runtime
    fall-throughs to skip isolation when the binary is missing
    (devcontainer / non-Linux laptop)."""
    return shutil.which("bwrap") is not None


def _expand(p: str) -> str:
    return os.path.expanduser(p)


def bwrap_command(
    *,
    agent_id: str,
    workspace: Path,
    brain_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    bwrap_bin: str = "bwrap",
) -> list[str]:
    """Return the `bwrap …` argv prefix that wraps an agent script.

    Caller appends the actual `python3 <script.py>` invocation:

        prefix = bwrap_command(agent_id=..., workspace=...)
        subprocess.run(prefix + ["python3", str(target)], ...)

    The default profile:
      - Masks the rest of the filesystem (no implicit access to
        other agents' workspaces or operator home dir)
      - RO-binds /usr, /etc, /lib, /lib64, /bin, /sbin (system Python
        + libs + DNS + certs)
      - RO-binds the repo (so deploy-time copies of shared-lib
        modules, prompts, fixtures resolve)
      - RO-binds the brain root + per-agent-subdir RW + RW-whitelists
        brain/{facts,people,commitments,queues} for shared writes
      - RO-binds ~/.codex (Codex OAuth token)
      - RW-binds the agent's own workspace
      - tmpfs /tmp + /var/tmp for per-invocation scratch
      - tmpfs /dev/shm for browser SysV shared-memory IPC
      - --share-net keeps the network stack so Telegram + LLM +
        browser calls work
      - --die-with-parent so a wrapper crash doesn't leave zombies
      - No --unshare-pid / --unshare-ipc — those break Camoufox.
    """
    workspace = Path(_expand(str(workspace))).resolve()
    cmd: list[str] = [
        bwrap_bin,
        "--die-with-parent",
        "--share-net",
        # /proc + minimal /dev — required for Python, subprocess,
        # tempfile, etc.
        "--proc", "/proc",
        "--dev", "/dev",
        # Scratch space — per-invocation tmpfs avoids cross-cron leakage.
        "--tmpfs", "/tmp",
        "--tmpfs", "/var/tmp",
        # /dev/shm — SysV shared-memory backing. Camoufox/Playwright/
        # Firefox use this for IPC between browser master and worker
        # processes; without a mount inside the namespace, the browser
        # crashes immediately on launch. Per-invocation tmpfs keeps
        # shm segments isolated and auto-cleans on namespace exit.
        # Added 2026-04-20 when bwrap rollout extended to browser crons.
        "--tmpfs", "/dev/shm",
    ]

    # System libs — read-only.
    for ro in ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64"):
        if Path(ro).is_dir() or Path(ro).is_symlink():
            cmd += ["--ro-bind-try", ro, ro]

    # /run — read-only. /etc/resolv.conf on Ubuntu 24.04 is a symlink
    # to /run/systemd/resolve/stub-resolv.conf; without binding /run
    # the symlink resolves to a missing file inside the namespace and
    # every DNS lookup fails ("Unable to find the server at
    # www.googleapis.com" was the regression on the first live test).
    if Path("/run").is_dir():
        cmd += ["--ro-bind-try", "/run", "/run"]

    # Codex OAuth token (~/.codex/auth.json) — RO-bound so
    # agents/shared/llm.py::infer can read the token. Every bwrap'd
    # LLM call failed with 'auth.json not found' until this bind was
    # added 2026-04-20. --ro-bind-try so the flag is safe on a machine
    # without Codex installed (CI / fresh dev laptop).
    cmd += ["--ro-bind-try", str(Path.home() / ".codex"),
            str(Path.home() / ".codex")]

    # Operator identity config (~/.clawford/operator.json) — RO-bound
    # so agents/shared/operator.py::load_operator resolves inside the
    # namespace. Added 2026-04-20 when operator.py shipped: agents
    # that were happy under the old BRIAN_ADDRESSES constant started
    # failing with "Operator config not found" until this bind landed.
    # The parent ~/.clawford/ dir is already per-agent; we only want
    # the JSON file exposed, not the whole directory (which contains
    # other agents' workspaces).
    cmd += ["--ro-bind-try",
            str(Path.home() / ".clawford" / "operator.json"),
            str(Path.home() / ".clawford" / "operator.json")]

    # Operator's --user pip install dir (~/.local/) — RO-bound so
    # user-installed Python packages and CLI tools (pip-audit,
    # google-auth, camoufox, etc.) resolve inside the namespace.
    # install-host-deps.sh installs with `pip install --user
    # --break-system-packages`, so this is where every fleet
    # dependency actually lives.
    user_local = Path.home() / ".local"
    if user_local.is_dir():
        cmd += ["--ro-bind", str(user_local), str(user_local)]

    # Repo (shared-lib code, prompts, fixtures). RO so a runaway agent
    # can't accidentally mutate source.
    if repo_root is not None:
        repo = Path(_expand(str(repo_root))).resolve()
        if repo.exists():
            cmd += ["--ro-bind", str(repo), str(repo)]

    # Brain — RO at the root, with a RW whitelist for shared write
    # targets. Pre-widening (2026-04-20) only <brain>/agents/<agent_id>/
    # was RW-bound; every other brain-path write hit EROFS silently.
    # daily-refresh surfaced this on brain/people/*.md starting
    # 2026-04-18, and the new fact miners would have hit the same wall
    # on brain/facts/YYYY-MM.md. The whitelist is explicit: facts,
    # people, commitments, queues — the four shared write targets.
    # brain/memory/ stays RO (agents write their own memory via
    # <brain>/agents/<agent_id>/, not the fleet memory root).
    if brain_root is not None:
        brain = Path(_expand(str(brain_root))).resolve()
        if brain.exists():
            cmd += ["--ro-bind", str(brain), str(brain)]
            agent_brain = brain / "agents" / agent_id
            if agent_brain.exists():
                cmd += ["--bind", str(agent_brain), str(agent_brain)]
            # Shared RW whitelist. --bind-try so a brain root without
            # one of these subdirs yet (fresh deploy) doesn't crash.
            for sub in ("facts", "people", "commitments", "queues"):
                sub_path = brain / sub
                cmd += ["--bind-try", str(sub_path), str(sub_path)]

    # Workspace — read-write.
    if workspace.exists():
        cmd += ["--bind", str(workspace), str(workspace)]

    # Set the working directory inside the namespace so relative
    # imports / cwd-sensitive code see the workspace, not /.
    cmd += ["--chdir", str(workspace)]

    return cmd


def resolve_isolation_mode(
    *, agent_id: str, manifest_mode: str | None,
) -> str:
    """Return the effective isolation mode for an agent.

    Apply the exempt-agent override here so the rest of the system
    can treat the manifest field as authoritative without re-checking
    the exemption list.
    """
    if agent_id in ISOLATION_EXEMPT_AGENTS:
        return ISOLATION_NONE
    raw = (manifest_mode or DEFAULT_ISOLATION).strip().lower()
    if raw not in ALLOWED_ISOLATION_MODES:
        return DEFAULT_ISOLATION
    return raw
