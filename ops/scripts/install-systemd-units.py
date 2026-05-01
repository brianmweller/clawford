#!/usr/bin/env python3
"""install-systemd-units.py — drift-aware deployer for clawford systemd units.

Why this exists
---------------
2026-05-01 incident. The repo's `costco-socks-tunnel.service` was
missing `-i` in its ExecStart since day one. The live unit on the
VPS had been manually patched at original deploy-time. The 2026-04-29
break-glass refactor's `sudo cp ~/repo/ops/systemd/*.service
/etc/systemd/system/` overwrote the manual fix with the broken repo
version. The autossh process kept running on its old args until a
later restart, then started rejecting auth and brought down SOCKS
for ~half a day.

This script makes the repo authoritative AND surfaces drift either
direction before clobbering live state.

Modes
-----

  --check
      Report drift, exit 0 if everything's in sync, exit 1 if any
      unit is drifted/missing-live/missing-repo. Called from the
      post-merge git hook so a stale live unit shows up immediately
      after `git pull` rather than silently lurking until next
      manual deploy.

  --install [--yes]
      Install: copy each repo unit to its target, daemon-reload, log
      the action. If a live unit differs from the repo version AND
      --yes is not passed, REFUSE to overwrite that one (others
      proceed). With --yes, overwrite even drifted live files.

  --install --yes --restart
      As above, plus restart the unit after install. Useful when
      ExecStart changed (the running process keeps the old args
      until restart).

The four clawford units (manifest hardcoded; see UNITS below):
  costco-socks-tunnel.service       system   /etc/systemd/system/
  clawford-huckle-push.service      system   /etc/systemd/system/
  clawford-calendar-brain.service   user     ~/.config/systemd/user/
  clawford-inbox.service            user     ~/.config/systemd/user/
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_SYSTEMD_DIR = REPO_ROOT / "ops" / "systemd"


@dataclass
class UnitEntry:
    """One clawford systemd unit and its deploy scope."""
    name: str
    scope: str  # "system" or "user"


# Hardcoded manifest. If a fifth unit gets added, fail the test
# at test_manifest_includes_all_four_clawford_units until this list
# is updated alongside the new file in ops/systemd/.
UNITS: list[UnitEntry] = [
    UnitEntry(name="costco-socks-tunnel.service", scope="system"),
    UnitEntry(name="clawford-huckle-push.service", scope="system"),
    UnitEntry(name="clawford-calendar-brain.service", scope="user"),
    UnitEntry(name="clawford-inbox.service", scope="user"),
]


def target_path(entry: UnitEntry, home: str | None = None) -> str:
    """Resolve the live filesystem path for a unit's installed copy."""
    if entry.scope == "system":
        return f"/etc/systemd/system/{entry.name}"
    home = home or os.path.expanduser("~")
    return f"{home}/.config/systemd/user/{entry.name}"


# ─── Drift detection ─────────────────────────────────────────────────


@dataclass
class DriftStatus:
    state: str   # "in_sync" | "drifted" | "missing_live" | "missing_repo"
    diff: str = ""


def check_one(repo_path: Path, live_path: Path) -> DriftStatus:
    """Compare a single unit's repo copy against its live target.

    Pure: no side effects, just file IO. Returns one of four states:
      in_sync      both files present and identical
      drifted      both present but contents differ — emit unified diff
      missing_live repo file present but no live copy yet
      missing_repo live file orphaned — repo no longer carries it
    """
    repo_path = Path(repo_path)
    live_path = Path(live_path)
    repo_exists = repo_path.exists()
    live_exists = live_path.exists()

    if not repo_exists and not live_exists:
        # Both missing — treat as in_sync (nothing to deploy or reconcile).
        return DriftStatus(state="in_sync")
    if not repo_exists:
        return DriftStatus(
            state="missing_repo",
            diff=f"orphan: {live_path} exists on disk but no source in ops/systemd/",
        )
    if not live_exists:
        return DriftStatus(
            state="missing_live",
            diff=f"not deployed: {live_path} missing — run --install to copy from repo",
        )

    repo_text = repo_path.read_text(encoding="utf-8")
    live_text = live_path.read_text(encoding="utf-8")
    if repo_text == live_text:
        return DriftStatus(state="in_sync")

    diff = "".join(difflib.unified_diff(
        live_text.splitlines(keepends=True),
        repo_text.splitlines(keepends=True),
        fromfile=f"live: {live_path}",
        tofile=f"repo: {repo_path}",
        n=2,
    ))
    return DriftStatus(state="drifted", diff=diff)


# ─── Install with confirmation ───────────────────────────────────────


@dataclass
class InstallResult:
    name: str = ""
    action: str = ""   # "installed" | "skipped_in_sync" | "refused_drifted" | "error"
    message: str = ""


