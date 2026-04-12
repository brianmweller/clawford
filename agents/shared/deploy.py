#!/usr/bin/env python3
"""
deploy.py — Unified, idempotent OpenClaw agent deployment.

Replaces the per-agent shell scripts (agents/*/deploy.sh) with a single
Python entrypoint that reads a per-agent manifest.json and brings the live
state on the VPS into alignment with it. Safe to re-run — makes no
destructive changes to state files, tolerates chattr-immutable config
files, and patches crons in-place via `openclaw cron edit` rather than
creating duplicates.

Usage (run on the VPS, inside or outside the gateway container):
    python3 agents/shared/deploy.py <agent_id> [--dry-run] [options]
    python3 agents/shared/deploy.py --all [--exclude fix-it] [--dry-run]

Options:
    --dry-run           Plan only; do not make any changes.
    --skip-files        Skip config-file install (SOUL.md, IDENTITY.md, …).
    --skip-scripts      Skip Python script install.
    --skip-crons        Skip cron sync.
    --skip-channel      Skip Telegram channel/binding + exec approvals.
    --remove-orphans    Delete live crons not in the manifest (default: warn).

Exit codes:
    0  Plan applied successfully (or dry-run clean).
    1  Plan failed partway through.
    2  Bad arguments or manifest parse error.
"""

import argparse
import base64
import difflib
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # .../Clawford
GATEWAY_CONTAINER = "openclaw-openclaw-gateway-1"
BACKUPS_ROOT = Path(os.path.expanduser("~/.openclaw/deploy-backups"))
# Off-VPS mirror: Dropbox syncs this path to the user's workstation with
# 180-day version history. Critical safety net for regression recovery —
# without it, a bad deploy destroys local-to-VPS data with no escape path.
DROPBOX_BACKUP_ROOT = Path(
    os.path.expanduser("~/Dropbox/openclaw-backup/deploy-backups")
)
BACKUP_RETENTION = 10  # keep last N backups per agent


# ────────────────────────────────────────────────────────────────────────
# Logging
# ────────────────────────────────────────────────────────────────────────

_DRY = False


def log(msg: str, level: str = "info") -> None:
    tag = {"info": "  ", "plan": "··", "ok": "✓ ", "warn": "! ", "err": "✗ "}.get(level, "  ")
    print(f"{tag}{msg}")


def note(msg: str) -> None:
    print(f"\n{msg}")


def print_banner(mf: "Manifest", agent_subpath: str) -> None:
    """Explicit workflow contract — printed at the start of every apply run.

    Communicates: source repo, git HEAD, target workspace, directional
    flow (local → VPS, never the reverse), backup location. Prevents
    ambiguity about what the tool does to the VPS.
    """
    source = REPO_ROOT
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=source, capture_output=True, text=True, check=False,
        )
        git_head = r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        git_head = "unknown"
    try:
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=source, capture_output=True, text=True, check=False,
        )
        branch = r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        branch = "unknown"

    workspace = mf.expanded_workspace
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = BACKUPS_ROOT / f"{mf.agent_id}-{stamp}.tar.gz"

    lines = [
        "",
        "+" + "-" * 66 + "+",
        f"| deploy.py {mf.agent_id}",
        "|",
        f"| Source:  {source}",
        f"|          git HEAD: {git_head}  branch: {branch}",
        f"| Target:  {workspace}",
        "|",
        "| Flow:    local git -> VPS workspace",
        "|          Edits made directly on the VPS workspace will be",
        "|          OVERWRITTEN unless committed back to local git first.",
        "|",
        f"| Backup:  {backup_path}",
        "+" + "-" * 66 + "+",
    ]
    for line in lines:
        print(line)


# ────────────────────────────────────────────────────────────────────────
# OpenClaw CLI wrapper
# ────────────────────────────────────────────────────────────────────────


