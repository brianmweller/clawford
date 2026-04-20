#!/usr/bin/env python3
"""facts-import-flux.py — one-time import of Flux's knowledge_facts into
Huckle Cat's fact store.

This is a BOOTSTRAP. Long-term Huckle needs its own mining pipeline
(Codex-backed, VPS-side) so the brain stays fresh after the one-time
import. See memory: project_huckle_mining_roadmap.md.

Flow:
  1. Open Flux SQLite (read-only).
  2. SELECT active rows from knowledge_facts.
  3. Build email → slug map from Huckle's people/*.md.
  4. For each row, map to Huckle fact kwargs; skip if subject unknown
     or content empty.
  5. Call upsert_fact() (idempotent on flux id via idempotency_key).
  6. Report: imported / skipped_unknown_subject / skipped_idempotent /
     skipped_empty / total.

Idempotent. Safe to re-run. Dry-run option shows what WOULD be written
without touching disk.

Usage:
  python3 facts-import-flux.py --dry-run
  python3 facts-import-flux.py                          # actual import
  python3 facts-import-flux.py --max 100                # cap for safe rollout
  python3 facts-import-flux.py --flux-db /path/to/db    # override default

Prints a final JSON status line for the OpenClaw SCRIPT_CONTRACT shape
when run via cron (unused today — this is manual-only for MVP).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root  # noqa: E402
from agents.shared.facts import upsert_fact, parse_facts_file  # noqa: E402
from flux_import_lib import (  # noqa: E402
    build_email_to_slug_map,
    flux_row_to_huckle_fact,
)


DEFAULT_FLUX_DB = Path("E:/Dropbox/Startup/Flux/data/flux.db")
BRAIN_ROOT = dropbox_brain_root()


def iter_flux_facts(db_path: Path, max_rows: int | None = None):
    """Yield dicts of Flux active fact rows."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    q = """
        SELECT id, fact_type, subject, subject_address, content,
               source_type, source_id, audience_scope, confidence,
               status, established_at, created_at
        FROM knowledge_facts
        WHERE status = 'active'
        ORDER BY id
    """
    if max_rows is not None:
        q += f" LIMIT {int(max_rows)}"
    for row in cur.execute(q):
        yield dict(row)
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flux-db", type=Path, default=DEFAULT_FLUX_DB)
    ap.add_argument("--people-dir", type=Path, default=BRAIN_ROOT / "people")
    ap.add_argument("--facts-dir", type=Path, default=BRAIN_ROOT / "facts")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, help="cap on rows processed")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not args.flux_db.exists():
        print(f"ERROR: Flux DB not found at {args.flux_db}", file=sys.stderr)
        return 1

    # Force UTF-8 stdout on Windows so em-dashes/arrows in content don't crash cp1252
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    email_to_slug = build_email_to_slug_map(args.people_dir)
    print(f"Loaded {len(email_to_slug)} email->slug mappings from {args.people_dir}")
    print(f"Flux DB: {args.flux_db}")
    print(f"Facts out: {args.facts_dir}{' (DRY RUN)' if args.dry_run else ''}")
    if args.max:
        print(f"Max rows: {args.max}")
    print()

    totals = {
        "total_scanned": 0,
        "imported": 0,
        "skipped_idempotent": 0,
        "skipped_unknown_subject": 0,
        "skipped_empty_content": 0,
        "skipped_bad_row": 0,
    }
    by_subject: dict[str, int] = {}

    for row in iter_flux_facts(args.flux_db, max_rows=args.max):
        totals["total_scanned"] += 1

        kwargs = flux_row_to_huckle_fact(row, email_to_slug)
        if kwargs is None:
            if not row.get("subject_address"):
                totals["skipped_bad_row"] += 1
            elif not (row.get("content") or "").strip():
                totals["skipped_empty_content"] += 1
            else:
                totals["skipped_unknown_subject"] += 1
            continue

        if args.dry_run:
            totals["imported"] += 1
            by_subject[kwargs["subject"]] = by_subject.get(kwargs["subject"], 0) + 1
            if args.verbose:
                print(f"  WOULD IMPORT: [{row['id']}] {kwargs['subject']}: {kwargs['content'][:80]}")
            continue

        result = upsert_fact(facts_dir=args.facts_dir, **kwargs)
        if result["status"] == "created":
            totals["imported"] += 1
            by_subject[kwargs["subject"]] = by_subject.get(kwargs["subject"], 0) + 1
        else:
            totals["skipped_idempotent"] += 1

    print("=" * 64)
    print("RESULTS")
    print("=" * 64)
    for k, v in totals.items():
        print(f"  {k:28s} {v}")
    print()
    print(f"  distinct_subjects_touched   {len(by_subject)}")

    top = sorted(by_subject.items(), key=lambda kv: -kv[1])[:10]
    if top:
        print()
        print("Top 10 subjects by fact count:")
        for slug, count in top:
            print(f"  {count:4d}  {slug}")

    # Status envelope (unused but cron-ready)
    print()
    print(json.dumps({
        "status": "ok",
        "dry_run": args.dry_run,
        **totals,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
