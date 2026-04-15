#!/usr/bin/env python3
"""
workspace-snapshot.py — Daily per-agent workspace backup to Dropbox.

Runs as a fix-it cron once per day. Tars each agent's workspace to
~/Dropbox/openclaw-backup/workspace-snapshots/<agent>-<date>.tar.gz.
Dropbox syncs the result to the user's local machine with 180-day
version history, giving off-VPS recovery independent of the
deploy-time tarballs written by agents/shared/deploy.py.

Why two backup paths:
  - deploy.py backup: captures the state JUST BEFORE a deploy. Only
    exists when a deploy runs. Bridges the gap between the moment the
    deploy tool is invoked and the point where workspace files are
    overwritten.
  - this script: captures the state ONCE PER DAY regardless of deploys.
    Protects against workspace corruption/edits that happen between
    deploys (the window deploy-time backup can't cover). Also runs when
    deploys haven't happened in weeks.

Retention: last 14 daily snapshots per agent, then rolling off.

Usage:
    python3 workspace-snapshot.py              # back up all 5 agents
    python3 workspace-snapshot.py --agent X    # single agent
    python3 workspace-snapshot.py --list       # list existing snapshots
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


AGENTS = ["shopping", "family-calendar", "meetings-coach", "news-digest", "connector", "fix-it"]
WORKSPACES_ROOT = Path(os.path.expanduser("~/.clawford"))
SNAPSHOTS_ROOT = Path(
    os.path.expanduser("~/Dropbox/openclaw-backup/workspace-snapshots")
)
RETENTION = 14


def snapshot_agent(agent_id: str) -> Path | None:
    workspace = WORKSPACES_ROOT / f"{agent_id}-workspace"
    if not workspace.exists():
        print(f"  {agent_id}: SKIP (no workspace)", file=sys.stderr)
        return None

    SNAPSHOTS_ROOT.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y-%m-%d", time.gmtime())
    tarball = SNAPSHOTS_ROOT / f"{agent_id}-{date}.tar.gz"

    if tarball.exists():
        # Already captured today — don't overwrite an existing snapshot.
        print(f"  {agent_id}: already have {tarball.name}")
        return tarball

    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(workspace, arcname=agent_id)
    size_mb = tarball.stat().st_size / (1024 * 1024)
    print(f"  {agent_id}: OK {tarball.name} ({size_mb:.1f} MB)")

    # Rotate: keep last RETENTION snapshots per agent
    existing = sorted(SNAPSHOTS_ROOT.glob(f"{agent_id}-*.tar.gz"))
    for old in existing[:-RETENTION]:
        try:
            old.unlink()
            print(f"  {agent_id}: rotated {old.name}")
        except Exception as e:
            print(f"  {agent_id}: rotate failed for {old.name}: {e}")
    return tarball


def list_snapshots() -> None:
    if not SNAPSHOTS_ROOT.exists():
        print(f"No snapshots directory at {SNAPSHOTS_ROOT}")
        return
    for agent_id in AGENTS:
        snaps = sorted(SNAPSHOTS_ROOT.glob(f"{agent_id}-*.tar.gz"))
        if not snaps:
            print(f"{agent_id}: (none)")
            continue
        print(f"{agent_id}: {len(snaps)} snapshots")
        for s in snaps[-3:]:
            size_mb = s.stat().st_size / (1024 * 1024)
            print(f"  {s.name} ({size_mb:.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", help="Snapshot only this agent")
    ap.add_argument("--list", action="store_true", help="List existing snapshots")
    args = ap.parse_args()

    if args.list:
        list_snapshots()
        return 0

    agents = [args.agent] if args.agent else AGENTS
    print(f"Snapshotting {len(agents)} agent(s) to {SNAPSHOTS_ROOT}")
    failed = 0
    for agent_id in agents:
        try:
            snapshot_agent(agent_id)
        except Exception as e:
            print(f"  {agent_id}: FAILED {e}", file=sys.stderr)
            failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
