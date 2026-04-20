#!/usr/bin/env python3
"""krisp-facts-mine.py — mine durable facts from Krisp meeting transcripts.

Daily cron. Scans
``~/.clawford/meetings-coach-workspace/cache/pending-debrief-*.json``,
skips all-hands meetings (>6 attendees) and transcripts whose attendees
map to zero known people, runs the shared fact extractor on each
transcript, and upserts high-confidence facts into the Huckle brain.
Low-confidence (0.3-0.6) facts also get flagged in
``brain/facts/_pending_review.md``.

Lives in meetings-coach so the cache read doesn't cross a workspace
boundary under bwrap (P1.2). Brain writes work because
``~/Dropbox/openclaw-backup`` is bind-mounted across all agents.

Safety: defaults to dry-run. Pass ``--commit`` to write.

Usage:
  python3 krisp-facts-mine.py --commit
  python3 krisp-facts-mine.py --max-age-days 7 --dry-run --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root                 # noqa: E402
from agents.shared.fact_extraction import (                        # noqa: E402
    append_pending_review,
    extract_facts_from_text,
)
from agents.shared.facts import upsert_fact                        # noqa: E402
from krisp_facts_mine_lib import (                                 # noqa: E402
    build_candidate_slugs,
    chunk_transcript,
    is_all_hands,
    load_debriefs,
)


BRIAN_ADDRESSES = {
    "sam.smith@example.com",
    "sam.smith+backup@example.com",
    "sam.smith+work@example.com",
}

DEFAULT_CACHE = Path(os.path.expanduser(
    "~/.clawford/meetings-coach-workspace/cache"
))
DEFAULT_CURSOR = Path(os.path.expanduser(
    "~/.clawford/meetings-coach-workspace/cache/krisp-mine-cursor.json"
))

TRANSCRIPT_CHUNK_CHARS = 8000


def _build_email_to_slug_map(people_dir: Path) -> dict[str, str]:
    """Inline slug map to avoid depending on connector's flux_import_lib
    (which could be in a different workspace under bwrap). Same parse
    logic as build_email_to_slug_map in flux_import_lib."""
    import re
    if not people_dir.exists():
        return {}
    email_re = re.compile(r"^\s*-\s*\*\*email(?::\*\*|\*\*:)\s*(.+?)\s*$")
    slug_re = re.compile(r"^\s*-\s*\*\*slug(?::\*\*|\*\*:)\s*(.+?)\s*$")
    out: dict[str, str] = {}
    for path in people_dir.glob("*.md"):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
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
        if not slug:
            slug = path.stem
        out[email.lower()] = slug
    return out


def run(
    *,
    cache_dir: Path,
    people_dir: Path,
    facts_dir: Path,
    cursor_path: Path,
    max_age_days: int,
    commit: bool,
    verbose: bool = False,
) -> dict:
    """Run one miner pass over pending-debrief files."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    email_to_slug = _build_email_to_slug_map(people_dir)
    debriefs = load_debriefs(cache_dir)

    stats = {
        "status": "ok",
        "transcripts_scanned": 0,
        "transcripts_skipped_all_hands": 0,
        "transcripts_skipped_no_candidates": 0,
        "transcripts_skipped_empty": 0,
        "facts_minted": 0,
        "skipped_dup": 0,
        "facts_flagged_low_conf": 0,
        "extract_errors": 0,
    }

    for d in debriefs:
        stats["transcripts_scanned"] += 1

        if is_all_hands(d):
            stats["transcripts_skipped_all_hands"] += 1
            continue

        slugs = build_candidate_slugs(
            d, email_to_slug=email_to_slug,
            operator_emails=BRIAN_ADDRESSES,
        )
        if not slugs:
            stats["transcripts_skipped_no_candidates"] += 1
            continue

        transcript_text = d.get("transcript_text") or ""
        chunks = chunk_transcript(transcript_text, max_chars=TRANSCRIPT_CHUNK_CHARS)
        if not chunks:
            stats["transcripts_skipped_empty"] += 1
            continue

        event_id = d.get("event_id") or ""
        attendee_emails = [
            (a.get("email") or "").lower()
            for a in (d.get("attendees") or [])
            if isinstance(a, dict) and a.get("email")
        ]

        for chunk in chunks:
            meta = {
                "source": "krisp",
                "event_id": event_id,
                "attendees": attendee_emails,
                "meeting_title": d.get("meeting_title") or "",
            }
            try:
                facts = extract_facts_from_text(
                    text=chunk,
                    source_context=meta,
                    candidate_slugs=slugs,
                )
            except Exception:
                stats["extract_errors"] += 1
                if verbose:
                    traceback.print_exc(file=sys.stderr)
                continue

            for f in facts:
                if commit:
                    result = upsert_fact(
                        facts_dir=facts_dir,
                        subject=f["subject"],
                        category=f["category"],
                        content=f["content"],
                        source_agent="meetings-coach",
                        source_type="mined",
                        source_detail=f["source_detail"],
                        confidence=f["confidence"],
                        idempotency_key=f["idempotency_key"],
                        recorded_at=now_iso,
                        audience_scope=f["audience_scope"],
                    )
                    if result["status"] == "skipped":
                        stats["skipped_dup"] += 1
                        continue
                    stats["facts_minted"] += 1
                    if f.get("needs_review"):
                        append_pending_review(
                            facts_dir,
                            {**f, "id": result["id"]},
                        )
                        stats["facts_flagged_low_conf"] += 1
                else:
                    stats["facts_minted"] += 1
                    if f.get("needs_review"):
                        stats["facts_flagged_low_conf"] += 1

    if commit:
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"last_run_at": now_iso}, indent=2), encoding="utf-8")
        tmp.replace(cursor_path)

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--cursor-path", type=Path, default=DEFAULT_CURSOR)
    ap.add_argument("--people-dir", type=Path, default=None)
    ap.add_argument("--facts-dir", type=Path, default=None)
    ap.add_argument("--max-age-days", type=int, default=30)
    ap.add_argument("--commit", action="store_true",
                    help="Write facts to the brain. Default is dry-run.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    brain = dropbox_brain_root()
    people_dir = args.people_dir or (brain / "people")
    facts_dir = args.facts_dir or (brain / "facts")

    commit = args.commit and not args.dry_run

    try:
        result = run(
            cache_dir=args.cache_dir,
            people_dir=people_dir,
            facts_dir=facts_dir,
            cursor_path=args.cursor_path,
            max_age_days=args.max_age_days,
            commit=commit,
            verbose=args.verbose,
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
    sys.exit(main(sys.argv[1:]))
