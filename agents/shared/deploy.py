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

    # SOUL.md / IDENTITY.md / AGENTS.md / MEMORY.md / USER.md live in the
    # Dropbox brain now — they're no longer tracked by the manifest's
    # config_files list. Existence is validated at the dispatcher / read
    # path, not at deploy time.

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


# ────────────────────────────────────────────────────────────────────────
# pip-audit gate (Safeguard 12) — supply-chain vulnerability check
# ────────────────────────────────────────────────────────────────────────


PIP_AUDIT_MODE_ENV_VAR = "CLAWFORD_PIP_AUDIT_MODE"
PIP_AUDIT_DEFAULT_MODE = "warn"
PIP_AUDIT_ALLOWED_MODES = ("warn", "enforce", "skip")
# By default we only block on HIGH/CRITICAL CVEs — informational and
# medium-severity findings clutter the log and almost never need
# emergency action. Override via CLAWFORD_PIP_AUDIT_SEVERITY=low.
PIP_AUDIT_BLOCKING_SEVERITY_ENV_VAR = "CLAWFORD_PIP_AUDIT_SEVERITY"
PIP_AUDIT_DEFAULT_BLOCKING_SEVERITY = "high"
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _pip_audit_mode() -> str:
    raw = (os.environ.get(PIP_AUDIT_MODE_ENV_VAR) or PIP_AUDIT_DEFAULT_MODE).strip().lower()
    if raw not in PIP_AUDIT_ALLOWED_MODES:
        return PIP_AUDIT_DEFAULT_MODE
    return raw


def _pip_audit_blocking_severity_threshold() -> int:
    raw = (
        os.environ.get(PIP_AUDIT_BLOCKING_SEVERITY_ENV_VAR)
        or PIP_AUDIT_DEFAULT_BLOCKING_SEVERITY
    ).strip().lower()
    return _SEVERITY_RANK.get(raw, _SEVERITY_RANK[PIP_AUDIT_DEFAULT_BLOCKING_SEVERITY])


def _parse_pip_audit_json(stdout: str) -> list[dict]:
    """Normalize pip-audit's JSON output to a flat list of vulnerability
    dicts: [{name, version, cve, severity, fix_versions, description}, ...].

    pip-audit's output shape varies slightly between versions; this
    handles both the {"dependencies": [...]} envelope and the bare list
    form. Unknown shapes return [] so the caller treats it as 'no
    findings' rather than crashing.
    """
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(body, dict):
        deps = body.get("dependencies") or []
    elif isinstance(body, list):
        deps = body
    else:
        return []

    findings: list[dict] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = dep.get("name") or dep.get("package") or "?"
        version = dep.get("version") or "?"
        for v in dep.get("vulns") or []:
            if not isinstance(v, dict):
                continue
            findings.append({
                "name": name,
                "version": version,
                "cve": v.get("id") or v.get("aliases", ["?"])[0] if v.get("aliases") else (v.get("id") or "?"),
                "severity": (v.get("severity") or "unknown").lower(),
                "fix_versions": v.get("fix_versions") or [],
                "description": (v.get("description") or "")[:200],
            })
    return findings


def _resolve_pip_audit_binary() -> str | None:
    """Find pip-audit on disk. Tries PATH first, then common --user
    install locations the host-deps script uses."""
    found = shutil.which("pip-audit")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "pip-audit",
        Path("/usr/local/bin/pip-audit"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def check_pip_audit(
    requirements_path=None,
    *,
    runner=None,
):
    """Run `pip-audit` against the given requirements file (or current
    environment if omitted). Returns (findings, error_message).

    If pip-audit isn't installed, returns ([], "pip-audit not available")
    so the caller can decide to log-and-skip vs hard-fail. The runner
    parameter is injectable for tests.
    """
    binary = _resolve_pip_audit_binary() or "pip-audit"
    cmd = [binary, "--format", "json"]
    if requirements_path is not None:
        cmd += ["-r", str(requirements_path)]
    runner = runner or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=120))
    try:
        proc = runner(cmd)
    except FileNotFoundError:
        return [], "pip-audit not installed (pip install pip-audit)"
    except subprocess.TimeoutExpired:
        return [], "pip-audit timed out after 120s"
    except Exception as e:  # pragma: no cover — defensive
        return [], f"pip-audit failed to launch: {e}"

    # pip-audit exits 1 when vulnerabilities are found — that's expected
    # and not an error. Other non-zero codes (missing requirements file,
    # malformed JSON output) ARE errors.
    stdout = (proc.stdout or "").strip()
    if proc.returncode not in (0, 1):
        stderr_tail = (proc.stderr or "").strip()[-300:]
        return [], f"pip-audit exit {proc.returncode}: {stderr_tail or 'no stderr'}"

    findings = _parse_pip_audit_json(stdout)
    return findings, None


