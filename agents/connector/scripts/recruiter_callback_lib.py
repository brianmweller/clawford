"""Recruiter-callback dispatcher — single entry point for Telegram
callbacks on cold-recruiter FYI messages.

Routes `recruiter:<action>:<thread_id>` (from agents/shared/dispatcher.py) to:

- **keep**: turn the ephemeral cold-recruiter stub into a real
  `brain/people/<slug>.md` file so Murphy's meeting-prep can find the
  contact on the calendar next week and so Huckle's own future inbounds
  from the same sender route as `queued` (known).
- **reject**: record the sender on `cache/rejected-recruiters.jsonl` so
  inbox_triage short-circuits future messages from the same address as
  `skipped_rejected_recruiter`, preventing the same inbound from
  re-queueing the cold-recruiter pipeline indefinitely.

Keep does NOT remove the entry from `cache/triage-queue.json` —
auto-compose's processed-log (`cache/auto-compose-log.json`) already
keeps the current run from double-composing; the queue entry ages out
naturally when the rolling triage window moves past it.

Return shape: `{"status": "ok"|"already_kept"|"already_rejected"|
"not_found"|"error", "action": <str>, ...}`. Callers render a short
confirmation message to Telegram from this.
"""
from __future__ import annotations

import json
import re
import sys
from email.utils import parseaddr
from pathlib import Path
from typing import Any

for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared import brain  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


RECRUITER_CIRCLES = "recruiter, professional-outer"
RECRUITER_RELATIONSHIP_TYPE = "recruiter"


# ---------------------------------------------------------------------------
# Field derivation from queue entry
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with single hyphens, strip
    leading/trailing hyphens. Mirrors agents/shared/brain.py::_slugify."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _display_name_from_header(from_header: str, from_email: str) -> str:
    """Extract the quoted display name from a From header; empty string when
    the header is bare email (no quoted name)."""
    if not from_header:
        return ""
    name, addr = parseaddr(from_header)
    name = (name or "").strip().strip('"')
    if not name:
        return ""
    # parseaddr returns the local-part as name when the header is bare
    # `foo@bar.com`. Treat that case as 'no display name'.
    if name.lower() == (from_email.partition("@")[0] or "").lower():
        return ""
    return name


def derive_person_fields_from_queue_entry(entry: dict) -> dict:
    """Build the fields needed by brain.create_person_file from a
    queued_cold_recruiter entry. Returns a dict with:
      - name (str) — display name or email local-part fallback
      - slug (str) — slugified name; when no display name is available
        the slug incorporates the domain to avoid collisions
      - email (str) — lowercased
      - circles (str) — comma-joined for create_person_file's `circle` arg
      - relationship_type (str)
      - recruiter_signal_domain (str)
      - source_thread_id (str)
    """
    from_email = (entry.get("from_email") or "").strip().lower()
    from_header = entry.get("from_header") or ""
    display = _display_name_from_header(from_header, from_email)

    if display:
        name = display
        slug = _slugify(name)
    else:
        # No display name. Use the email local part as the name and include
        # the domain in the slug so jane@lever.co and jane@greenhouse-mail.io
        # don't collide.
        local, _, domain = from_email.partition("@")
        name = local or "unknown-recruiter"
        slug = _slugify(f"{local}-{domain}") if domain else _slugify(local)

    return {
        "name": name,
        "slug": slug,
        "email": from_email,
        "circles": RECRUITER_CIRCLES,
        "relationship_type": RECRUITER_RELATIONSHIP_TYPE,
        "recruiter_signal_domain": entry.get("recruiter_matched_domain", ""),
        "source_thread_id": entry.get("thread_id", ""),
    }


# ---------------------------------------------------------------------------
# Queue + log I/O
# ---------------------------------------------------------------------------


def _load_queue(queue_path: Path) -> dict:
    return json.loads(queue_path.read_text(encoding="utf-8"))


def _find_entry(queue: dict, thread_id: str) -> dict | None:
    for item in queue.get("queued") or []:
        if item.get("thread_id") == thread_id:
            return item
    return None


