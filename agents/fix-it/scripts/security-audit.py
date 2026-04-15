#!/usr/bin/env python3
"""
security-audit.py — Fix-It weekly security audit.

Replaces the inline chr()-obfuscated python -c that previously lived in the
security-audit cron prompt. Pure Python, no shell, no obfuscation.

What it does (post-Phase-6.5 native audit):
  1. Read ~/.clawford/exec-approvals.json and emit per-agent policy
  2. Run native Python checks:
      - chattr +i on each agent's SOUL.md / IDENTITY.md
      - file-mode 0600 on ~/.codex/auth.json and ~/clawford/.env
      - brain directory exists with expected subdirs
      - world-writable files under ~/.clawford/*-workspace/
  3. Apply enrichment rules (suppress known-acceptable findings)
  4. Format an emoji-headed severity report

Exit code: always 0. Audit results are reported, not failed on.

Override the approvals path with EXEC_APPROVALS_PATH env var (for tests).
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

APPROVALS_PATH = os.environ.get(
    "EXEC_APPROVALS_PATH",
    os.path.expanduser("~/.clawford/exec-approvals.json"),
)

# Default host paths for the native audit. Overridable via run_native_audit()
# kwargs for tests.
DEFAULT_WORKSPACE_ROOT = Path(os.path.expanduser("~/.clawford"))
DEFAULT_CODEX_AUTH_PATH = Path(os.path.expanduser("~/.codex/auth.json"))
DEFAULT_ENV_PATH = Path(os.path.expanduser("~/clawford/.env"))
DEFAULT_BRAIN_PATH = Path(os.path.expanduser("~/Dropbox/openclaw-backup"))
EXPECTED_BRAIN_SUBDIRS = frozenset({
    "agents", "people", "facts", "queues", "commitments",
})

LSATTR_TIMEOUT_SEC = 10


def get_agent_policies(approvals_path: str | os.PathLike | None = None):
    """Return list of (agent, policy) tuples from exec-approvals.json.

    `approvals_path` defaults to APPROVALS_PATH, which honors the
    EXEC_APPROVALS_PATH env var. Tests override it directly.
    """
    path = str(approvals_path) if approvals_path is not None else APPROVALS_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return [("(error)", f"approvals file not found at {path}")]
    except Exception as e:
        return [("(error)", f"approvals file unreadable: {e}")]

    agents = data.get("agents", {})
    if not agents:
        defaults = data.get("defaults", {})
        policy = defaults.get("policy") or defaults.get("security") or "?"
        return [("(defaults)", policy)]

    result = []
    for name in sorted(agents.keys()):
        cfg = agents[name] or {}
        policy = cfg.get("policy") or cfg.get("security") or "?"
        result.append((name, policy))
    return result


SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def _default_lsattr_runner(path: Path) -> str | None:
    """Run `lsattr <path>` and return the attribute string, or None if
    lsattr is unavailable or the call failed.

    lsattr output for a single file looks like:
        `----i---------e-- /path/to/file`
    We return the leading attribute column.
    """
    try:
        r = subprocess.run(
            ["lsattr", str(path)],
            capture_output=True,
            text=True,
            timeout=LSATTR_TIMEOUT_SEC,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    except OSError:
        return None
    if r.returncode != 0:
        return None
    parts = r.stdout.split(None, 1)
    if not parts:
        return None
    return parts[0]


def run_native_audit(
    *,
    workspace_root: Path | None = None,
    codex_auth_path: Path | None = None,
    env_path: Path | None = None,
    brain_path: Path | None = None,
    lsattr_runner: Callable[[Path], str | None] | None = None,
) -> dict[str, list[str]]:
    """Native Python security audit for the Clawford fleet.

    Replaces the pre-Phase-6.5 `openclaw security audit --deep` subprocess.
    Returns findings in the same {severity: [description]} shape
    render_report() expects.

    Checks performed:
      1. SOUL.md / IDENTITY.md in each ~/.clawford/*-workspace/ have
         the immutable (chattr +i) attribute.
      2. ~/.codex/auth.json and ~/clawford/.env exist with mode 0600.
      3. ~/Dropbox/openclaw-backup/ exists with expected subdirs.
      4. No world-writable files under ~/.clawford/*-workspace/.

    All path arguments are overridable for tests. `lsattr_runner` takes
    a Path and returns the mode string (containing 'i' if immutable),
    or None if lsattr cannot be run at all.
    """
    workspace_root = workspace_root or DEFAULT_WORKSPACE_ROOT
    codex_auth_path = codex_auth_path or DEFAULT_CODEX_AUTH_PATH
    env_path = env_path or DEFAULT_ENV_PATH
    brain_path = brain_path or DEFAULT_BRAIN_PATH
    lsattr_runner = lsattr_runner or _default_lsattr_runner

    findings: dict[str, list[str]] = {sev: [] for sev in SEVERITIES}

    # 1. Immutable identity files
    lsattr_warned = False
    if workspace_root.is_dir():
        for ws in sorted(workspace_root.glob("*-workspace")):
            agent_label = ws.name.removesuffix("-workspace")
            for immutable_name in ("SOUL.md", "IDENTITY.md"):
                target = ws / immutable_name
                if not target.exists():
                    continue
                attrs = lsattr_runner(target)
                if attrs is None:
                    if not lsattr_warned:
                        findings["LOW"].append(
                            "cannot verify immutability — lsattr missing "
                            "(install e2fsprogs)"
                        )
                        lsattr_warned = True
                    continue
                if "i" not in attrs:
                    findings["CRITICAL"].append(
                        f"{immutable_name} not immutable in "
                        f"{agent_label}-workspace — agent values can be "
                        f"rewritten"
                    )

    # 2. Secret file modes
    for label, path in (
        ("~/.codex/auth.json", codex_auth_path),
        ("~/clawford/.env", env_path),
    ):
        if not path.exists():
            findings["HIGH"].append(f"credential missing: {label}")
            continue
        try:
            mode = path.stat().st_mode & 0o777
        except OSError as e:
            findings["HIGH"].append(f"cannot stat {label}: {e}")
            continue
        if mode > 0o600:
            findings["HIGH"].append(
                f"{label} is mode {oct(mode)[2:]}, expected 600"
            )

    # 3. Brain directory sanity
    if not brain_path.is_dir():
        findings["MEDIUM"].append(f"brain directory missing at {brain_path}")
    else:
        existing = {p.name for p in brain_path.iterdir() if p.is_dir()}
        missing = sorted(EXPECTED_BRAIN_SUBDIRS - existing)
        if missing:
            findings["MEDIUM"].append(
                f"brain directory missing subdirs: {missing}"
            )

    # 4. World-writable files under workspaces
    if workspace_root.is_dir():
        for ws in sorted(workspace_root.glob("*-workspace")):
            for dirpath, _dirs, files in os.walk(ws):
                for fname in files:
                    fp = Path(dirpath) / fname
                    try:
                        mode = fp.stat().st_mode
                    except OSError:
                        continue
                    if mode & 0o002:
                        findings["MEDIUM"].append(
                            f"world-writable: {fp}"
                        )

    return findings


SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🟢",
}


def render_report(policies, findings):
    """Format the final emoji-headed report.

    Post-Phase-6.5: `findings` is the dict returned by run_native_audit().
    No raw audit text to render — the native audit produces structured
    findings directly.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"🦊🔧 Security Audit — {today}", ""]

    lines.append("🔒 Exec Policies")
    for agent, policy in policies:
        lines.append(f"• {agent}: {policy}")
    lines.append("(All agents policy=full is intentional — see KNOWN_ISSUES.md)")
    lines.append("")

    total_findings = sum(len(v) for v in findings.values())
    if total_findings == 0:
        lines.append("✅ Security audit clean.")
        return "\n".join(lines).rstrip() + "\n"

    for sev in SEVERITIES:
        items = findings.get(sev, [])
        emoji = SEVERITY_EMOJI[sev]
        if not items:
            continue
        lines.append(f"{emoji} {sev} ({len(items)})")
        for item in items:
            lines.append(f"• {item}")
        lines.append("")

    crit_high = findings.get("CRITICAL", []) + findings.get("HIGH", [])
    if crit_high:
        lines.append("Remediation:")
        for item in crit_high:
            lines.append(f"• {item} — investigate, then notify human.")

    return "\n".join(lines).rstrip() + "\n"


def main():
    policies = get_agent_policies()
    findings = run_native_audit()
    report = render_report(policies, findings)
    sys.stdout.write(report)
    sys.exit(0)


if __name__ == "__main__":
    main()