def filter_blocking_findings(
    findings: list[dict], threshold_rank: int,
) -> list[dict]:
    """Keep only findings whose severity is >= threshold_rank.

    Findings with severity 'unknown' fall through as non-blocking —
    pip-audit can't always grade GHSA records, and a non-blocking
    advisory still appears in the deploy log for operator review.
    """
    return [
        f for f in findings
        if _SEVERITY_RANK.get(f.get("severity", "unknown"), 0) >= threshold_rank
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
    self_healed: list[str] = []

    workspace = mf.expanded_workspace

    for cf in mf.config_files:
        src = mf.source_dir / cf.src
        template = mf.source_dir / (cf.src + ".example")
        if not src.exists():
            # Self-heal: if the deployed workspace already has a real
            # (non-sentinel) copy, seed the repo source from it. A past
            # ``git clean`` can wipe the gitignored repo copies while the
            # workspace copies survive — restore the link transparently
            # rather than forcing a bootstrap-then-re-edit cycle.
            ws_copy = workspace / cf.src
            if ws_copy.exists() and not _has_bootstrap_sentinel(ws_copy):
                src.parent.mkdir(parents=True, exist_ok=True)
                src.write_bytes(ws_copy.read_bytes())
                self_healed.append(cf.src)
                continue
            if template.exists():
                needs_bootstrap.append((cf.src, src, template))
            else:
                truly_broken.append((cf.src, src))
        elif _has_bootstrap_sentinel(src):
            sentinel_present.append((cf.src, src))

    if self_healed:
        log(
            f"config sources self-healed from workspace ({len(self_healed)} file(s)): "
            + ", ".join(self_healed),
            "warn",
        )

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
    "audience.py",           # 2026-04-19 — Flux-ported audience-visibility filter for draft-compose
    "availability.py",       # 2026-04-19 — free-slot calculator for draft-compose
    "brain.py",
    "brain_index.py",         # 2026-04-20 — per-subject _index.json for fast fact lookup
    "brain_tasks.py",         # 2026-04-18 — task-queue parser + in-place editors for Mouse
    "camoufox_proxy.py",
    "context_builder.py",     # 2026-04-19 — RecipientContext assembler for draft-compose
    "fact_extraction.py",    # 2026-04-20 — shared miner LLM extractor (Gmail/Krisp/Workflowy)
    "facts.py",              # 2026-04-19 — fact-file reader + upsert_fact for birthday miner
    "people.py",             # 2026-04-20 — append_observation helper for people-card nudges
    "gmail_api.py",          # 2026-04-19 — Gmail threaded-draft creation + thread_to_compose_inputs
    "gcal_freebusy.py",      # 2026-04-21 — Calendar freebusy.query wrapper for connector scheduling
    "gmail_watch.py",        # 2026-04-20 — users.watch() wrapper + WatchState for real-time triage
    "pubsub_pull.py",        # 2026-04-20 — Pub/Sub pull/ack helpers for gmail-push-listener
    "fleet_health_types.py",
    "google_oauth.py",
    "heartbeat_base.py",
    "inbound_patterns.py",   # P0.4 — regex list for inbound scanner
    "inbound_scanner.py",    # P0.4 — scan_inbound() + semantic_guard()
    "isolation.py",          # P1.2 — bubblewrap argv builder
    "calendar_index.py",      # 2026-04-18 — shared brain calendar index reader
    "llm.py",
    "meeting_classifier.py",  # 2026-04-18 — Murphy/Mouse routing predicate
    "memory_writer.py",
    "operator.py",            # 2026-04-20 — operator identity loader (~/.clawford/operator.json)
    "pending_actions.py",
    "pending_queue.py",       # 2026-04-21 — brain-maintenance review queue JSONL
    "pending_review_resolve.py",  # 2026-04-21 — approve/reject helpers for pending facts
    "embed.py",               # 2026-04-21 — fastembed wrapper for cross-run semantic dedupe
    "playwright_profile.py",
    "rate_limit.py",         # P1.3 — outbound rate limit + dedup
    "retry_policy.py",
    "reviewer.py",           # P0.1 — outbound action classifier
    "scan_fields.py",        # P0.4 — wire-in helper (per-agent ingest)
    "self_profile.py",       # 2026-04-21 — the operator's professional brain loader for Huckle cold-recruiter drafting
    "state_introspection.py",  # 2026-04-20 — host-cron log parser for get_recent_runs
    "subprocess_helpers.py",
    "telegram_api.py",
    "voice.py",              # 2026-04-19 — B&L politeness + register model for draft-compose
    # Non-.py runtime data files (the sync uses literal filenames, no
    # extension check) — included here so load-from-workspace lookups
    # resolve without assuming the repo layout.
    "fleet-manifest.json",   # consumed by doctor-audit + others
)


