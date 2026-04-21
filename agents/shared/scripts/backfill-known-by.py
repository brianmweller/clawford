#!/usr/bin/env python3
"""backfill-known-by.py — one-time pass to populate the `known_by` list
on every fact that pre-dates the Phase 5b miner wiring.

Resolvers per source:
  - Flux numeric → Flux SQLite messages table
  - gmail:<id>   → Gmail API From/To/Cc
  - everything else → skipped (no participant backref)

Non-destructive:
  - Dry-run by default. Emits ``facts/_backfill-known-by-proposed.md``
    with one block per fact showing the old source_detail + proposed
    known_by + resolver used.
  - --commit: tars ``facts/`` to a dated backup tarball FIRST, then
    atomically rewrites each month file with the spliced known_by lines.
    Idempotent — blocks that already carry known_by are left alone.

Run locally (Flux SQLite is Windows-local; ~/Dropbox/openclaw-backup
syncs to the VPS via Dropbox). Gmail resolver is optional — if the
token isn't reachable locally, the 9 gmail-source facts stay
unresolved and will fill in on their next organic miner run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tarfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parent.parent.parent))  # repo root

from backfill_known_by_lib import (  # type: ignore
    detect_source_type,
    resolve_flux_numeric,
    resolve_gmail,
    rewrite_block_with_known_by,
)


_FIELD_RE = re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")

DEFAULT_FACTS_DIR_VPS = Path(os.path.expanduser("~/Dropbox/openclaw-backup/facts"))
DEFAULT_FACTS_DIR_LOCAL = Path("E:/Dropbox/openclaw-backup/facts")
DEFAULT_PEOPLE_DIR_VPS = Path(os.path.expanduser("~/Dropbox/openclaw-backup/people"))
DEFAULT_PEOPLE_DIR_LOCAL = Path("E:/Dropbox/openclaw-backup/people")
DEFAULT_FLUX_DB = Path("E:/Dropbox/Startup/Flux/data/flux.db")
DEFAULT_BACKUP_DIR = Path(os.path.expanduser("~/.clawford/deploy-backups"))
PROPOSAL_FILENAME = "_backfill-known-by-proposed.md"


def _default_facts_dir() -> Path:
    # Prefer the local path when it exists (running on the operator's laptop);
    # fall back to the VPS expansion. Lets the CLI be rerun in either env.
    if DEFAULT_FACTS_DIR_LOCAL.exists():
        return DEFAULT_FACTS_DIR_LOCAL
    return DEFAULT_FACTS_DIR_VPS


def _default_people_dir() -> Path:
    if DEFAULT_PEOPLE_DIR_LOCAL.exists():
        return DEFAULT_PEOPLE_DIR_LOCAL
    return DEFAULT_PEOPLE_DIR_VPS


def _fields_from_block(block: str) -> dict:
    fields: dict = {}
    for line in block.splitlines():
        m = _FIELD_RE.match(line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def _operator_emails() -> set[str]:
    try:
        from agents.shared.operator import load_operator
        return {a.lower() for a in load_operator().emails}
    except Exception:  # noqa: BLE001
        return {"sam.smith@example.com", "sam.smith+work@example.com"}


def _email_to_slug_map(people_dir: Path) -> dict[str, str]:
    try:
        from agents.connector.scripts.flux_import_lib import build_email_to_slug_map  # type: ignore
    except ImportError:
        # Fallback inline — same pattern as flux_import_lib.
        out: dict[str, str] = {}
        email_re = re.compile(r"^\s*-\s*\*\*email(?::\*\*|\*\*:)\s*(.+?)\s*$")
        slug_re = re.compile(r"^\s*-\s*\*\*slug(?::\*\*|\*\*:)\s*(.+?)\s*$")
        if not people_dir.exists():
            return {}
        for p in sorted(people_dir.glob("*.md")):
            if p.name.startswith("_"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            email = None
            slug = None
            for line in text.splitlines():
                if email is None:
                    m = email_re.match(line)
                    if m:
                        email = m.group(1).strip()
                if slug is None:
                    m = slug_re.match(line)
                    if m:
                        slug = m.group(1).strip()
                if email and slug:
                    break
            if not email or email in {"—", "-", ""} or "@" not in email:
                continue
            out[email.lower()] = slug or p.stem
        return out
    return build_email_to_slug_map(people_dir)


def _build_gmail_service():
    try:
        from agents.shared.gmail_api import build_gmail_service  # type: ignore
    except ImportError:
        return None
    token = Path(os.path.expanduser(
        "~/.clawford/connector-workspace/token.json"))
    creds = Path(os.path.expanduser(
        "~/.clawford/connector-workspace/credentials.json"))
    if not token.exists() or not creds.exists():
        return None
    try:
        return build_gmail_service(
            str(token), str(creds),
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
    except Exception:  # noqa: BLE001
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tarball_backup(facts_dir: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"facts-pre-backfill-known-by-{ts}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        tar.add(facts_dir, arcname=facts_dir.name)
    return path


def _format_proposal(
    fact_id: str,
    subject: str,
    source_detail: str,
    source_type: str,
    known_by: list[str] | None,
    status: str,
) -> str:
    return (
        "\n---\n\n"
        f"- **id:** {fact_id}\n"
        f"- **subject:** {subject}\n"
        f"- **source_detail:** {source_detail}\n"
        f"- **source_type:** {source_type}\n"
        f"- **known_by:** {json.dumps(known_by) if known_by is not None else '(none)'}\n"
        f"- **status:** {status}\n"
    )


def run(
    *,
    facts_dir: Path,
    people_dir: Path,
    flux_db: Path | None,
    enable_gmail: bool,
    commit: bool,
    max_facts: int | None,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
) -> dict:
    stats = {
        "status": "ok",
        "total_facts": 0,
        "already_typed": 0,
        "by_source_type": {},
        "resolved": 0,
        "unresolved": 0,
        "skipped_type": 0,
        "facts_written": 0,
    }

    if not facts_dir.exists():
        stats["status"] = "error"
        stats["error"] = f"facts dir missing: {facts_dir}"
        return stats

    email_to_slug = _email_to_slug_map(people_dir)
    operator = _operator_emails()

    flux_conn: sqlite3.Connection | None = None
    if flux_db and flux_db.exists():
        try:
            flux_conn = sqlite3.connect(str(flux_db))
        except sqlite3.Error:
            flux_conn = None

    gmail_service = _build_gmail_service() if enable_gmail else None

    proposals_by_file: dict[Path, dict[str, list[str]]] = {}
    proposal_blocks: list[str] = []

    for path in sorted(facts_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        blocks = text.split("\n---\n")
        for b in blocks:
            fields = _fields_from_block(b)
            fid = fields.get("id", "")
            if not fid:
                continue
            stats["total_facts"] += 1
            if fields.get("known_by"):
                stats["already_typed"] += 1
                continue
            if max_facts is not None and stats["resolved"] >= max_facts:
                continue

            source_detail = fields.get("source_detail", "")
            source_type = detect_source_type(source_detail)
            stats["by_source_type"].setdefault(source_type, 0)
            stats["by_source_type"][source_type] += 1

            known_by: list[str] | None = None
            if source_type == "flux_numeric" and flux_conn is not None:
                known_by = resolve_flux_numeric(
                    flux_conn, source_detail,
                    email_to_slug=email_to_slug, operator_emails=operator,
                )
            elif source_type == "gmail" and gmail_service is not None:
                known_by = resolve_gmail(
                    gmail_service, source_detail,
                    email_to_slug=email_to_slug, operator_emails=operator,
                )
            # Unresolvable source types get None.

            if known_by is None:
                stats["unresolved"] += 1
                status = f"unresolved/{source_type}"
            elif not known_by:
                stats["unresolved"] += 1
                status = f"empty/{source_type}"
            else:
                stats["resolved"] += 1
                status = f"resolved/{source_type}"
                proposals_by_file.setdefault(path, {})[fid] = known_by

            proposal_blocks.append(_format_proposal(
                fid, fields.get("subject", ""), source_detail,
                source_type, known_by, status,
            ))

    header = (
        f"# Proposed known_by backfill — {_now_iso()}\n\n"
        f"- total: {stats['total_facts']}\n"
        f"- already_typed: {stats['already_typed']}\n"
        f"- resolved (would write): {stats['resolved']}\n"
        f"- unresolved (skipped): {stats['unresolved']}\n"
        f"- by_source_type: {json.dumps(stats['by_source_type'])}\n"
    )
    proposal_path = facts_dir / PROPOSAL_FILENAME
    proposal_path.write_text(header + "".join(proposal_blocks), encoding="utf-8")

    if flux_conn is not None:
        flux_conn.close()

    if not commit:
        stats["mode"] = "dry_run"
        stats["proposal_path"] = str(proposal_path)
        return stats

    # Commit: tarball + atomic per-file rewrite.
    backup = _tarball_backup(facts_dir, backup_dir)
    stats["backup"] = str(backup)

    for path, applications in proposals_by_file.items():
        text = path.read_text(encoding="utf-8")
        blocks = text.split("\n---\n")
        new_blocks: list[str] = []
        touched = 0
        for b in blocks:
            fields = _fields_from_block(b)
            fid = fields.get("id", "")
            if fid in applications and not fields.get("known_by"):
                b = rewrite_block_with_known_by(b, applications[fid])
                touched += 1
            new_blocks.append(b)
        if touched:
            new_text = "\n---\n".join(new_blocks)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, path)
            stats["facts_written"] += touched

    stats["mode"] = "commit"
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-dir", type=Path, default=None)
    ap.add_argument("--people-dir", type=Path, default=None)
    ap.add_argument("--flux-db", type=Path, default=DEFAULT_FLUX_DB)
    ap.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    ap.add_argument("--enable-gmail", action="store_true",
                    help="Resolve gmail: sources via Gmail API (needs token)")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--max", type=int, default=None,
                    help="Cap resolved writes for staged runs")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    facts_dir = args.facts_dir or _default_facts_dir()
    people_dir = args.people_dir or _default_people_dir()

    try:
        result = run(
            facts_dir=facts_dir,
            people_dir=people_dir,
            flux_db=args.flux_db,
            enable_gmail=args.enable_gmail,
            commit=args.commit,
            max_facts=args.max,
            backup_dir=args.backup_dir,
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print()
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 0

    print()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