def _append_jsonl(path: Path, entry: dict) -> None:
    """Atomic-append: read existing, append one line, rewrite. JSONL files
    under `cache/` are small (dozens of entries over months) so full
    rewrite is cheaper than managing exclusive append locks across
    Dropbox + VPS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_rejected_recruiters(rejected_path: Path) -> set[str]:
    """Return the set of lowercased rejected sender emails. Absent file →
    empty set. Malformed lines are skipped silently.

    Used by inbox_triage_lib to short-circuit future inbounds from the
    same sender before the recruiter detector runs."""
    out: set[str] = set()
    for entry in _read_jsonl(rejected_path):
        addr = (entry.get("from_email") or "").strip().lower()
        if addr:
            out.add(addr)
    return out


# ---------------------------------------------------------------------------
# Keep — turn a cold-recruiter queue entry into a durable people file
# ---------------------------------------------------------------------------


def _is_already_kept(kept_path: Path, thread_id: str) -> dict | None:
    for entry in _read_jsonl(kept_path):
        if entry.get("thread_id") == thread_id:
            return entry
    return None


def _keep_recruiter(
    thread_id: str,
    *,
    people_dir: Path,
    workspace_dir: Path,
    queue_path: Path,
    now_iso: str,
) -> dict:
    kept_path = workspace_dir / "cache" / "kept-recruiters.jsonl"
    prior = _is_already_kept(kept_path, thread_id)
    if prior is not None:
        return {
            "status": "already_kept",
            "action": "keep",
            "slug": prior.get("slug", ""),
            "path": prior.get("path", ""),
        }

    queue = _load_queue(queue_path)
    entry = _find_entry(queue, thread_id)
    if entry is None:
        return {"status": "not_found", "action": "keep", "thread_id": thread_id}

    fields = derive_person_fields_from_queue_entry(entry)
    name = fields.pop("name")
    slug = fields.pop("slug")
    circles = fields.pop("circles")

    try:
        created = brain.create_person_file(
            name, circles, slug=slug, **fields,
        )
    except FileExistsError:
        # A people file with this slug already exists — a prior keep
        # landed on a separate thread, or the sender matches an existing
        # contact. Treat as already_kept.
        return {
            "status": "already_kept",
            "action": "keep",
            "slug": slug,
            "detail": "people file already exists at slug",
        }

    _append_jsonl(kept_path, {
        "thread_id": thread_id,
        "from_email": fields["email"],
        "slug": created["slug"],
        "path": created["path"],
        "kept_at": now_iso,
    })

    return {
        "status": "ok",
        "action": "keep",
        "slug": created["slug"],
        "path": created["path"],
        "name": name,
    }


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


def _is_already_rejected(rejected_path: Path, thread_id: str) -> dict | None:
    for entry in _read_jsonl(rejected_path):
        if entry.get("thread_id") == thread_id:
            return entry
    return None


def _reject_recruiter(
    thread_id: str,
    *,
    workspace_dir: Path,
    queue_path: Path,
    now_iso: str,
) -> dict:
    rejected_path = workspace_dir / "cache" / "rejected-recruiters.jsonl"
    prior = _is_already_rejected(rejected_path, thread_id)
    if prior is not None:
        return {
            "status": "already_rejected",
            "action": "reject",
            "from_email": prior.get("from_email", ""),
        }

    queue = _load_queue(queue_path)
    entry = _find_entry(queue, thread_id)
    if entry is None:
        return {"status": "not_found", "action": "reject", "thread_id": thread_id}

    _append_jsonl(rejected_path, {
        "thread_id": thread_id,
        "from_email": (entry.get("from_email") or "").strip().lower(),
        "subject": entry.get("subject", ""),
        "rejected_at": now_iso,
    })

    return {
        "status": "ok",
        "action": "reject",
        "from_email": (entry.get("from_email") or "").strip().lower(),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handle_recruiter_callback(
    action: str,
    arg: str,
    *,
    people_dir: Path,
    workspace_dir: Path,
    queue_path: Path,
    now_iso: str,
) -> dict[str, Any]:
    """Route a `recruiter:<action>:<thread_id>` callback.

    Never raises — returns a structured dict so the dispatcher can render
    a confirmation or failure message.
    """
    try:
        if action == "keep":
            return _keep_recruiter(
                arg,
                people_dir=people_dir,
                workspace_dir=workspace_dir,
                queue_path=queue_path,
                now_iso=now_iso,
            )
        if action == "reject":
            return _reject_recruiter(
                arg,
                workspace_dir=workspace_dir,
                queue_path=queue_path,
                now_iso=now_iso,
            )
        return {
            "status": "error",
            "action": action,
            "detail": f"unknown action: {action!r}",
        }
    except Exception as exc:  # noqa: BLE001 — surface all failures as structured
        return {"status": "error", "action": action, "detail": str(exc)}