# Subdirectories under agents/shared/ that must also be mirrored into
# the workspace. Pure data files (prompt templates, etc.) read by the
# runtime modules at call time.
SHARED_RUNTIME_DIRS: tuple[str, ...] = (
    "prompts",  # P0.4 — anti_leakage.txt + semantic_guard.txt
)


def _manifest_path(agent_id: str) -> Path:
    """manifest.json lives at the repo-side per-agent directory."""
    return REPO_ROOT / "agents" / agent_id / "manifest.json"


# Fields that flow from manifest.json.example → manifest.json on sync.
# These are STRUCTURAL (same across all operators of this agent).
# Fields NOT in this list are operator-private (crons with PII, approvals,
# etc.) and must NEVER be overwritten by sync.
MANIFEST_STRUCTURAL_FIELDS: tuple[str, ...] = (
    "config_files",
    "scripts",
    "state_files",
)


def sync_manifest_structure(
    actual_path: Path | str,
    example_path: Path | str,
    *,
    force_delete: bool = False,
) -> dict:
    """Copy structural fields from manifest.json.example to manifest.json
    while preserving operator-private fields (crons with PII, approvals).

    Context: manifest.json is gitignored per PII remediation (cron prompts
    contain real names, places, calendar IDs). Only manifest.json.example
    is tracked. When a structural change needs to flow — adding/removing
    config_files entries, updating scripts lists — it lands in .example
    via git, then operators run this sync to apply it.

    If manifest.json doesn't exist yet, bootstraps it from .example as a
    fresh copy (operator will fill in crons next).

    Guard: when the sync would REMOVE one or more scripts, config_files,
    or state_files from the live manifest (meaning the entry exists in
    actual but is missing from example), the sync refuses to write and
    returns status=blocked with the removal list in the diff. The
    operator must re-run with force_delete=True after inspecting. This
    guard was added 2026-04-20 after a sync silently dropped three
    legitimate scripts that were present in the live manifest but
    missing from .example.

    Returns a dict with status + diff description so callers can log
    what changed.
    """
    actual_path = Path(actual_path)
    example_path = Path(example_path)

    if not example_path.exists():
        return {
            "status": "error",
            "error": f"manifest.json.example not found at {example_path}",
        }

    try:
        with open(example_path, encoding="utf-8") as f:
            example = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "error", "error": f"could not read .example: {exc}"}

    if actual_path.exists():
        try:
            with open(actual_path, encoding="utf-8") as f:
                actual = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "error", "error": f"could not read manifest.json: {exc}"}
        bootstrapped = False
    else:
        # Bootstrap case: no manifest.json yet, seed it from .example.
        actual = json.loads(json.dumps(example))
        bootstrapped = True

    diff = {"status": "ok", "bootstrapped": bootstrapped}

    for field in MANIFEST_STRUCTURAL_FIELDS:
        if field not in example:
            continue
        before = actual.get(field, [])
        after = example[field]

        # Human-readable diff for config_files / scripts / state_files
        if field == "config_files":
            before_srcs = [cf.get("src") for cf in before if isinstance(cf, dict)]
            after_srcs = [cf.get("src") for cf in after if isinstance(cf, dict)]
            diff["config_files_added"] = [s for s in after_srcs if s not in before_srcs]
            diff["config_files_removed"] = [s for s in before_srcs if s not in after_srcs]
        elif field == "scripts":
            diff["scripts_added"] = [s for s in after if s not in before]
            diff["scripts_removed"] = [s for s in before if s not in after]
        elif field == "state_files":
            before_paths = [sf.get("path") for sf in before if isinstance(sf, dict)]
            after_paths = [sf.get("path") for sf in after if isinstance(sf, dict)]
            diff["state_files_added"] = [p for p in after_paths if p not in before_paths]
            diff["state_files_removed"] = [p for p in before_paths if p not in after_paths]

        actual[field] = after

    # Silent-delete guard. If any structural field would lose entries
    # on sync and force_delete wasn't passed, refuse to write. Returning
    # status=blocked leaves the operator's manifest untouched so they
    # can inspect the removal list and decide whether to re-sync with
    # --force-delete or patch .example first. Bootstraps skip the guard
    # (no existing data to protect).
    removals = (
        diff.get("scripts_removed", [])
        + diff.get("config_files_removed", [])
        + diff.get("state_files_removed", [])
    )
    if removals and not force_delete and not bootstrapped:
        return {
            **diff,
            "status": "blocked",
            "error": (
                "sync would remove entries present in the live manifest "
                "but missing from .example — re-run with --force-delete "
                "to proceed"
            ),
        }

    try:
        actual_path.parent.mkdir(parents=True, exist_ok=True)
        with open(actual_path, "w", encoding="utf-8") as f:
            json.dump(actual, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError as exc:
        return {"status": "error", "error": f"write failed: {exc}"}

    return diff


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

    # Mirror data-only subdirectories (prompt templates, etc.) so
    # runtime modules can resolve sibling files via __file__-relative
    # paths inside the workspace just like they do in the repo.
    for sub in SHARED_RUNTIME_DIRS:
        src_sub = src_dir / sub
        if not src_sub.is_dir():
            continue
        dst_sub = dst_dir / sub
        if not _DRY:
            dst_sub.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(src_sub.iterdir()):
            if not src_file.is_file():
                continue
            dst_file = dst_sub / src_file.name
            action = copy_with_immutable(
                src_file, dst_file, immutable=False, yes_updates=True,
            )
            if action in ("updated", "created"):
                log(
                    f"shared {('UPDATE' if action == 'updated' else 'CREATE')} "
                    f"{sub}/{src_file.name}",
                    "plan",
                )
                updated += 1
            else:
                skipped += 1

    return updated, skipped


def heal_cross_workspace_symlinks(mf: Manifest) -> list[str]:
    """Replace any symlink inside the workspace whose target is OUTSIDE
    the workspace with an independent file copy.

    Why: once P1.2 bubblewrap isolation lands, a symlink that points
    across workspaces silently breaks every time its agent's cron
    runs under bwrap — the namespace only binds the agent's own
    workspace, so the symlink target isn't visible inside. The canonical
    case was shopping-workspace/token.json → ../family-calendar-
    workspace/token.json on 2026-04-16; gmail-search errored with
    "token.json not found" and the operator's /arriving went dark on the
    Gmail side.

    Fix is idempotent: if the symlink is already a real file, skip.
    If the target is inside the same workspace, keep the symlink
    (within-workspace symlinks resolve inside the namespace fine).
    Only cross-workspace symlinks get resolved — the copy is taken
    from the target's current contents so the agent gets a fresh
    snapshot on every deploy.

    Returns the list of healed paths (relative to the workspace).
    """
    healed: list[str] = []
    workspace = mf.expanded_workspace.resolve()
    if not workspace.is_dir():
        return healed

    for child in workspace.rglob("*"):
        if not child.is_symlink():
            continue
        try:
            target = child.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        try:
            target.relative_to(workspace)
            # Target is inside the workspace — symlink is bwrap-safe,
            # leave it.
            continue
        except ValueError:
            pass
        # Cross-workspace (or outside-workspace) symlink — heal it.
        if not target.is_file():
            # Dangling symlink — log and skip; we don't invent content.
            log(
                f"dangling symlink (cross-workspace target missing): "
                f"{child.relative_to(workspace)} → {target}",
                "warn",
            )
            continue
        if _DRY:
            log(
                f"would heal symlink: {child.relative_to(workspace)} → "
                f"{target} (cross-workspace, copy-in-place)",
                "plan",
            )
            continue
        try:
            content = target.read_bytes()
            child.unlink()
            child.write_bytes(content)
            # Preserve restrictive mode if the target was restricted
            # (token files typically land at 0600).
            try:
                os.chmod(child, target.stat().st_mode & 0o777)
            except OSError:
                pass
        except OSError as e:
            log(f"failed to heal symlink {child}: {e}", "warn")
            continue
        rel = str(child.relative_to(workspace))
        log(
            f"healed cross-workspace symlink: {rel} → independent copy",
            "plan",
        )
        healed.append(rel)
    return healed


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
    manifest_path = _manifest_path(agent_id)
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
        # Heal any cross-workspace symlinks (e.g. a historical
        # token.json → ../other-workspace/token.json) that would
        # silently break under P1.2 bwrap isolation.
        note("Cross-workspace symlink heal")
        healed = heal_cross_workspace_symlinks(mf)
        if healed:
            log(f"healed {len(healed)} cross-workspace symlink(s)", "ok")
        else:
            log("no cross-workspace symlinks to heal", "ok")

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
    ap.add_argument(
        "--sync-manifest", action="store_true",
        help=(
            "Sync structural fields (config_files, scripts, state_files) from "
            "manifest.json.example to manifest.json, preserving operator-private "
            "fields (crons with PII, approvals). Use this when a git pull brings "
            "in structural manifest changes. Does not run a deploy. Refuses to "
            "remove entries silently — re-run with --force-delete after "
            "inspecting the reported removal list."
        ),
    )
    ap.add_argument(
        "--force-delete", action="store_true",
        help=(
            "Allow --sync-manifest to remove entries from the live manifest "
            "that are missing from .example. Use only after inspecting the "
            "removal list in a prior blocked sync."
        ),
    )
    ap.add_argument(
        "--skip-pip-audit", action="store_true",
        help=(
            "Skip Safeguard 12 (pip-audit supply-chain check). Emergency "
            "override; CLAWFORD_PIP_AUDIT_MODE=skip is the env-var equivalent."
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

    if args.sync_manifest:
        def _sync_one(agent_id: str) -> int:
            actual = REPO_ROOT / "agents" / agent_id / "manifest.json"
            example = REPO_ROOT / "agents" / agent_id / "manifest.json.example"
            result = sync_manifest_structure(
                actual, example, force_delete=args.force_delete,
            )
            if result.get("status") == "error":
                log(f"{agent_id}: sync failed — {result.get('error')}", "err")
                return 1
            if result.get("status") == "blocked":
                parts = []
                for key in ("scripts_removed", "config_files_removed",
                            "state_files_removed"):
                    vals = result.get(key, [])
                    if vals:
                        parts.append(f"{key}={vals}")
                log(
                    f"{agent_id}: sync BLOCKED — would remove {', '.join(parts)}. "
                    "Re-run with --force-delete after confirming the removals "
                    "are intentional (the usual fix is to update .example).",
                    "err",
                )
                return 2
            parts = []
            for key in ("config_files_added", "config_files_removed",
                        "scripts_added", "scripts_removed",
                        "state_files_added", "state_files_removed"):
                vals = result.get(key, [])
                if vals:
                    parts.append(f"{key}={vals}")
            if result.get("bootstrapped"):
                log(f"{agent_id}: bootstrapped from .example", "ok")
            elif parts:
                log(f"{agent_id}: synced — {', '.join(parts)}", "ok")
            else:
                log(f"{agent_id}: already in sync", "ok")
            return 0
        if args.all:
            agents_dir = REPO_ROOT / "agents"
            rc = 0
            for child in sorted(agents_dir.iterdir()):
                if not child.is_dir():
                    continue
                if child.name in args.exclude or child.name.startswith(("_", ".", "shared")):
                    continue
                if not (child / "manifest.json.example").exists():
                    continue
                rc |= _sync_one(child.name)
            return rc
        if not args.agent_id:
            log("--sync-manifest requires an agent_id (or --all)", "err")
            return 2
        return _sync_one(args.agent_id)

    # Safeguard 12: pip-audit supply-chain check. Runs ONCE per deploy
    # invocation (system-wide check; same answer for every agent) and
    # short-circuits before any backup or workspace write happens.
    # Mode resolution: CLI flag > env var > default ("warn"). Default is
    # warn so the gate gathers data without blocking the operator's
    # routine deploys; flip to enforce after the first round of advisory
    # review.
    if not _run_pip_audit_safeguard(args):
        return 9

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


def _run_pip_audit_safeguard(args) -> bool:
    """Run Safeguard 12. Returns True to proceed, False to abort.

    Decision matrix:
      mode == skip OR --skip-pip-audit  → skip the check, log it, return True
      pip-audit not installed           → log warning, return True
      no findings at-or-above threshold → log clean, return True
      findings, mode == warn            → log them, return True (don't block)
      findings, mode == enforce         → log them, return False (block)
    """
    if getattr(args, "skip_pip_audit", False):
        note("Safeguard 12 (pip-audit) — SKIPPED via --skip-pip-audit")
        return True
    mode = _pip_audit_mode()
    if mode == "skip":
        note("Safeguard 12 (pip-audit) — SKIPPED via CLAWFORD_PIP_AUDIT_MODE=skip")
        return True

    note("Safeguard 12 (pip-audit)")
    findings, err = check_pip_audit()
    if err:
        log(f"pip-audit unavailable — {err}; continuing without supply-chain check", "warn")
        return True

    threshold = _pip_audit_blocking_severity_threshold()
    blocking = filter_blocking_findings(findings, threshold)

    if not blocking:
        if findings:
            log(
                f"pip-audit: {len(findings)} advisory(ies) below severity "
                f"threshold; deploy proceeds",
                "ok",
            )
        else:
            log("pip-audit: no advisories", "ok")
        return True

    log(
        f"pip-audit: {len(blocking)} blocking advisory(ies) "
        f"(severity >= rank {threshold}):",
        "err" if mode == "enforce" else "warn",
    )
    for f in blocking:
        fixes = ",".join(f["fix_versions"]) if f["fix_versions"] else "(no fix)"
        log(
            f"  {f['name']}=={f['version']} {f['cve']} "
            f"severity={f['severity']} fix={fixes}",
            "err" if mode == "enforce" else "warn",
        )
        if f.get("description"):
            log(f"    {f['description']}", "warn")
    if mode == "enforce":
        log(
            "Refusing deploy. Either upgrade the affected packages, "
            "rerun with --skip-pip-audit, or set "
            "CLAWFORD_PIP_AUDIT_MODE=warn / CLAWFORD_PIP_AUDIT_SEVERITY=critical.",
            "err",
        )
        return False
    log("warn mode: deploy proceeds despite advisories", "warn")
    return True


if __name__ == "__main__":
    sys.exit(main())