def _run(cmd: list[str], **kwargs):
    """Wrapper around subprocess.run so tests can stub it."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def install_one(
    repo_path: Path,
    live_path: Path,
    *,
    scope: str,
    yes: bool,
    restart: bool = False,
) -> InstallResult:
    """Install one unit. Refuses drifted live without `yes=True`."""
    repo_path = Path(repo_path)
    live_path = Path(live_path)
    name = repo_path.name

    status = check_one(repo_path, live_path)
    if status.state == "in_sync":
        return InstallResult(name=name, action="skipped_in_sync", message="identical to repo")
    if status.state == "missing_repo":
        return InstallResult(
            name=name,
            action="error",
            message=f"orphan: {live_path} has no repo source",
        )
    if status.state == "drifted" and not yes:
        return InstallResult(
            name=name,
            action="refused_drifted",
            message=(
                f"live differs from repo; pass --yes to overwrite. "
                f"Diff (first 10 lines):\n"
                + "\n".join(status.diff.splitlines()[:10])
            ),
        )

    # Either missing_live or (drifted && yes). Copy.
    if scope == "system":
        cp_cmd = ["sudo", "cp", str(repo_path), str(live_path)]
        reload_cmd = ["sudo", "systemctl", "daemon-reload"]
        restart_cmd = ["sudo", "systemctl", "restart", name]
    else:
        cp_cmd = ["cp", str(repo_path), str(live_path)]
        reload_cmd = ["systemctl", "--user", "daemon-reload"]
        restart_cmd = ["systemctl", "--user", "restart", name]

    cp_result = _run(cp_cmd)
    if getattr(cp_result, "returncode", 0) != 0:
        return InstallResult(name=name, action="error", message=f"cp failed: {cp_result}")

    reload_result = _run(reload_cmd)
    if getattr(reload_result, "returncode", 0) != 0:
        return InstallResult(
            name=name,
            action="error",
            message=f"daemon-reload failed: {reload_result}",
        )

    if restart:
        _run(restart_cmd)

    return InstallResult(name=name, action="installed", message="copied + daemon-reload")


# ─── --check entrypoint ──────────────────────────────────────────────


def check_main() -> int:
    """Report drift across all clawford units. Exit nonzero on any
    drift so the post-merge hook can flag it."""
    any_drift = False
    print("Drift check (clawford systemd units):")
    for entry in UNITS:
        repo_path = REPO_SYSTEMD_DIR / entry.name
        live_path = Path(target_path(entry))
        status = check_one(repo_path, live_path)
        marker = {
            "in_sync": "✓",
            "drifted": "⚠",
            "missing_live": "•",
            "missing_repo": "✗",
        }.get(status.state, "?")
        print(f"  {marker} {entry.name:<40s} {status.state}")
        if status.state in ("drifted", "missing_repo"):
            any_drift = True
            for line in status.diff.splitlines()[:6]:
                print(f"      {line}")
    if any_drift:
        print("\nDrift detected. Run `python ops/scripts/install-systemd-units.py "
              "--install [--yes]` to reconcile.", file=sys.stderr)
        return 1
    return 0


# ─── --install entrypoint ────────────────────────────────────────────


def install_main(yes: bool, restart: bool) -> int:
    print(f"Install (yes={yes}, restart={restart}):")
    had_error = False
    for entry in UNITS:
        repo_path = REPO_SYSTEMD_DIR / entry.name
        live_path = Path(target_path(entry))
        result = install_one(
            repo_path, live_path,
            scope=entry.scope, yes=yes, restart=restart,
        )
        marker = {
            "installed": "✓",
            "skipped_in_sync": "·",
            "refused_drifted": "⚠",
            "error": "✗",
        }.get(result.action, "?")
        print(f"  {marker} {result.name:<40s} {result.action}")
        if result.message:
            for line in result.message.splitlines():
                print(f"      {line}")
        if result.action in ("refused_drifted", "error"):
            had_error = True
    return 1 if had_error else 0


# ─── CLI ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=False)

    sub.add_parser("--check", help="report drift, exit nonzero if any")
    inst = sub.add_parser("--install", help="copy + daemon-reload")
    inst.add_argument("--yes", action="store_true",
                      help="overwrite drifted live files without prompting")
    inst.add_argument("--restart", action="store_true",
                      help="systemctl restart each installed unit")

    # Allow flat flags (--check / --install / --yes / --restart) so callers
    # don't need subparser ergonomics.
    ap.add_argument("--check", action="store_true", help="report drift only")
    ap.add_argument("--install", action="store_true", help="install units")
    ap.add_argument("--yes", action="store_true",
                    help="overwrite drifted live files without prompting")
    ap.add_argument("--restart", action="store_true",
                    help="systemctl restart each installed unit")

    args, _ = ap.parse_known_args(argv)

    if args.check and not args.install:
        return check_main()
    if args.install:
        return install_main(yes=args.yes, restart=args.restart)
    # Default: --check
    return check_main()


if __name__ == "__main__":
    sys.exit(main())
