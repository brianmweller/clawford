#!/usr/bin/env python3
"""monthly-archival.py — Mr Fixit monthly brain archival.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:monthly-archival`. Pure-Python deterministic implementation
of the confidence decay archival logic.

Confidence decay formula:
    effective_confidence = original * 0.5 ** (days_since_recorded / half_life)

Category half-lives (days):
    identity     never (returns original confidence)
    established  365
    situation    90
    preference   180
    plan         30
    logistics    7
    rumor        14
    (unknown)    90  (default fallback)

Archival rules:
    - Facts: effective_confidence < 0.2 AND age > 90 days
    - Tasks: status == 'done' AND completed_at > 90 days ago

Destination: ~/Dropbox/openclaw-backup/archive/YYYY-MM/
  - facts-<source-stem>.md     (one per source facts file with archived rows)
  - tasks-queue.md             (archived task rows)
  - manifest.json              (count + ids + timestamp)

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.telegram_api import resolve_credentials, send_message  # noqa: E402


WORKSPACE = Path(os.path.expanduser("~/.openclaw/fix-it-workspace"))
CACHE_DIR = WORKSPACE / "cache"
LAST_RUN_FILE = CACHE_DIR / "last-monthly-archival.json"

BRAIN_DIR = Path(os.path.expanduser("~/Dropbox/openclaw-backup"))
FACTS_DIR = BRAIN_DIR / "facts"
TASKS_FILE = BRAIN_DIR / "tasks" / "queue.md"
ARCHIVE_ROOT = BRAIN_DIR / "archive"

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

CATEGORY_HALF_LIVES = {
    "identity": None,  # never decays
    "established": 365,
    "situation": 90,
    "preference": 180,
    "plan": 30,
    "logistics": 7,
    "rumor": 14,
}
DEFAULT_HALF_LIFE_DAYS = 90
ARCHIVAL_AGE_FLOOR_DAYS = 90
ARCHIVAL_CONFIDENCE_THRESHOLD = 0.2

_FIELD_RE = re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")


def effective_confidence(
    original: float, category: str, recorded: datetime, now: datetime
) -> float:
    if (category or "").lower() == "identity":
        return original
    half_life = CATEGORY_HALF_LIVES.get((category or "").lower(), DEFAULT_HALF_LIFE_DAYS)
    if half_life is None:
        return original
    days = (now - recorded).total_seconds() / 86400.0
    if days <= 0:
        return original
    return original * (0.5 ** (days / half_life))


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_blocks(text: str) -> list[tuple[dict, str]]:
    """Split a markdown file into blocks separated by `---` lines.
    Returns a list of (parsed_fields_dict, raw_block_text) tuples.
    Blocks without `id` or `description` are dropped (header noise)."""
    blocks: list[tuple[dict, str]] = []
    for raw_block in re.split(r"\n---\n", text):
        block = raw_block.strip()
        if not block:
            continue
        fields: dict = {}
        for line in block.splitlines():
            m = _FIELD_RE.match(line)
            if m:
                key = m.group(1).strip()
                value = m.group(2).strip()
                fields[key] = value
        if fields.get("id"):
            blocks.append((fields, raw_block))
    return blocks


def parse_facts_file(path: Path) -> list[dict]:
    """Parse a facts/*.md file and return one dict per entry."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    facts: list[dict] = []
    for fields, raw in _parse_blocks(text):
        try:
            confidence = float(fields.get("confidence", "0") or 0)
        except ValueError:
            confidence = 0.0
        facts.append({
            "id": fields["id"],
            "content": fields.get("content", ""),
            "subject": fields.get("subject", ""),
            "confidence": confidence,
            "category": fields.get("category", ""),
            "recorded_at": fields.get("recorded_at", ""),
            "recorded_at_dt": _parse_iso(fields.get("recorded_at", "")),
            "raw": raw,
        })
    return facts


def parse_tasks_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    tasks: list[dict] = []
    for fields, raw in _parse_blocks(text):
        tasks.append({
            "id": fields["id"],
            "description": fields.get("description", ""),
            "status": (fields.get("status") or "").lower(),
            "completed_at": fields.get("completed_at", ""),
            "completed_at_dt": _parse_iso(fields.get("completed_at", "")),
            "raw": raw,
        })
    return tasks


def select_facts_to_archive(facts: list[dict], now: datetime) -> tuple[list, list]:
    """Returns (to_archive, to_keep)."""
    to_archive: list = []
    to_keep: list = []
    floor = now - timedelta(days=ARCHIVAL_AGE_FLOOR_DAYS)
    for fact in facts:
        recorded = fact.get("recorded_at_dt")
        if recorded is None:
            to_keep.append(fact)
            continue
        if recorded > floor:
            to_keep.append(fact)
            continue
        eff = effective_confidence(
            fact.get("confidence", 0.0),
            fact.get("category", ""),
            recorded,
            now,
        )
        fact["effective_confidence"] = eff
        if eff < ARCHIVAL_CONFIDENCE_THRESHOLD:
            to_archive.append(fact)
        else:
            to_keep.append(fact)
    return to_archive, to_keep


def select_tasks_to_archive(tasks: list[dict], now: datetime) -> tuple[list, list]:
    """Returns (to_archive, to_keep)."""
    to_archive: list = []
    to_keep: list = []
    floor = now - timedelta(days=ARCHIVAL_AGE_FLOOR_DAYS)
    for task in tasks:
        if task.get("status") != "done":
            to_keep.append(task)
            continue
        completed = task.get("completed_at_dt")
        if completed is None or completed > floor:
            to_keep.append(task)
            continue
        to_archive.append(task)
    return to_archive, to_keep


def _rebuild_md_from_blocks(header: str, kept_blocks: list[str]) -> str:
    """Reassemble a markdown file from kept blocks. Preserves the
    header (everything up to the first `---` separator)."""
    if not kept_blocks:
        return header.rstrip() + "\n"
    body = "\n---\n".join(b.strip() for b in kept_blocks)
    return header.rstrip() + "\n\n---\n\n" + body + "\n\n---\n"


def _split_header(text: str) -> tuple[str, list[str]]:
    """Return (header_text_before_first_---, list_of_block_strings)."""
    parts = re.split(r"\n---\n", text)
    if not parts:
        return "", []
    header = parts[0]
    blocks = [p.strip() for p in parts[1:] if p.strip()]
    return header, blocks


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _archive_facts_file(
    src: Path, archive_dir: Path, now: datetime
) -> tuple[int, list[str]]:
    """Archive eligible facts from a single facts/*.md source file.
    Returns (count_archived, archived_ids)."""
    text = src.read_text(encoding="utf-8")
    facts = parse_facts_file(src)
    if not facts:
        return 0, []
    to_archive, to_keep = select_facts_to_archive(facts, now)
    if not to_archive:
        return 0, []

    # Write archive file
    archive_path = archive_dir / f"facts-{src.stem}.md"
    archive_lines = [f"# Archived facts from {src.name} (run {now.strftime('%Y-%m-%d')})", ""]
    for fact in to_archive:
        archive_lines.append("---")
        archive_lines.append("")
        archive_lines.append(fact["raw"].strip())
        archive_lines.append("")
    archive_text = "\n".join(archive_lines).rstrip() + "\n"
    _write_atomic(archive_path, archive_text)

    # Rebuild source file with kept entries
    header, _ = _split_header(text)
    kept_raws = [f["raw"] for f in to_keep]
    new_src = _rebuild_md_from_blocks(header, kept_raws)
    _write_atomic(src, new_src)

    return len(to_archive), [f["id"] for f in to_archive]


def _archive_tasks_file(
    src: Path, archive_dir: Path, now: datetime
) -> tuple[int, list[str]]:
    if not src.exists():
        return 0, []
    text = src.read_text(encoding="utf-8")
    tasks = parse_tasks_file(src)
    if not tasks:
        return 0, []
    to_archive, to_keep = select_tasks_to_archive(tasks, now)
    if not to_archive:
        return 0, []

    archive_path = archive_dir / "tasks-queue.md"
    archive_lines = [f"# Archived tasks from queue.md (run {now.strftime('%Y-%m-%d')})", ""]
    for task in to_archive:
        archive_lines.append("---")
        archive_lines.append("")
        archive_lines.append(task["raw"].strip())
        archive_lines.append("")
    archive_text = "\n".join(archive_lines).rstrip() + "\n"
    _write_atomic(archive_path, archive_text)

    header, _ = _split_header(text)
    kept_raws = [t["raw"] for t in to_keep]
    new_src = _rebuild_md_from_blocks(header, kept_raws)
    _write_atomic(src, new_src)

    return len(to_archive), [t["id"] for t in to_archive]


def run() -> dict:
    now = datetime.now(timezone.utc)
    archive_dir = ARCHIVE_ROOT / now.strftime("%Y-%m")
    archive_dir.mkdir(parents=True, exist_ok=True)

    facts_archived = 0
    facts_ids: list[str] = []
    if FACTS_DIR.exists():
        for src in sorted(FACTS_DIR.glob("*.md")):
            if src.name.startswith("_"):
                continue
            count, ids = _archive_facts_file(src, archive_dir, now)
            facts_archived += count
            facts_ids.extend(ids)

    tasks_archived, task_ids = _archive_tasks_file(TASKS_FILE, archive_dir, now)

    manifest = {
        "run_at": now.isoformat(),
        "facts_archived": facts_archived,
        "facts_ids": facts_ids,
        "tasks_archived": tasks_archived,
        "tasks_ids": task_ids,
    }
    _write_atomic(archive_dir / "manifest.json", json.dumps(manifest, indent=2))

    # Telegram report (this cron is announce=true)
    msg = (
        f"\U0001f9ea\U0001f527 Monthly archival complete\n"
        f"\u2022 Facts archived: {facts_archived}\n"
        f"\u2022 Tasks archived: {tasks_archived}\n"
        f"\u2022 Archive: {archive_dir}\n"
    )
    sent_count = 0
    token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
    if send_message(token, chat_id, msg, silent=False):
        sent_count = 1

    result = {
        "status": "ok",
        "facts_archived": facts_archived,
        "tasks_archived": tasks_archived,
        "archive_dir": str(archive_dir),
        "sent": sent_count,
    }
    _write_atomic(
        LAST_RUN_FILE,
        json.dumps({"timestamp": now.isoformat(), **result}, indent=2),
    )
    return result


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f9ea\U0001f527 monthly-archival failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