def oc(*args: str, input_data: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run `openclaw <args>` inside the gateway container."""
    cmd = ["docker", "exec"]
    if input_data is not None:
        cmd.append("-i")
    cmd += [GATEWAY_CONTAINER, "openclaw", *args]
    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"oc {' '.join(args[:3])}... exited {result.returncode}: "
            f"{result.stderr.strip()[:300]}"
        )
    return result


def oc_json(*args: str) -> Any:
    """Run `openclaw <args>` and parse JSON stdout."""
    result = oc(*args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"oc {' '.join(args[:3])}... did not return JSON: {e}")


def oc_cron_edit_message(cron_id: str, message: str) -> None:
    """Safely patch a cron's --message via base64 stdin wrapper.

    `subprocess.run` passes args as argv so we can hand the message in as a
    regular Python string — no shell quoting required.
    """
    if _DRY:
        return
    oc("cron", "edit", cron_id, "--message", message)


def oc_cron_add(cron_def: dict) -> str:
    """Register a new cron. Returns the new cron id."""
    args = [
        "cron", "add",
        "--agent", cron_def["agent"],
        "--name", cron_def["name"],
        "--cron", cron_def["cron"],
        "--message", cron_def["message"],
    ]
    if cron_def.get("to"):
        args += ["--to", cron_def["to"]]
    if cron_def.get("account"):
        args += ["--account", cron_def["account"]]
    if cron_def.get("announce"):
        args.append("--announce")
    if cron_def.get("no_deliver"):
        args.append("--no-deliver")
    if _DRY:
        return "<dry-run>"
    result = oc_json(*args)
    return result.get("id", "<unknown>")


def oc_cron_rm(cron_id: str) -> None:
    if _DRY:
        return
    oc("cron", "rm", cron_id)


# ────────────────────────────────────────────────────────────────────────
# Manifest loading + validation
# ────────────────────────────────────────────────────────────────────────


@dataclass
class Cron:
    name: str
    cron: str
    message: str
    announce: bool = False
    no_deliver: bool = False
    account: str | None = None

    def to_cron_def(self, agent_id: str, telegram_account: str, telegram_chat_id: str) -> dict:
        return {
            "agent": agent_id,
            "name": self.name,
            "cron": self.cron,
            "message": self.message,
            "announce": self.announce,
            "no_deliver": self.no_deliver,
            "to": telegram_chat_id,
            "account": self.account or telegram_account,
        }


@dataclass
class ConfigFile:
    src: str
    immutable: bool = False


@dataclass
class StateFile:
    path: str
    seed_if_absent: Any = None


@dataclass
class Manifest:
    agent_id: str
    display_name: str
    workspace: str
    status_file: str
    telegram_account: str
    telegram_bot_token_env: str
    config_files: list[ConfigFile]
    scripts: list[str]
    state_files: list[StateFile]
    approvals_allowlist: list[str]
    crons: list[Cron]
    source_dir: Path = field(default_factory=Path)
    smoke_test: dict | None = None  # {"cron_name": ..., "max_wait_s": int}

    @property
    def expanded_workspace(self) -> Path:
        return Path(os.path.expanduser(self.workspace))

    @property
    def container_workspace(self) -> str:
        # Host `~/.openclaw/<agent>-workspace` is bind-mounted into the
        # gateway container at `/home/node/.openclaw/<agent>-workspace`.
        expanded = os.path.expanduser(self.workspace)
        home = os.path.expanduser("~")
        if expanded.startswith(home):
            return "/home/node" + expanded[len(home):]
        return expanded


def load_manifest(path: Path) -> Manifest:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    required = {"agent_id", "display_name", "workspace", "telegram", "crons"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"{path}: missing required keys: {sorted(missing)}")

    crons = []
    for c in data["crons"]:
        if c.get("announce") and c.get("no_deliver"):
            raise ValueError(
                f"{path}: cron '{c['name']}' cannot have both announce and no_deliver"
            )
        crons.append(Cron(
            name=c["name"],
            cron=c["cron"],
            message=c["message"],
            announce=c.get("announce", False),
            no_deliver=c.get("no_deliver", False),
            account=c.get("account"),
        ))

    mf = Manifest(
        agent_id=data["agent_id"],
        display_name=data["display_name"],
        workspace=data["workspace"],
        status_file=data.get("status_file", ""),
        telegram_account=data["telegram"]["account"],
        telegram_bot_token_env=data["telegram"].get("bot_token_env", ""),
        config_files=[
            ConfigFile(src=cf["src"], immutable=cf.get("immutable", False))
            for cf in data.get("config_files", [])
        ],
        scripts=data.get("scripts", []),
        state_files=[
            StateFile(path=sf["path"], seed_if_absent=sf.get("seed_if_absent"))
            for sf in data.get("state_files", [])
        ],
        approvals_allowlist=data.get("approvals", {}).get(
            "allowlist",
            ["/usr/bin/*", "/bin/*", "/usr/local/bin/*"],
        ),
        crons=crons,
        source_dir=path.parent,
        smoke_test=data.get("smoke_test"),
    )
    return mf


# ────────────────────────────────────────────────────────────────────────
# File operations (chattr-aware)
# ────────────────────────────────────────────────────────────────────────


def is_immutable(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        result = subprocess.run(
            ["lsattr", str(path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return False
        # lsattr output: "----i---------e------- /path/to/file"
        attrs = result.stdout.split()[0] if result.stdout.split() else ""
        return "i" in attrs
    except FileNotFoundError:
        return False


def chattr(path: Path, op: str) -> None:
    """op is '+i' or '-i'."""
    if _DRY:
        return
    subprocess.run(
        ["sudo", "chattr", op, str(path)],
        check=False,
        capture_output=True,
    )


def _show_diff(src: Path, dst: Path, max_lines: int = 30) -> None:
    """Print a unified diff between dst (current) and src (incoming).

    Truncated to max_lines to keep console output manageable.
    """
    try:
        a = dst.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except Exception:
        a = []
    try:
        b = src.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except Exception:
        b = []
    diff = list(difflib.unified_diff(
        a, b, fromfile=f"CURRENT {dst}", tofile=f"INCOMING {src}", n=3,
    ))
    if not diff:
        return
    truncated = diff[:max_lines]
    for line in truncated:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")
    if len(diff) > max_lines:
        print(f"  ... ({len(diff) - max_lines} more diff lines omitted)")


def _confirm_update(src: Path, dst: Path, yes_updates: bool) -> bool:
    """Show diff and ask the user to confirm overwriting dst with src.

    Returns True to proceed, False to skip.
    --yes-updates short-circuits to True without any prompt or diff.
    """
    if yes_updates:
        return True
    _show_diff(src, dst)
    try:
        answer = input(f"Apply UPDATE to {dst.name}? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


def copy_with_immutable(
    src: Path, dst: Path, immutable: bool, yes_updates: bool = False
) -> str:
    """Copy src → dst, honoring any existing immutable flag on dst.

    Returns one of:
      'skip'           — content already matches, no write needed
      'updated'        — dst existed with different content; overwrite applied
      'created'        — dst did not exist; fresh write
      'skip-declined'  — user declined the UPDATE via the confirm gate
    """
    if not src.exists():
        log(f"source missing: {src}", "warn")
        return "skip"

    action = "created"
    if dst.exists():
        if _files_equal(src, dst):
            return "skip"
        action = "updated"
        # Safeguard 3: gate every UPDATE behind a diff preview + confirm
        if not _DRY and not _confirm_update(src, dst, yes_updates):
            log(f"user declined UPDATE for {dst.name}", "warn")
            return "skip-declined"
        if is_immutable(dst):
            chattr(dst, "-i")

    if not _DRY:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    if immutable:
        chattr(dst, "+i")

    return action


def _files_equal(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    return _sha256(a) == _sha256(b)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def seed_state_file(path: Path, content: Any) -> str:
    """Create path with JSON-serialized content only if it doesn't exist."""
    if path.exists():
        return "preserve"
    if _DRY:
        return "would-create"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(content, (dict, list)):
            json.dump(content, f, indent=2)
            f.write("\n")
        else:
            f.write(str(content))
    return "created"


# ────────────────────────────────────────────────────────────────────────
# Source-cleanliness gate (Safeguard 2)
# ────────────────────────────────────────────────────────────────────────


def check_source_clean(source_dir: Path, agent_subpath: str) -> list[str]:
    """Return a list of problem strings if the source git state is not clean.

    Empty list means clean. Non-empty means refuse-deploy (unless --allow-dirty).

    Checks:
      - The source dir is inside a git repo
      - The agent's subpath has no uncommitted modifications
      - The agent's subpath has no untracked files
    """
    problems: list[str] = []
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=source_dir, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            problems.append(f"{source_dir} is not inside a git repo")
            return problems
    except FileNotFoundError:
        problems.append("git executable not found")
        return problems

    result = subprocess.run(
        ["git", "status", "--porcelain", "--", agent_subpath],
        cwd=source_dir, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        problems.append(f"git status failed: {result.stderr.strip()}")
        return problems

    for line in result.stdout.splitlines():
        if not line:
            continue
        # Format: "XY path" where X is staged state, Y is worktree state
        code = line[:2]
        path = line[3:].strip().strip('"')
        if code.startswith("??"):
            problems.append(f"untracked: {path}")
        elif code.strip():
            problems.append(f"modified ({code.strip()}): {path}")

    return problems


# ────────────────────────────────────────────────────────────────────────
# Backup (Safeguard 1)
# ────────────────────────────────────────────────────────────────────────


def backup_workspace(mf: "Manifest") -> Path | None:
    """Tar the target workspace to BACKUPS_ROOT/<agent>-<ts>.tar.gz.

    Captures the PRE-deploy state of the workspace. Non-skippable — called
    at the start of every apply run (dry-run prints the would-be path but
    does not create the file). Rotates old backups, keeping the most recent
    BACKUP_RETENTION per agent.

    Returns the path to the created tarball (or None if the workspace is
    empty / doesn't exist / dry-run).
    """
    workspace = mf.expanded_workspace
    BACKUPS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    tarball = BACKUPS_ROOT / f"{mf.agent_id}-{stamp}.tar.gz"

    if _DRY:
        log(f"backup WOULD write {tarball}", "plan")
        return None

    if not workspace.exists() or not any(workspace.iterdir()):
        log(f"backup SKIP   {workspace} is empty — nothing to back up", "info")
        return None

    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(workspace, arcname=mf.agent_id)
    log(f"backup OK     {tarball.name}", "ok")

    # Mirror to Dropbox for off-VPS retention with version history.
    try:
        DROPBOX_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        mirror = DROPBOX_BACKUP_ROOT / tarball.name
        shutil.copy2(tarball, mirror)
        log(f"backup MIRROR {mirror}", "ok")
    except Exception as e:
        log(f"backup MIRROR failed: {e}", "warn")

    # Rotate: keep last BACKUP_RETENTION per agent (both locations).
    for root in (BACKUPS_ROOT, DROPBOX_BACKUP_ROOT):
        if not root.exists():
            continue
        existing = sorted(root.glob(f"{mf.agent_id}-*.tar.gz"))
        for old in existing[:-BACKUP_RETENTION]:
            try:
                old.unlink()
            except Exception as e:
                log(f"backup rotate failed for {old.name}: {e}", "warn")
    return tarball


# ────────────────────────────────────────────────────────────────────────
# Drift detection (Safeguard 4) — BLOCKING per the NO ON-VPS DEV rule
# ────────────────────────────────────────────────────────────────────────


def _drift_manifest_path(agent_id: str) -> Path:
    return BACKUPS_ROOT / f"{agent_id}-latest.manifest.json"


def _workspace_sha_map(mf: "Manifest") -> dict[str, str]:
    """Compute sha256 for every manifest-tracked file currently in the
    workspace (config files, scripts, state files). Missing files map to
    the empty string — the absence of a previously-tracked file is itself
    a form of drift."""
    ws = mf.expanded_workspace
    result: dict[str, str] = {}
    tracked = [cf.src for cf in mf.config_files] + list(mf.scripts) + [
        sf.path for sf in mf.state_files
    ]
    for rel in tracked:
        p = ws / rel
        if p.exists():
            result[rel] = _sha256(p)
        else:
            result[rel] = ""
    return result


def check_drift(mf: "Manifest") -> list[tuple[str, str, str]]:
    """Compare current workspace hashes to the last-deploy manifest.

    Returns a list of (path, recorded_hash, current_hash) tuples for any
    files that have drifted. Empty list means no drift. If no prior
    manifest exists (first deploy), returns empty (silent pass).
    """
    mpath = _drift_manifest_path(mf.agent_id)
    if not mpath.exists():
        return []
    try:
        recorded = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception:
        return []
    recorded_files: dict[str, str] = recorded.get("files", {})
    current = _workspace_sha_map(mf)
    drifted = []
    for path, rec_hash in recorded_files.items():
        cur_hash = current.get(path, "")
        if cur_hash != rec_hash:
            drifted.append((path, rec_hash, cur_hash))
    return drifted


def write_drift_manifest(mf: "Manifest") -> None:
    """After a successful apply, record per-file sha256 so the next
    deploy can detect drift."""
    if _DRY:
        return
    BACKUPS_ROOT.mkdir(parents=True, exist_ok=True)
    data = {
        "agent_id": mf.agent_id,
        "deploy_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": _workspace_sha_map(mf),
    }
    _drift_manifest_path(mf.agent_id).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def restore_backup(mf: "Manifest", tarball: Path) -> None:
    """Extract a backup tarball over the workspace, replacing current
    content. Used by the smoke-test circuit breaker when the post-deploy
    validation cron fails.
    """
    if _DRY:
        log(f"restore WOULD extract {tarball.name}", "plan")
        return
    workspace = mf.expanded_workspace
    # The tarball is written with arcname=mf.agent_id, so extraction target
    # is the PARENT of the workspace directory.
    with tarfile.open(tarball, "r:gz") as tf:
        # Wipe the workspace first so extraction replaces, not merges
        if workspace.exists():
            for child in workspace.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        # filter='data' for Python 3.12+ safe extraction (defends against
        # path traversal, bad symlinks, etc. — we control the tarballs but
        # belt-and-suspenders).
        tf.extractall(path=workspace.parent, filter="data")
        # The tarball's root is the agent_id directory. Move its contents
        # into the workspace (which has a different name on the host).
        extracted = workspace.parent / mf.agent_id
        if extracted != workspace and extracted.exists():
            for child in extracted.iterdir():
                shutil.move(str(child), str(workspace / child.name))
            extracted.rmdir()
    log(f"restore OK    {tarball.name}", "ok")


def run_smoke_test(mf: "Manifest", cron_name: str, max_wait_s: int) -> bool:
    """Fire the agent's smoke-test cron and return True on exit 0.

    Real implementation uses `oc cron list` to find the cron id, `oc cron
    run <id>` to enqueue, and polls `oc cron runs --id <id> --limit 1` for
    status. In tests this function is monkey-patched.
    """
    try:
        data = oc_json("cron", "list", "--json")
    except Exception as e:
        log(f"smoke test failed to list crons: {e}", "err")
        return False
    cron_id = None
    for j in data.get("jobs", []):
        if j.get("agentId") == mf.agent_id and j.get("name") == cron_name:
            cron_id = j.get("id")
            break
    if not cron_id:
        log(f"smoke test cron '{cron_name}' not found for {mf.agent_id}", "err")
        return False
    try:
        oc("cron", "run", cron_id)
    except Exception as e:
        log(f"smoke test failed to trigger cron: {e}", "err")
        return False
    # Poll for the latest run status
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        time.sleep(5)
        try:
            runs = oc_json("cron", "runs", "--id", cron_id, "--limit", "1")
        except Exception:
            continue
        entries = runs.get("entries", [])
        if entries:
            status = entries[0].get("status")
            if status == "ok":
                return True
            if status in ("error", "failed"):
                return False
    return False


def log_drift_violation(mf: "Manifest", drifted: list[tuple[str, str, str]]) -> None:
    """Append a one-line audit record when --accept-drift is used to
    override a drift detection."""
    if _DRY:
        return
    BACKUPS_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = BACKUPS_ROOT / "drift-violations.log"
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    paths = ",".join(p for p, _, _ in drifted)
    user = os.environ.get("USER") or os.environ.get("USERNAME", "unknown")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{ts} agent={mf.agent_id} user={user} files={paths}\n")


# ────────────────────────────────────────────────────────────────────────
# Cron sync
# ────────────────────────────────────────────────────────────────────────


def fetch_live_crons(agent_id: str) -> dict[str, dict]:
    """Return {name: job_dict} for all live crons owned by this agent."""
    data = oc_json("cron", "list", "--json")
    return {
        j["name"]: j
        for j in data.get("jobs", [])
        if j.get("agentId") == agent_id
    }


def plan_cron_ops(mf: Manifest, live: dict[str, dict], telegram_chat_id: str) -> list[dict]:
    """Return a list of {op, name, ...} ops to bring live → manifest."""
    ops = []
    manifest_names = {c.name for c in mf.crons}

    for cron in mf.crons:
        spec = cron.to_cron_def(mf.agent_id, mf.telegram_account, telegram_chat_id)
        existing = live.get(cron.name)
        if existing is None:
            ops.append({"op": "add", "spec": spec})
            continue

        current_message = existing.get("payload", {}).get("message", "")
        current_cron = existing.get("schedule", {}).get("expr", "")
        if current_message == cron.message and current_cron == cron.cron:
            ops.append({"op": "skip", "name": cron.name})
            continue

        patch: dict[str, Any] = {"op": "edit", "id": existing["id"], "name": cron.name}
        if current_message != cron.message:
            patch["message"] = cron.message
        if current_cron != cron.cron:
            patch["cron"] = cron.cron
        ops.append(patch)

    for name, job in live.items():
        if name not in manifest_names:
            ops.append({"op": "orphan", "name": name, "id": job["id"]})

    return ops


def apply_cron_ops(ops: list[dict], remove_orphans: bool) -> tuple[int, int]:
    """Returns (applied, failed) counts."""
    applied = failed = 0
    for op in ops:
        try:
            if op["op"] == "skip":
                log(f"cron  SKIP   {op['name']}", "ok")
            elif op["op"] == "edit":
                pieces = []
                if "message" in op:
                    pieces.append(f"message({len(op['message'])}ch)")
                if "cron" in op:
                    pieces.append(f"cron={op['cron']}")
                log(f"cron  EDIT   {op['name']} ({', '.join(pieces)})", "plan")
                if not _DRY:
                    edit_args = ["cron", "edit", op["id"]]
                    if "message" in op:
                        edit_args += ["--message", op["message"]]
                    if "cron" in op:
                        edit_args += ["--cron", op["cron"]]
                    oc(*edit_args)
                applied += 1
            elif op["op"] == "add":
                log(f"cron  ADD    {op['spec']['name']} ({op['spec']['cron']})", "plan")
                new_id = oc_cron_add(op["spec"])
                log(f"       → {new_id}", "ok")
                applied += 1
            elif op["op"] == "orphan":
                if remove_orphans:
                    log(f"cron  REMOVE {op['name']} (orphan, --remove-orphans)", "plan")
                    oc_cron_rm(op["id"])
                    applied += 1
                else:
                    log(f"cron  ORPHAN {op['name']} (live but not in manifest — left alone)", "warn")
        except Exception as e:
            log(f"       failed: {e}", "err")
            failed += 1
    return applied, failed


# ────────────────────────────────────────────────────────────────────────
# Channel / binding / approvals (idempotent)
# ────────────────────────────────────────────────────────────────────────


def ensure_channel(mf: Manifest) -> None:
    bot_token = os.environ.get(mf.telegram_bot_token_env, "")
    if not bot_token:
        log(f"channel SKIP  (${mf.telegram_bot_token_env} not in env)", "warn")
        return

    # Check if the channel is already configured — `openclaw channels list`
    # output includes the account id. Avoid adding if already present.
    try:
        existing = oc("channels", "list", check=False)
        if mf.telegram_account in existing.stdout:
            log(f"channel SKIP  telegram:{mf.telegram_account} (already configured)", "ok")
            return
    except Exception:
        pass

    log(f"channel ADD   telegram:{mf.telegram_account}", "plan")
    if not _DRY:
        oc(
            "channels", "add",
            "--channel", "telegram",
            "--token", bot_token,
            "--account", mf.telegram_account,
            "--name", mf.display_name,
            check=False,
        )


def ensure_binding(mf: Manifest) -> None:
    log(f"bind    ENSURE {mf.agent_id} → telegram:{mf.telegram_account}", "plan")
    if not _DRY:
        oc(
            "agents", "bind",
            "--agent", mf.agent_id,
            "--bind", f"telegram:{mf.telegram_account}",
            check=False,
        )


def ensure_approvals(mf: Manifest) -> None:
    for pattern in mf.approvals_allowlist:
        log(f"allow   ENSURE {mf.agent_id} {pattern}", "plan")
        if not _DRY:
            oc(
                "approvals", "allowlist", "add",
                "--agent", mf.agent_id,
                pattern,
                check=False,
            )


# ────────────────────────────────────────────────────────────────────────
# File sync orchestration
# ────────────────────────────────────────────────────────────────────────


def sync_files(mf: Manifest, yes_updates: bool = False) -> tuple[int, int]:
    """Returns (updated, skipped) counts."""
    updated = skipped = 0
    workspace = mf.expanded_workspace
    if not _DRY:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "cache").mkdir(exist_ok=True)
        (workspace / "logs").mkdir(exist_ok=True)

    for cf in mf.config_files:
        src = mf.source_dir / cf.src
        dst = workspace / cf.src
        action = copy_with_immutable(src, dst, cf.immutable, yes_updates=yes_updates)
        tag = {
            "updated": "UPDATE",
            "created": "CREATE",
            "skip": "SKIP  ",
            "skip-declined": "DECLINED",
        }.get(action, "SKIP  ")
        imm = " [immutable]" if cf.immutable else ""
        level = "ok" if action in ("skip", "skip-declined") else "plan"
        log(f"file  {tag} {cf.src}{imm}", level)
        if action in ("updated", "created"):
            updated += 1
        else:
            skipped += 1
    return updated, skipped


def sync_scripts(mf: Manifest, yes_updates: bool = False) -> tuple[int, int]:
    updated = skipped = 0
    workspace = mf.expanded_workspace
    scripts_dir = workspace / "scripts"
    if not _DRY:
        scripts_dir.mkdir(parents=True, exist_ok=True)

    for script in mf.scripts:
        src = mf.source_dir / script
        dst = workspace / script
        action = copy_with_immutable(src, dst, immutable=False, yes_updates=yes_updates)
        if action in ("updated", "created"):
            log(f"script {('UPDATE' if action == 'updated' else 'CREATE')} {script}", "plan")
            if not _DRY and dst.exists():
                os.chmod(dst, 0o755)
            updated += 1
        else:
            skipped += 1

    return updated, skipped


def sync_state_files(mf: Manifest) -> tuple[int, int]:
    created = preserved = 0
    workspace = mf.expanded_workspace
    for sf in mf.state_files:
        path = workspace / sf.path
        action = seed_state_file(path, sf.seed_if_absent)
        if action == "preserve":
            log(f"state  SKIP   {sf.path} (preserving live data)", "ok")
            preserved += 1
        else:
            log(f"state  CREATE {sf.path}", "plan")
            created += 1
    return created, preserved


# ────────────────────────────────────────────────────────────────────────
# Main deploy flow
# ────────────────────────────────────────────────────────────────────────


def deploy_one(agent_id: str, args: argparse.Namespace) -> int:
    manifest_path = REPO_ROOT / "agents" / agent_id / "manifest.json"
    if not manifest_path.exists():
        log(f"no manifest at {manifest_path}", "err")
        return 2

    try:
        mf = load_manifest(manifest_path)
    except Exception as e:
        log(f"manifest invalid: {e}", "err")
        return 2

    note(f"=== {mf.display_name} ({mf.agent_id}) ===")
    # Safeguard 5: banner with workflow contract
    agent_subpath_for_banner = f"agents/{agent_id}"
    print_banner(mf, agent_subpath_for_banner)

    # Safeguard 2: refuse if the source agent dir has uncommitted edits or
    # untracked files, unless --allow-dirty. Prevents the 2026-04-12 incident
    # where local uncommitted work was propagated to production.
    agent_subpath = f"agents/{agent_id}"
    problems = check_source_clean(REPO_ROOT, agent_subpath)
    if problems:
        if getattr(args, "allow_dirty", False):
            note("! Source has uncommitted changes — proceeding under --allow-dirty")
            for p in problems:
                log(p, "warn")
        else:
            log(f"Source not clean. Refusing deploy. {len(problems)} issue(s):", "err")
            for p in problems:
                log(p, "err")
            log("Fix: commit the changes, or rerun with --allow-dirty", "err")
            return 2

    # Safeguard 4: drift detection. If the workspace has changed since the
    # last deploy's recorded manifest, refuse unless --accept-drift. This
    # enforces the NO ON-VPS DEV rule structurally.
    drifted = check_drift(mf)
    if drifted:
        note("Drift check")
        log(f"DRIFT VIOLATION — {len(drifted)} file(s) changed since last deploy:", "err")
        for path, rec, cur in drifted:
            status = "DELETED" if not cur else ("ADDED" if not rec else "MODIFIED")
            log(f"  {status}: {path}", "err")
        if getattr(args, "accept_drift", False):
            log("Proceeding under --accept-drift (violation logged)", "warn")
            log_drift_violation(mf, drifted)
        else:
            log("Refusing deploy. To accept, rerun with --accept-drift", "err")
            log("Or: pull the drifted files back to local git first", "err")
            return 3

    # Safeguard 1: back up the workspace BEFORE any file write. Non-skippable.
    note("Backup")
    pre_deploy_backup = backup_workspace(mf)

    if not args.skip_files:
        note("Config files")
        sync_files(mf, yes_updates=getattr(args, "yes_updates", False))
        note("Scripts")
        sync_scripts(mf, yes_updates=getattr(args, "yes_updates", False))
        note("State files")
        sync_state_files(mf)

    if not args.skip_channel:
        note("Channel / binding / approvals")
        ensure_channel(mf)
        ensure_binding(mf)
        ensure_approvals(mf)

    if not args.skip_crons:
        note("Crons")
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not telegram_chat_id:
            log("TELEGRAM_CHAT_ID not in env — crons may fail delivery", "warn")
        live = fetch_live_crons(mf.agent_id)
        ops = plan_cron_ops(mf, live, telegram_chat_id)
        applied, failed = apply_cron_ops(ops, args.remove_orphans)
        if failed:
            return 1

    # Safeguard 6: smoke test. If the manifest declares a smoke-test cron
    # and --smoke-test is set, fire it. On failure, restore the pre-deploy
    # backup automatically.
    if getattr(args, "smoke_test", False) and mf.smoke_test:
        note("Smoke test")
        cron_name = mf.smoke_test.get("cron_name", "heartbeat")
        max_wait_s = mf.smoke_test.get("max_wait_s", 120)
        log(f"firing {mf.agent_id}/{cron_name}, waiting up to {max_wait_s}s", "info")
        ok = run_smoke_test(mf, cron_name, max_wait_s)
        if not ok:
            log(f"smoke test FAILED — restoring pre-deploy backup", "err")
            if pre_deploy_backup and pre_deploy_backup.exists():
                restore_backup(mf, pre_deploy_backup)
            else:
                log("no backup to restore — workspace left in post-deploy state", "err")
            return 4
        log("smoke test PASSED", "ok")

    # Record the post-deploy workspace state so the next deploy can detect
    # any drift (Safeguard 4).
    write_drift_manifest(mf)
    return 0


def main() -> int:
    global _DRY
    ap = argparse.ArgumentParser(description="Unified OpenClaw agent deploy.")
    ap.add_argument("agent_id", nargs="?", help="Agent id (e.g. shopping); use --all instead to fan out.")
    ap.add_argument("--all", action="store_true", help="Deploy every agent with a manifest.")
    ap.add_argument("--exclude", action="append", default=[], help="Skip this agent (with --all).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-files", action="store_true")
    ap.add_argument("--skip-scripts", action="store_true")
    ap.add_argument("--skip-crons", action="store_true")
    ap.add_argument("--skip-channel", action="store_true")
    ap.add_argument("--remove-orphans", action="store_true")
    ap.add_argument(
        "--allow-dirty", action="store_true",
        help="Permit deploy from a dirty git state (emergency override)",
    )
    ap.add_argument(
        "--yes-updates", action="store_true",
        help="Skip per-file diff confirmation (interactive safety off)",
    )
    ap.add_argument(
        "--accept-drift", action="store_true",
        help="Permit overwrite of drifted workspace files (VPS-side edits)",
    )
    ap.add_argument(
        "--smoke-test", action="store_true",
        help="Fire the manifest's smoke_test cron post-apply; auto-restore on fail",
    )
    args = ap.parse_args()

    _DRY = args.dry_run
    if _DRY:
        note("*** DRY RUN — no changes will be made ***")

    if args.all:
        agents_dir = REPO_ROOT / "agents"
        targets = []
        for child in sorted(agents_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name in args.exclude or child.name.startswith(("_", ".", "shared")):
                continue
            if (child / "manifest.json").exists():
                targets.append(child.name)
        if not targets:
            log("no manifests found", "err")
            return 2
        rc = 0
        for agent_id in targets:
            rc |= deploy_one(agent_id, args)
        return rc

    if not args.agent_id:
        ap.print_usage()
        return 2
    return deploy_one(args.agent_id, args)


if __name__ == "__main__":
    sys.exit(main())
