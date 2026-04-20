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


def agent_config_path(agent_id: str, filename: str) -> Path:
    """Return the full path to a specific agent config file in the
    Dropbox brain. Canonical location for SOUL.md, IDENTITY.md,
    AGENTS.md, MEMORY.md, and manifest.json since the liberation-era
    migration from repo-side to Dropbox-synced.

    Rejects path traversal — filename must be a single component,
    agent_id must be a simple identifier. Agents pass operator-
    unsanitized inputs sometimes; this guards against injection.
    """
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"filename must be a single path component: {filename!r}")
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise ValueError(f"agent_id must be a simple identifier: {agent_id!r}")
    return dropbox_brain_root() / "agents" / agent_id / filename


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


# ---------------------------------------------------------------------------
# People helpers — people/<slug>.md is a flat layout; circle lives in the
# frontmatter "circles" field (may be comma-separated for multi-circle).
# ---------------------------------------------------------------------------


import re as _re  # noqa: E402


_PEOPLE_FIELD_RE = _re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")


def _slugify(name: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with single hyphens, strip
    leading/trailing hyphens. Matches the convention used by the existing
    people-seed-from-mine.py miner."""
    slug = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


def _parse_people_md(text: str) -> tuple[str, dict]:
    """Parse a people file. Returns (display_name, fields_dict).

    display_name is pulled from the `# Name` H1 header if present, else "".
    fields is a dict of every `- **key:** value` line.
    """
    name = ""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("# ") and not name:
            name = stripped[2:].strip()
            continue
        m = _PEOPLE_FIELD_RE.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return name, fields


def get_person(name_or_slug: str) -> dict | None:
    """Look up a person file by name ("Priya Rivera"), slug ("priya-rivera"),
    or first-name-only shorthand ("Priya").

    Returns ``{"slug", "name", "path", "fields", "raw"}`` if found, else
    ``None``. ``fields`` is the flat dict of frontmatter key/values; ``raw`` is
    the full markdown content for callers that need to render it verbatim.

    Matching order:
      1. Exact slug / slugified-name lookup.
      2. First-name fallback: if the slugified input has one token and
         exactly ONE person file's slug starts with ``<token>-``, return
         that person. Ambiguous first names return None — callers must
         ask for disambiguation.
    """
    slug = name_or_slug if "-" in name_or_slug and name_or_slug == name_or_slug.lower() else _slugify(name_or_slug)
    path = dropbox_brain_root() / "people" / f"{slug}.md"
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        name, fields = _parse_people_md(raw)
        return {
            "slug": slug,
            "name": name,
            "path": str(path),
            "fields": fields,
            "raw": raw,
        }

    # First-name fallback — only engages when input is a single token.
    if "-" not in slug:
        people_dir = dropbox_brain_root() / "people"
        if people_dir.exists():
            candidates = [
                p for p in people_dir.glob(f"{slug}-*.md")
                if p.is_file()
            ]
            if len(candidates) == 1:
                return get_person(candidates[0].stem)
    return None


def list_persons(circle: str | None = None) -> list[dict]:
    """List person files, optionally filtered by circle membership.

    Circle matching is exact against comma-separated tokens in the
    ``circles`` frontmatter field. Returns a list of ``{"slug", "name",
    "circles", "last_interaction"}`` dicts — a minimal summary, not the
    full file (call ``get_person`` for that).
    """
    people_dir = dropbox_brain_root() / "people"
    if not people_dir.exists():
        return []
    out: list[dict] = []
    for path in sorted(people_dir.glob("*.md")):
        slug = path.stem
        name, fields = _parse_people_md(path.read_text(encoding="utf-8"))
        circles_raw = fields.get("circles", "")
        circles = [c.strip() for c in circles_raw.split(",") if c.strip()]
        if circle is not None and circle not in circles:
            continue
        out.append({
            "slug": slug,
            "name": name,
            "circles": circles,
            "last_interaction": fields.get("last_interaction", ""),
        })
    return out


def create_person_file(
    name: str,
    circle: str,
    *,
    slug: str | None = None,
    **extra_fields: str,
) -> dict:
    """Create ``people/<slug>.md`` with frontmatter fields. Raises
    ``FileExistsError`` if a file for this slug already exists.

    ``extra_fields`` values are coerced to str and written as
    ``- **key:** value`` lines after the core ``slug``/``circles`` pair.
    """
    final_slug = slug or _slugify(name)
    path = dropbox_brain_root() / "people" / f"{final_slug}.md"
    if path.exists():
        raise FileExistsError(f"Person file already exists: {path}")

    lines = [f"# {name}", "", f"- **slug:** {final_slug}", f"- **circles:** {circle}"]
    for key, value in extra_fields.items():
        lines.append(f"- **{key}:** {value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"slug": final_slug, "path": str(path), "name": name}


# ---------------------------------------------------------------------------
# Inbox notes — notes/inbox.md is append-only with `---` dividers and a
# canonical frontmatter block per entry.
# ---------------------------------------------------------------------------


from datetime import datetime as _dt, timezone as _tz  # noqa: E402


def append_inbox_note(agent_id: str, text: str, *, triaged: bool = False) -> dict:
    """Append a note entry to ``notes/inbox.md`` with a stable id, ISO
    timestamp, source agent, content, and triage flag.

    Returns ``{"id", "path"}``. Entries are divided by ``---`` and follow the
    same ``- **key:** value`` frontmatter convention used by facts and
    commitments.
    """
    import secrets as _secrets

    now = _dt.now(_tz.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    # Windows' datetime.now resolution can be coarser than a microsecond —
    # two calls in the same tick would collide on stamp alone. Append a
    # short random suffix to guarantee uniqueness.
    entry_id = f"{agent_id}-{stamp}-{_secrets.token_hex(2)}"
    path = dropbox_brain_root() / "notes" / "inbox.md"
    existing = path.exists() and path.read_text(encoding="utf-8").strip() != ""

    lines = []
    if existing:
        lines.append("\n---\n")
    lines.extend([
        f"- **id:** {entry_id}",
        f"- **timestamp:** {now.isoformat().replace('+00:00', 'Z')}",
        f"- **agent:** {agent_id}",
        f"- **content:** {text}",
        f"- **triaged:** {'true' if triaged else 'false'}",
    ])
    chunk = "\n".join(lines) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(chunk)
    return {"id": entry_id, "path": str(path)}
