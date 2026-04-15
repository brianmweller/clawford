#!/usr/bin/env python3
"""
deploy.py — Unified, idempotent Clawford agent deployment.

Replaces the per-agent shell scripts (agents/*/deploy.sh) with a single
Python entrypoint that reads a per-agent manifest.json and brings the live
state on the VPS into alignment with it. Safe to re-run — makes no
destructive changes to state files and tolerates chattr-immutable config
files.

Post-liberation (Phases 5–7), deploy.py is a file-sync + validation tool.
Cron registration is owned by `ops/scripts/install-host-cron.sh`;
Telegram channel setup is manual per `guide-v3/04-vps-setup.md`.

Usage (run on the VPS):
    python3 agents/shared/deploy.py <agent_id> [--dry-run] [options]
    python3 agents/shared/deploy.py --all [--exclude fix-it] [--dry-run]

Options:
    --dry-run           Plan only; do not make any changes.
    --skip-files        Skip config-file install (SOUL.md, IDENTITY.md, …).
    --skip-scripts      Skip Python script install.

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
BACKUPS_ROOT = Path(os.path.expanduser("~/.clawford/deploy-backups"))

# Off-VPS mirror: Dropbox syncs this path to the user's workstation with
# 180-day version history. Critical safety net for regression recovery —
# without it, a bad deploy destroys local-to-VPS data with no escape path.
DROPBOX_BACKUP_ROOT = Path(
    os.path.expanduser("~/Dropbox/openclaw-backup/deploy-backups")
)
BACKUP_RETENTION = 10  # keep last N backups per agent

# Sentinel prepended to bootstrapped .md config files. Safeguard 10 refuses
# to deploy any config file whose first line still carries it — this is how
# we prevent fake-PII template content from silently shipping to a live
# workspace after --bootstrap-configs but before the operator hand-edits.
BOOTSTRAP_SENTINEL = "CLAWFORD_BOOTSTRAP_UNEDITED"

# VPS-side secrets file. A human running `python3 deploy.py` from a
# fresh shell wouldn't have its values in os.environ — so load it
# explicitly before the cron-sync step consults TELEGRAM_CHAT_ID et al.
VPS_ENV_FILE = Path(os.path.expanduser("~/clawford/.env"))


def load_vps_env() -> dict[str, str]:
    """Parse VPS_ENV_FILE into a flat dict. Missing file → empty dict.

    Supports simple KEY=VALUE lines, with comments (#) and blank lines
    ignored. Strips surrounding single/double quotes from values.
    """
    if not VPS_ENV_FILE.exists():
        return {}
    result: dict[str, str] = {}
    for raw in VPS_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            result[key] = val
    return result


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
    enabled: bool = True

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
    crons: list[Cron]
    source_dir: Path = field(default_factory=Path)
    smoke_test: dict | None = None  # {"script": "scripts/heartbeat.py", "max_wait_s": int}

    @property
    def expanded_workspace(self) -> Path:
        return Path(os.path.expanduser(self.workspace))

    @property
    def container_workspace(self) -> str:
        # Vestigial — the gateway container is retired post-Phase-6.
        # Returns the expanded host workspace unchanged. Kept as an
        # attribute so older callers still resolve, but no callers
        # remap into /home/node/ anymore.
        return os.path.expanduser(self.workspace)


def load_manifest_from_dict(data: dict, source_dir: Path | None = None, source_label: str = "<dict>") -> Manifest:
    """Parse an already-loaded manifest dict into a Manifest dataclass.

    Exposed so tests can exercise the parser without going through the
    filesystem. `source_dir` is stored on the Manifest for callers that
    need it (sync_files etc.); `source_label` is only used in error
    messages and defaults to a placeholder when loading from a dict.
    """
    required = {"agent_id", "display_name", "workspace", "telegram", "crons"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"{source_label}: missing required keys: {sorted(missing)}")

    crons = []
    for c in data["crons"]:
        if c.get("announce") and c.get("no_deliver"):
            raise ValueError(
                f"{source_label}: cron '{c['name']}' cannot have both announce and no_deliver"
            )
        crons.append(Cron(
            name=c["name"],
            cron=c["cron"],
            message=c["message"],
            announce=c.get("announce", False),
            no_deliver=c.get("no_deliver", False),
            account=c.get("account"),
            enabled=c.get("enabled", True),
        ))

    return Manifest(
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
        crons=crons,
        source_dir=source_dir or Path(),
        smoke_test=data.get("smoke_test"),
    )


def load_manifest(path: Path) -> Manifest:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return load_manifest_from_dict(data, source_dir=path.parent, source_label=str(path))


def validate_manifest(mf: Manifest, expected_agent_id: str = "") -> list[str]:
    """Return a list of semantic violations in a parsed Manifest.

    Phase 5 of Clawford liberation replaces the old `check_openclaw_config_valid`
    (which asked the OpenClaw gateway to validate its own composed config)
    with a pure-Python cross-field check on the per-agent manifest about to
    be deployed. Catches real developer errors: duplicate cron names,
    missing identity anchors, smoke_test.script dangling references, etc.

    Checks are deliberately narrow — every rule must pass against all 6
    real agents' manifest.json.example files. Any regression here blocks
    deploy with exit code 6 (same code the old check used).
    """
    errors: list[str] = []

    if expected_agent_id and mf.agent_id != expected_agent_id:
        errors.append(
            f"agent_id mismatch: manifest says {mf.agent_id!r}, deploy target is {expected_agent_id!r}"
        )

    cf_srcs = {cf.src for cf in mf.config_files}
    for required_cf in ("SOUL.md", "IDENTITY.md"):
        if required_cf not in cf_srcs:
            errors.append(
                f"config_files missing required entry: {required_cf}"
            )

    if not mf.scripts:
        errors.append("scripts list is empty")
    elif "scripts/heartbeat.py" not in mf.scripts:
        errors.append("scripts list missing scripts/heartbeat.py (fleet-health probe)")

    seen_cron_names: set[str] = set()
    for c in mf.crons:
        if c.name in seen_cron_names:
            errors.append(f"duplicate cron name: {c.name!r}")
        seen_cron_names.add(c.name)

    if not mf.telegram_account:
        errors.append("telegram.account is empty")
    if not mf.telegram_bot_token_env:
        errors.append("telegram.bot_token_env is empty")

    if mf.smoke_test:
        script_rel = mf.smoke_test.get("script")
        if script_rel and script_rel not in mf.scripts:
            errors.append(
                f"smoke_test.script {script_rel!r} is not in the scripts list"
            )

    for sf in mf.state_files:
        p = sf.path
        if p.startswith("/") or p.startswith("~") or (len(p) > 1 and p[1] == ":"):
            errors.append(
                f"state_files[].path must be relative, got absolute/home path: {p!r}"
            )
        if "\\" in p:
            errors.append(
                f"state_files[].path must use forward slashes, got backslash: {p!r}"
            )

    return errors


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


def _has_bootstrap_sentinel(path: Path) -> bool:
    """Return True if the first line of path contains the bootstrap sentinel.

    Only meaningful for text files. Returns False for missing or unreadable
    paths — Safeguard 10's caller classifies those separately.
    """
    try:
        with open(path, encoding="utf-8") as f:
            first_line = f.readline()
    except OSError:
        return False
    return BOOTSTRAP_SENTINEL in first_line


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


# Forbidden shell-operator substrings in cron message bodies — see
# agents/shared/SCRIPT_CONTRACT.md and tests/test_cron_message_hygiene.py.
# An LLM reading a cron message tends to copy commands verbatim into its
# exec tool call. openclaw 2026.4.11's hardcoded preflight rejects any
# interpreter invocation combined with these patterns, which cascaded the
# entire fleet into "approval required" messages on 2026-04-13.
# Safeguard 9 enforces these patterns don't reach the fleet.
CRON_MESSAGE_FORBIDDEN_PATTERNS: list[str] = [
    "; echo $?",
    "; printf",
    "; echo __EXIT",
    '; echo "EXIT',
    "; echo EXIT",
    "$?",
    "sh -lc 'python",
    'sh -lc "python',
    "bash -lc 'python",
    'bash -lc "python',
    "&& python3",
    "&& python ",
    "| python3 ",
    "> /tmp/",
    ">> /tmp/",
    "2>&1",
    "2>/dev/null",
    "$(python",
    # Post-R6 regression guard: per-agent .status.md files are no longer
    # authoritative (fleet-health.json is). Cron messages that tell the
    # LLM to "Update your status file" produced the 2026-04-14 bad-header
    # brain-validation FAIL — the LLM drifts to whatever schema it picks.
    # The canonical guard wording is "DO NOT touch <agent>.status.md —
    # fleet-health.json (R6 orchestrator) is the authoritative health source."
    "Update your status file",
]


def check_cron_message_hygiene(manifest: "Manifest") -> list[str]:
    """Return a list of hygiene errors across the manifest's cron messages.

    Walks every cron's `message` field and flags each occurrence of a
    forbidden shell-operator pattern from CRON_MESSAGE_FORBIDDEN_PATTERNS.
    Each returned string names the offending cron + the matched pattern,
    so the deploy log shows the user exactly where the violation is.

    Returns an empty list when everything is clean. Non-empty → deploy.py
    refuses to proceed with exit code 8.
    """
    errors: list[str] = []
    for cron in manifest.crons:
        msg = cron.message or ""
        for pattern in CRON_MESSAGE_FORBIDDEN_PATTERNS:
            if pattern in msg:
                errors.append(
                    f"cron {cron.name!r}: message contains forbidden pattern "
                    f"{pattern!r}"
                )
    return errors


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
    workspace (config files + scripts). State files are owned by the
    agent at runtime — cron runs mutate them continuously — so they
    must not be hashed into the drift baseline. Missing files map to
    the empty string — the absence of a previously-tracked file is
    itself a form of drift."""
    ws = mf.expanded_workspace
    result: dict[str, str] = {}
    tracked = [cf.src for cf in mf.config_files] + list(mf.scripts)
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

    Legacy drift manifests written before state_files were excluded
    from _workspace_sha_map still contain state_file entries — filter
    them out so we don't fire a spurious DELETED/MODIFIED on them.
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
    state_file_paths = {sf.path for sf in mf.state_files}
    drifted = []
    for path, rec_hash in recorded_files.items():
        if path in state_file_paths:
            continue
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


def run_smoke_test(mf: "Manifest", smoke_cfg: dict) -> bool:
    """Run the agent's smoke-test script as a host subprocess.

    Phase 5 rewrite: was `oc cron list` + `oc cron run` + poll
    `oc cron runs`. Now runs the script on the host directly, matching
    how every cron actually fires post-Phase-4 (host crontab → python3).

    smoke_cfg shape: `{"script": "scripts/heartbeat.py", "max_wait_s": 120}`.
    Backward-compatible with the old `{"cron_name": ..., "max_wait_s": ...}`
    shape — falls through to `scripts/heartbeat.py` as the universal default.
    Every agent is required to ship `scripts/heartbeat.py`, so this path
    always resolves.

    Returns True iff the subprocess exits 0 AND stdout is non-empty (the
    script contract requires at least one JSON status line). False on
    non-zero exit, empty stdout, or TimeoutExpired.
    """
    script_rel = (smoke_cfg or {}).get("script") or "scripts/heartbeat.py"
    max_wait_s = int((smoke_cfg or {}).get("max_wait_s", 120))

    script_abs = mf.expanded_workspace / script_rel
    if not script_abs.exists():
        log(f"smoke test script not found: {script_abs}", "err")
        return False

    try:
        result = subprocess.run(
            ["python3", str(script_abs)],
            timeout=max_wait_s,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            cwd=str(mf.expanded_workspace),
        )
    except subprocess.TimeoutExpired:
        log(f"smoke test timed out after {max_wait_s}s: {script_rel}", "err")
        return False
    except Exception as e:
        log(f"smoke test failed to launch: {e}", "err")
        return False

    if result.returncode != 0:
        log(f"smoke test exited {result.returncode}: {result.stderr[:200]!r}", "err")
        return False
    if not (result.stdout or "").strip():
        log(f"smoke test stdout empty (script contract requires JSON line)", "err")
        return False
    return True


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
# File sync orchestration
# ────────────────────────────────────────────────────────────────────────


def _ensure_config_sources_present(mf: Manifest) -> int:
    """Safeguard 10: classify each config file source and refuse the deploy
    if any can't be resolved.

    Real config files (SOUL.md, IDENTITY.md, USER.md, …) are gitignored
    because they carry PII — only the .example templates are tracked.
    Returns 0 on success, 5 on any problem (errors logged). The caller
    (deploy_one) gates this check on `not args.skip_files` so a partial
    bootstrap can still iterate on crons/approvals.
    """
    needs_bootstrap: list[tuple[str, Path, Path]] = []
    truly_broken: list[tuple[str, Path]] = []
    sentinel_present: list[tuple[str, Path]] = []

    for cf in mf.config_files:
        src = mf.source_dir / cf.src
        template = mf.source_dir / (cf.src + ".example")
        if not src.exists():
            if template.exists():
                needs_bootstrap.append((cf.src, src, template))
            else:
                truly_broken.append((cf.src, src))
        elif _has_bootstrap_sentinel(src):
            sentinel_present.append((cf.src, src))

    if truly_broken:
        log(
            f"config source(s) missing with no .example template "
            f"({len(truly_broken)} file(s)):",
            "err",
        )
        for name, src in truly_broken:
            log(f"  {src}", "err")
        log(
            "Manifest references files that don't exist and have no template "
            "to bootstrap from. Fix the manifest or restore the missing files.",
            "err",
        )
        return 5

    if needs_bootstrap:
        log(
            f"config source(s) missing ({len(needs_bootstrap)} file(s)) — "
            f"real files are gitignored after PII sanitization:",
            "err",
        )
        for name, src, tmpl in needs_bootstrap:
            log(f"  missing:  {src}", "err")
            log(f"  template: {tmpl}", "err")
        log(
            f"Run: python3 agents/shared/deploy.py {mf.agent_id} "
            f"--bootstrap-configs",
            "err",
        )
        log(
            "Then edit each scaffolded file with real values and delete "
            f"the first-line {BOOTSTRAP_SENTINEL} comment before redeploying.",
            "err",
        )
        return 5

    if sentinel_present:
        log(
            f"config source(s) still carry the {BOOTSTRAP_SENTINEL} sentinel "
            f"({len(sentinel_present)} file(s)):",
            "err",
        )
        for name, src in sentinel_present:
            log(f"  {src}", "err")
        log(
            f"Remove the first-line <!-- {BOOTSTRAP_SENTINEL} ... --> comment "
            "after replacing the dummy values with real ones.",
            "err",
        )
        return 5

    return 0


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

    # Remove auto-generated openclaw onboarding files that conflict with a
    # deployed IDENTITY.md. BOOTSTRAP.md is the "fresh workspace" script
    # openclaw writes on first `agents add`; it runs a "who am I?" dialog
    # that shadows the real identity. Once IDENTITY.md is in place, BOOTSTRAP
    # must go.
    _ONBOARDING_ORPHANS = ("BOOTSTRAP.md",)
    have_identity = any(cf.src == "IDENTITY.md" for cf in mf.config_files)
    if have_identity:
        for name in _ONBOARDING_ORPHANS:
            path = workspace / name
            if path.exists():
                if not _DRY:
                    try:
                        path.unlink()
                    except OSError as e:
                        log(f"file  FAIL   remove {name}: {e}", "error")
                        continue
                log(f"file  REMOVE {name} (onboarding orphan)", "plan")
    return updated, skipped


def sync_scripts(
    mf: Manifest,
    yes_updates: bool = False,
    *,
    remove_orphan_scripts: bool = False,
) -> tuple[int, int]:
    """Mirror the manifest's scripts[] into <workspace>/scripts/.

    When remove_orphan_scripts is True, also sweep any *.py file under
    <workspace>/scripts/ that the manifest no longer lists. Non-Python
    files (.js, .json, .sh) are always preserved so the sweep's blast
    radius stays predictable — the Phase 3b followup that motivated
    this flag only needed to clean up deliver-digest.py, and the
    surrounding LinkedIn extractors / etc. are distributed as .js.
    """
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

    if remove_orphan_scripts and scripts_dir.is_dir():
        # Build the set of script basenames the manifest currently lists,
        # scoped to files under scripts/ (not top-level workspace files).
        manifest_script_basenames = {
            Path(s).name
            for s in mf.scripts
            if s.startswith("scripts/") or s.startswith("scripts\\")
        }
        for child in scripts_dir.iterdir():
            if not child.is_file():
                continue
            if child.suffix != ".py":
                continue
            if child.name in manifest_script_basenames:
                continue
            log(f"script REMOVE {child.name} (orphan, --remove-orphan-scripts)", "plan")
            if not _DRY:
                try:
                    child.unlink()
                except OSError as e:
                    log(f"script FAIL   remove {child.name}: {e}", "err")

    return updated, skipped


# Runtime modules agents import from `agents.shared`. Anything NOT in
# this list stays in the repo (deploy.py, workspace-snapshot.py,
# contract_wrap.py, tests/, SCRIPT_CONTRACT.md, fleet-manifest.json,
# …). Expand as new shared modules ship.
SHARED_RUNTIME_MODULES: tuple[str, ...] = (
    "brain.py",
    "camoufox_proxy.py",
    "fleet_health_types.py",
    "google_oauth.py",
    "heartbeat_base.py",
    "llm.py",
    "playwright_profile.py",
    "retry_policy.py",
    "telegram_api.py",
)


def sync_shared_library(mf: Manifest) -> tuple[int, int]:
    """Mirror the runtime `agents/shared/*.py` modules into
    `<workspace>/agents/shared/*.py` so scripts running inside the
    gateway container can `from agents.shared import X` via a small
    sys.path shim.

    Returns (updated_count, skipped_count). Creates the target dir if
    missing. Honors _DRY (no file writes, logs planned actions). Only
    copies the modules in SHARED_RUNTIME_MODULES — deploy-only tools
    and the tests/ subdir stay in the repo.
    """
    updated = skipped = 0
    workspace = mf.expanded_workspace
    src_dir = REPO_ROOT / "agents" / "shared"
    dst_dir = workspace / "agents" / "shared"

    if not src_dir.is_dir():
        log(f"shared source dir missing at {src_dir}", "err")
        return updated, skipped

    if not _DRY:
        dst_dir.mkdir(parents=True, exist_ok=True)

    for name in SHARED_RUNTIME_MODULES:
        src = src_dir / name
        if not src.is_file():
            # A listed module is missing from the source tree — skip
            # silently so the allowlist can list future modules without
            # breaking the current deploy.
            continue
        dst = dst_dir / name
        action = copy_with_immutable(src, dst, immutable=False, yes_updates=True)
        if action in ("updated", "created"):
            log(
                f"shared {('UPDATE' if action == 'updated' else 'CREATE')} {name}",
                "plan",
            )
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


def bootstrap_configs(agent_id: str) -> int:
    """Scaffold missing real config files from their .example siblings.

    After the 2026-04-13 PII sanitization, real config files are
    gitignored — a fresh checkout only has .example templates. This
    function walks agents/<agent_id>/**/*.example and copies each to its
    unsuffixed sibling, prepending a BOOTSTRAP_SENTINEL marker to .md
    files so the operator physically cannot deploy unedited fake values.

    Idempotent: refuses to overwrite existing real files. Operates on the
    filesystem without loading manifest.json (manifest.json.example itself
    may be one of the files that needs scaffolding on a fresh clone).

    Returns 0 on success, 2 if the agent directory doesn't exist.
    """
    agent_dir = REPO_ROOT / "agents" / agent_id
    if not agent_dir.is_dir():
        log(f"no agent directory at {agent_dir}", "err")
        return 2

    note(f"=== bootstrap-configs: {agent_id} ===")
    templates = sorted(agent_dir.rglob("*.example"))
    if not templates:
        log(f"no .example templates found under {agent_dir}", "warn")
        return 0

    created = 0
    skipped = 0
    for template in templates:
        # Strip the trailing ".example" suffix to compute the real path.
        real = template.with_name(template.name[: -len(".example")])
        rel = real.relative_to(agent_dir)

        if real.exists():
            log(f"✓ already present: {rel}", "ok")
            skipped += 1
            continue

        if not _DRY:
            real.parent.mkdir(parents=True, exist_ok=True)
            if real.suffix == ".md":
                body = template.read_text(encoding="utf-8")
                wrapped = (
                    f"<!-- {BOOTSTRAP_SENTINEL}: replace the dummy values "
                    f"below with real ones and delete this line before "
                    f"deploying -->\n\n"
                ) + body
                real.write_text(wrapped, encoding="utf-8")
                log(f"+ scaffolded: {rel} [sentinel]", "plan")
            else:
                # JSON / .py / everything else — copy verbatim. A sentinel
                # comment would break JSON parsing and Python syntax for
                # shebang lines.
                shutil.copy2(template, real)
                log(f"+ scaffolded: {rel}", "plan")
        else:
            marker = " [sentinel]" if real.suffix == ".md" else ""
            log(f"+ would scaffold: {rel}{marker}", "plan")
        created += 1

    note(f"bootstrap-configs: {created} created, {skipped} already present")
    if created and not _DRY:
        log(
            "Next: hand-edit each scaffolded file with real values. For .md "
            f"files, delete the first-line {BOOTSTRAP_SENTINEL} comment — "
            "deploy.py will refuse them until you do.",
            "info",
        )
    return 0


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

    # Safeguard 11 (docker-compose.yml drift) retired 2026-04-15 Phase 7 —
    # the OpenClaw gateway container is gone, so there's no compose file
    # to diff. `ops/docker-compose.yml` was deleted alongside this check.

    # Safeguard 7: refuse if the per-agent manifest has semantic violations.
    # Post-Phase-5 rewrite: was "ask openclaw gateway to validate its own
    # composed config" via `oc config validate --json`; now "cross-check the
    # parsed Manifest against Clawford's structural rules" via pure Python.
    # Catches duplicate cron names, missing SOUL/IDENTITY anchors,
    # smoke_test.script dangling refs, etc. — the class of bug the old check
    # couldn't see because it was looking at the gateway, not the manifest.
    note("Manifest validation")
    manifest_errors = validate_manifest(mf, expected_agent_id=agent_id)
    if manifest_errors:
        log(f"manifest validation failed ({len(manifest_errors)} issue(s)):", "err")
        for e in manifest_errors:
            log(f"  {e}", "err")
        log("Refuse to deploy — fix agents/<id>/manifest.json first.", "err")
        return 6
    log("manifest structurally valid", "ok")

    # Safeguard 8 (exec-approvals baseline) removed 2026-04-15 Phase 5 —
    # OpenClaw approvals concept no longer exists. Phase 7 deletes
    # ops/exec-approvals-baseline.json.

    # Safeguard 9: refuse if any cron message contains a forbidden
    # shell-operator pattern (`; echo $?`, `sh -lc python`, `> /tmp/`, …).
    # Catches the 2026-04-13 class of bug where an LLM copies the pattern
    # from the message verbatim into its exec tool call, hits openclaw's
    # hardcoded preflight, and cascades "approval required" alerts across
    # the fleet. See agents/shared/SCRIPT_CONTRACT.md.
    cron_hygiene_errors = check_cron_message_hygiene(mf)
    if cron_hygiene_errors:
        log(f"cron message hygiene failed ({len(cron_hygiene_errors)} issue(s)):", "err")
        for e in cron_hygiene_errors:
            log(f"  {e}", "err")
        log("Refuse to deploy — rewrite the cron message(s).", "err")
        log("See agents/shared/SCRIPT_CONTRACT.md for the allowed shape.", "err")
        return 8
    log("cron messages clean", "ok")

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
        # Safeguard 10: refuse if any config file source is missing or
        # still carries the bootstrap sentinel. Gated on skip_files so a
        # partial bootstrap can still iterate on crons/approvals.
        note("Config source resolution")
        rc10 = _ensure_config_sources_present(mf)
        if rc10 != 0:
            return rc10
        log("config sources resolved", "ok")

        note("Config files")
        sync_files(mf, yes_updates=getattr(args, "yes_updates", False))
        note("Scripts")
        sync_scripts(
            mf,
            yes_updates=getattr(args, "yes_updates", False),
            remove_orphan_scripts=getattr(args, "remove_orphan_scripts", False),
        )
        note("Shared library")
        sync_shared_library(mf)
        note("State files")
        sync_state_files(mf)

    # Safeguard 6: smoke test. Post-Phase-5: runs the manifest's smoke_test
    # script as a host subprocess and asserts exit 0 + non-empty stdout.
    # On failure, restore the pre-deploy backup automatically.
    if getattr(args, "smoke_test", False) and mf.smoke_test:
        note("Smoke test")
        script_rel = (mf.smoke_test or {}).get("script") or "scripts/heartbeat.py"
        max_wait_s = int((mf.smoke_test or {}).get("max_wait_s", 120))
        log(f"running {mf.agent_id}/{script_rel}, waiting up to {max_wait_s}s", "info")
        ok = run_smoke_test(mf, mf.smoke_test)
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
    ap = argparse.ArgumentParser(description="Clawford agent deploy.")
    ap.add_argument("agent_id", nargs="?", help="Agent id (e.g. shopping); use --all instead to fan out.")
    ap.add_argument("--all", action="store_true", help="Deploy every agent with a manifest.")
    ap.add_argument("--exclude", action="append", default=[], help="Skip this agent (with --all).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-files", action="store_true")
    ap.add_argument("--skip-scripts", action="store_true")
    ap.add_argument(
        "--remove-orphan-scripts", action="store_true",
        help=(
            "Delete any *.py file under <workspace>/scripts/ that is "
            "not in the manifest's scripts list. Off by default so "
            "operator-placed files survive a routine deploy."
        ),
    )
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
    ap.add_argument(
        "--bootstrap-configs", action="store_true",
        help=(
            "Scaffold missing real config files from their .example siblings "
            "(agents/<agent>/SOUL.md.example → SOUL.md). Prepends a sentinel "
            "to .md files — deploy.py refuses until you hand-edit and remove "
            "the sentinel. Does not run a deploy."
        ),
    )
    args = ap.parse_args()

    _DRY = args.dry_run
    if _DRY:
        note("*** DRY RUN — no changes will be made ***")

    if args.bootstrap_configs:
        if args.all:
            agents_dir = REPO_ROOT / "agents"
            rc = 0
            for child in sorted(agents_dir.iterdir()):
                if not child.is_dir():
                    continue
                if child.name in args.exclude or child.name.startswith(("_", ".", "shared")):
                    continue
                rc |= bootstrap_configs(child.name)
            return rc
        if not args.agent_id:
            log("--bootstrap-configs requires an agent_id (or --all)", "err")
            return 2
        return bootstrap_configs(args.agent_id)

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
