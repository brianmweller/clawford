#!/usr/bin/env python3
"""people-expand-from-flux.py — auto-generate minimal Huckle people/*.md
stubs from Flux subjects Huckle doesn't yet know about.

Prerequisite for a richer facts-import-flux run: without expansion, only
~300 of Flux's ~7000 facts land in Huckle (because most subjects have no
people/ entry). After expansion, ~2000 facts become importable.

Flow:
  1. Query Flux for distinct (subject, subject_address, fact_count,
     most_recent_fact_date) across active rows.
  2. Drop rows where subject_address is a service account, the operator's own,
     or below --min-facts threshold.
  3. Drop rows whose email already maps to a Huckle person.
  4. Drop rows whose derived slug collides with an existing file.
  5. Write minimal stubs (--dry-run prints without writing).

Each generated stub is tagged `auto_generated: flux-import-YYYY-MM-DD`
so the operator can find them in a later review pass to refine
circles/relationship_type/tone.

Usage:
  python3 people-expand-from-flux.py --dry-run
  python3 people-expand-from-flux.py --dry-run --min-facts 5
  python3 people-expand-from-flux.py                         # actual write
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root  # noqa: E402
from flux_import_lib import (  # noqa: E402
    build_email_to_slug_map,
    is_likely_service_account,
    is_person_name,
    is_self_subject,
    flux_subject_to_person_stub,
    format_person_md,
    slugify,
)


DEFAULT_FLUX_DB = Path("E:/Dropbox/Startup/Flux/data/flux.db")
BRAIN_ROOT = dropbox_brain_root()


def query_flux_candidates(db_path: Path) -> list[dict]:
    """Return one row per (most-common-subject, subject_address) in Flux,
    with fact count and most recent established_at."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Pick the subject name most frequently associated with each address.
    # Group by address, take any top-frequency subject. SQLite-friendly form:
    cur.execute(
        """
        WITH ranked AS (
            SELECT subject_address,
                   subject,
                   COUNT(*) AS subject_hits,
                   ROW_NUMBER() OVER (
                       PARTITION BY subject_address
                       ORDER BY COUNT(*) DESC, subject
                   ) AS rn
            FROM knowledge_facts
            WHERE status = 'active' AND subject_address IS NOT NULL
            GROUP BY subject_address, subject
        ),
        totals AS (
            SELECT subject_address,
                   COUNT(*) AS fact_count,
                   MAX(established_at) AS last_fact_date
            FROM knowledge_facts
            WHERE status = 'active' AND subject_address IS NOT NULL
            GROUP BY subject_address
        )
        SELECT t.subject_address,
               r.subject AS subject_name,
               t.fact_count,
               t.last_fact_date
        FROM totals t
        JOIN ranked r ON r.subject_address = t.subject_address AND r.rn = 1
        ORDER BY t.fact_count DESC
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flux-db", type=Path, default=DEFAULT_FLUX_DB)
    ap.add_argument("--people-dir", type=Path, default=BRAIN_ROOT / "people")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-facts", type=int, default=3,
                    help="skip subjects with fewer than N facts in Flux")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    if not args.flux_db.exists():
        print(f"ERROR: Flux DB not found at {args.flux_db}", file=sys.stderr)
        return 1
    args.people_dir.mkdir(parents=True, exist_ok=True)

    existing_email_to_slug = build_email_to_slug_map(args.people_dir)
    existing_slugs = {p.stem for p in args.people_dir.glob("*.md") if not p.name.startswith("_")}

    print(f"Existing Huckle people: {len(existing_slugs)} files, "
          f"{len(existing_email_to_slug)} with emails")

    candidates = query_flux_candidates(args.flux_db)
    print(f"Flux candidates scanned: {len(candidates)}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    to_create: list[tuple[dict, str]] = []
    buckets = {
        "already_in_huckle": 0,
        "service_account": 0,
        "self_subject": 0,
        "topic_not_person": 0,
        "below_min_facts": 0,
        "slug_collision": 0,
        "missing_name": 0,
        "to_create": 0,
    }

    for row in candidates:
        addr = (row["subject_address"] or "").lower().strip()
        name = (row["subject_name"] or "").strip()
        n = row["fact_count"]

        if n < args.min_facts:
            buckets["below_min_facts"] += 1
            continue
        if is_self_subject(name):
            buckets["self_subject"] += 1
            continue
        if is_likely_service_account(addr, name):
            buckets["service_account"] += 1
            continue
        if not is_person_name(name):
            buckets["topic_not_person"] += 1
            if args.verbose:
                print(f"  TOPIC:   {name!r}  <{addr}>  ({n} facts)")
            continue
        if addr in existing_email_to_slug:
            buckets["already_in_huckle"] += 1
            continue
        if not name:
            buckets["missing_name"] += 1
            continue
        slug = slugify(name)
        if not slug:
            buckets["missing_name"] += 1
            continue
        if slug in existing_slugs:
            buckets["slug_collision"] += 1
            if args.verbose:
                print(f"  COLLIDE: {slug}  (existing file; address {addr} not yet linked)")
            continue

        stub = flux_subject_to_person_stub(
            flux_name=name,
            email=addr,
            fact_count=n,
            last_fact_date=(row["last_fact_date"] or "")[:10],
        )
        md = format_person_md(stub, generated_on=today)
        to_create.append((stub, md))
        buckets["to_create"] += 1

    print()
    print("=" * 64)
    print("SUMMARY")
    print("=" * 64)
    for k, v in buckets.items():
        print(f"  {k:22s} {v}")

    if not to_create:
        print("\nNothing to create.")
        print(json.dumps({"status": "ok", "dry_run": args.dry_run, **buckets}))
        return 0

    print()
    print(f"Sample (first 15 of {len(to_create)}):")
    for stub, _ in to_create[:15]:
        print(f"  [{stub['fact_count']:3d} facts, last {stub['last_fact_date'] or '—'}]  "
              f"{stub['slug']:30s}  <{stub['email']}>  ({stub['full_name']})")

    if args.dry_run:
        print("\nDRY RUN — no files written.")
        print(json.dumps({"status": "ok", "dry_run": True, **buckets}))
        return 0

    # Real write — belt-and-suspenders collision guard in case of races
    written = 0
    for stub, md in to_create:
        path = args.people_dir / f"{stub['slug']}.md"
        if path.exists():
            continue
        path.write_text(md, encoding="utf-8")
        written += 1

    print(f"\nWrote {written} new people files.")
    print(json.dumps({"status": "ok", "dry_run": False, "written": written, **buckets}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
