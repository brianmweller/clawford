"""agents/shared/brain.py — shared-brain filesystem helpers.

The shared brain is the Clawford fleet's cross-agent state layer. It
lives in two places, split by write semantics:

    git-tracked (ops/brain/*)
        Schemas, canonical configs, rules files. Read-only from
        agents at runtime. Written only by deploy scripts and host
        crons at controlled points. Changes flow through normal code
        review and git history.

    Dropbox-synced (~/Dropbox/openclaw-backup/*)
        Ephemeral agent state. People facts, per-agent status files,
        queues, commitments, tasks, notes, fleet-health snapshots.
        Agent-writable without flooding git history. Append-only
        patterns for queue files so concurrent writers don't stomp
        each other.

This module codifies both root paths, exposes low-level read/write
helpers for each, plus a handful of high-level shortcuts for the most
common files (fleet-health.json, per-agent status dirs).

Existing convention the module follows:
    DEFAULT_DROPBOX_ROOT = Path.home() / "Dropbox" / "openclaw-backup"
Matches what ops/brain/scripts/validate.py and every brain-aware
script in the fleet already use.

Env var overrides for tests and non-standard installs:
    CLAWFORD_BRAIN_DROPBOX_ROOT
    CLAWFORD_BRAIN_GIT_ROOT
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DROPBOX_ROOT_ENV = "CLAWFORD_BRAIN_DROPBOX_ROOT"
GIT_ROOT_ENV = "CLAWFORD_BRAIN_GIT_ROOT"

FLEET_HEALTH_FILENAME = "fleet-health.json"


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def dropbox_brain_root() -> Path:
    """Return the root of the Dropbox-synced brain.

    Honors CLAWFORD_BRAIN_DROPBOX_ROOT env var for tests/overrides.
    Falls back to Path.home() / Dropbox / openclaw-backup — the same
    convention used by the rest of the fleet (ops/scripts, obsidian-
    briefing config, commitment-scan, notes-triage, etc.).
    """
    env = os.environ.get(DROPBOX_ROOT_ENV)
    if env:
        return Path(env)
    return Path.home() / "Dropbox" / "openclaw-backup"


def git_brain_root() -> Path:
    """Return the root of the git-tracked brain (ops/brain/).

    Honors CLAWFORD_BRAIN_GIT_ROOT env var for tests. Falls back to the
    repo-relative location: agents/shared/brain.py → parents[2] is the
    repo root, ops/brain/ hangs off it.
    """
    env = os.environ.get(GIT_ROOT_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "ops" / "brain"


# ---------------------------------------------------------------------------
# Text read / write / append  (Dropbox side)
# ---------------------------------------------------------------------------


def read_text(relative_path: str) -> str:
    """Read a text file from the Dropbox-synced brain. Raises
    FileNotFoundError if the file doesn't exist."""
    return (dropbox_brain_root() / relative_path).read_text(encoding="utf-8")


def write_text(relative_path: str, content: str) -> None:
    """Write content to the Dropbox-synced brain, creating parent dirs
    as needed. Overwrites existing files — use append_text for log-shaped
    writes."""
    full = dropbox_brain_root() / relative_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def append_text(relative_path: str, line: str) -> None:
    """Append a line to a file in the Dropbox-synced brain, creating
    the file and parent dirs if needed. Ensures a trailing newline is
    present — if the caller already included one, don't double it."""
    full = dropbox_brain_root() / relative_path
    full.parent.mkdir(parents=True, exist_ok=True)
    if not line.endswith("\n"):
        line = line + "\n"
    with open(full, "a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# JSON read / write  (Dropbox side, atomic writes)
# ---------------------------------------------------------------------------


def read_json(relative_path: str) -> dict:
    """Read and parse a JSON file from the Dropbox-synced brain."""
    return json.loads(
        (dropbox_brain_root() / relative_path).read_text(encoding="utf-8")
    )


def write_json(relative_path: str, data: dict, *, indent: int = 2) -> None:
    """Atomically write JSON data to the Dropbox-synced brain.

    Uses the tmpfile+rename pattern so a partially-written file never
    appears at the target path — important for files like fleet-health
    .json that are read concurrently with writes.
    """
    full = dropbox_brain_root() / relative_path
    full.parent.mkdir(parents=True, exist_ok=True)
    tmp = full.with_name(full.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    tmp.replace(full)


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def read_fleet_health() -> dict:
    """Load fleet-health.json from the brain root. Written by the host-
    cron probe in ops/scripts/fleet-health.py; read by fix-it's
    morning-status and heartbeat scripts."""
    return read_json(FLEET_HEALTH_FILENAME)


def write_fleet_health(data: dict) -> None:
    """Atomically overwrite fleet-health.json."""
    write_json(FLEET_HEALTH_FILENAME, data)


def agent_status_path(agent_id: str) -> Path:
    """Return the directory where a given agent's status files live.

    Agents write their own per-tick status here; Mr Fixit and the
    probe script read from the same location. Caller is responsible
    for constructing sub-paths (e.g., /status.md, /activity.jsonl).
    """
    return dropbox_brain_root() / "agents" / agent_id


# ---------------------------------------------------------------------------
# Git-side reads  (no writes — deploy handles those through normal file I/O)
# ---------------------------------------------------------------------------


def read_git_text(relative_path: str) -> str:
    """Read a text file from the git-tracked brain (ops/brain/*).

    There's no write_git_text counterpart on purpose: agents never write
    to git-tracked paths at runtime. Deploy scripts write via normal
    Path operations because they're working on the checked-out source
    tree, not the runtime brain.
    """
    return (git_brain_root() / relative_path).read_text(encoding="utf-8")


def read_git_json(relative_path: str) -> dict:
    return json.loads(
        (git_brain_root() / relative_path).read_text(encoding="utf-8")
    )
