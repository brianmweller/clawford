#!/usr/bin/env python3
"""inbox-triage.py — list Gmail threads that plausibly need a drafted reply.

Scans recent inbound threads, filters out service accounts, the operator's own
messages, threads the operator has already replied to, and unknown senders, and
outputs the remaining threads as a queue for draft-compose.py.

Flow:
  1. Load email→slug map from people/*.md.
  2. Query Gmail: in:inbox newer_than:{window_days}d -from:me.
  3. For each thread, fetch metadata (From/Date/Subject/Message-ID headers)
     of every message.
  4. classify_thread_for_triage() on each.
  5. Write queued entries to --queue-json (default cache/triage-queue.json).
  6. Print a human-readable summary + final JSON status envelope.

Uses existing gmail.readonly scope; no OAuth changes needed beyond the
compose refresh already done.

Usage:
  python3 inbox-triage.py --dry-run
  python3 inbox-triage.py --window-days 7
  python3 inbox-triage.py --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root                  # noqa: E402
from agents.shared.operator import load_operator                    # noqa: E402
from flux_import_lib import build_email_to_slug_map                 # noqa: E402
from inbox_triage_lib import (                                      # noqa: E402
    auto_promote_cold_recruiter,
    auto_promote_thread_continuity,
    build_queue_from_results,
    build_skipped_samples,
    classify_thread_for_triage,
    upsert_thread_in_queue,
)
from recruiter_callback_lib import load_rejected_recruiters         # noqa: E402

DEFAULT_TOKEN = Path(os.path.expanduser("~/.clawford/connector-workspace/token.json"))
DEFAULT_CREDS = Path(os.path.expanduser("~/.clawford/connector-workspace/credentials.json"))
DEFAULT_QUEUE = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/triage-queue.json"))
DEFAULT_REJECTED = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/rejected-recruiters.jsonl"))


def fetch_recent_threads(service, window_days: int, max_threads: int = 50) -> list[dict]:
    """List thread IDs of inbound threads within the window, then fetch
    each one with metadata format (headers only, cheap)."""
    q = f"in:inbox newer_than:{window_days}d -from:me"
    resp = service.users().threads().list(
        userId="me", q=q, maxResults=max_threads,
    ).execute()
    thread_ids = [t["id"] for t in resp.get("threads", [])]

    threads = []
    for tid in thread_ids:
        t = service.users().threads().get(
            userId="me", id=tid, format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date", "Message-ID"],
        ).execute()
        threads.append(t)
    return threads


def _fetch_thread_metadata(service, thread_id: str) -> dict:
    """Single-thread fetch — used by --thread-id mode when the
    gmail-push-listener triggers triage on a specific Pub/Sub event."""
    return service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "To", "Subject", "Date", "Message-ID"],
    ).execute()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--max-threads", type=int, default=50)
    ap.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    ap.add_argument("--creds", type=Path, default=DEFAULT_CREDS)
    ap.add_argument("--queue-json", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--people-dir", type=Path, default=None,
                    help="Override brain/people dir (for testing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write queue file; just print")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--thread-id",
                    help="Single-thread mode: classify just this Gmail thread "
                         "and upsert it into the existing queue. Used by the "
                         "real-time push listener.")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    people_dir = args.people_dir or (dropbox_brain_root() / "people")
    email_to_slug = build_email_to_slug_map(people_dir)
    print(f"People map: {len(email_to_slug)} email->slug entries from {people_dir}")

    rejected_recruiters = load_rejected_recruiters(DEFAULT_REJECTED)
    if rejected_recruiters:
        print(f"Rejected recruiters: {len(rejected_recruiters)} senders (short-circuit)")

    if not args.token.exists():
        print(f"ERROR: Gmail token not found at {args.token}", file=sys.stderr)
        print("Run: python3 gcal-auth.py", file=sys.stderr)
        return 1

    from googleapiclient.discovery import build                     # noqa: F401
    from agents.shared.google_oauth import get_credentials

    scopes = [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    creds = get_credentials(str(args.creds), str(args.token), scopes)
    service = build("gmail", "v1", credentials=creds)

    # ---- single-thread mode ----
    if args.thread_id:
        print(f"Single-thread mode: fetching {args.thread_id}")
        thread = _fetch_thread_metadata(service, args.thread_id)
        brian_addrs = load_operator().emails
        result = classify_thread_for_triage(
            thread,
            operator_emails=brian_addrs,
            email_to_slug=email_to_slug,
            rejected_recruiters=rejected_recruiters,
        )
        print(f"  classified: {result['status']}")
        if result["status"] == "queued":
            print(f"    slug={result['slug']}  from={result['from_email']}")
            print(f"    subject={result.get('subject','')[:80]}")
        elif result["status"] == "queued_cold_recruiter":
            promo = auto_promote_cold_recruiter(result, operator_emails=brian_addrs)
            if promo["status"] == "created":
                print(f"    auto-promoted: {promo['slug']} (next inbound queues as known)")
            elif promo["status"] == "already_exists":
                print(f"    auto-promote: slug {promo['slug']} already exists (no-op)")
        elif result.get("thread_continuity"):
            # Backstop: the operator engaged on this thread but sender wasn't
            # in people brain yet. Promote so draft-compose can load.
            promo = auto_promote_thread_continuity(result, operator_emails=brian_addrs)
            if promo["status"] == "created":
                print(f"    thread-continuity promoted: {promo['slug']}")

        if not args.dry_run:
            existing_queue: dict = {}
            if args.queue_json.exists():
                try:
                    existing_queue = json.loads(args.queue_json.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing_queue = {}
            new_queue = upsert_thread_in_queue(existing_queue, result)
            args.queue_json.parent.mkdir(parents=True, exist_ok=True)
            args.queue_json.write_text(
                json.dumps(new_queue, indent=2),
                encoding="utf-8",
            )
            print(f"Queue updated at {args.queue_json} "
                  f"({len(new_queue.get('queued', []))} entries)")

        print()
        print(json.dumps({
            "status": "ok",
            "mode": "single-thread",
            "thread_id": args.thread_id,
            "classification": result["status"],
        }))
        return 0

    # ---- full-scan mode (existing behaviour) ----
    print(f"Fetching threads: in:inbox newer_than:{args.window_days}d -from:me "
          f"(max {args.max_threads})")
    threads = fetch_recent_threads(service, args.window_days, args.max_threads)
    print(f"Fetched {len(threads)} threads.")
    print()

    buckets: dict[str, int] = {}
    classified_results: list[dict] = []
    brian_addrs = load_operator().emails
    promoted_count = 0
    for t in threads:
        result = classify_thread_for_triage(
            t,
            operator_emails=brian_addrs,
            email_to_slug=email_to_slug,
            rejected_recruiters=rejected_recruiters,
        )
        buckets[result["status"]] = buckets.get(result["status"], 0) + 1
        classified_results.append(result)
        # Auto-promote cold recruiters into the people brain so the next
        # message in the same thread is recognized as `queued` without
        # re-running the recruiter detector. Idempotent on re-classify.
        if not args.dry_run:
            if result["status"] == "queued_cold_recruiter":
                promo = auto_promote_cold_recruiter(result, operator_emails=brian_addrs)
                if promo["status"] == "created":
                    promoted_count += 1
            elif result.get("thread_continuity"):
                # Backstop for senders the operator engaged with before
                # gmail-sent-mine got around to creating the stub.
                promo = auto_promote_thread_continuity(result, operator_emails=brian_addrs)
                if promo["status"] == "created":
                    promoted_count += 1
        # Verbose-print non-persisted statuses so the operator can see
        # what got filtered. queued + queued_cold_recruiter are persisted
        # and listed in the QUEUED block below.
        if result["status"] not in ("queued", "queued_cold_recruiter") and args.verbose:
            print(f"  {result['status']:26s} {result.get('from_email','?'):40s}  "
                  f"{result.get('subject','')[:50]}")

    queue_dict = build_queue_from_results(classified_results)
    queued = queue_dict["queued"]
    if promoted_count:
        print(f"Auto-promoted {promoted_count} cold-recruiter sender(s) to people brain")

    print("=" * 72)
    print("TRIAGE SUMMARY")
    print("=" * 72)
    for k in sorted(buckets.keys()):
        print(f"  {k:26s} {buckets[k]}")
    print()

    if queued:
        print(f"QUEUED ({len(queued)}):")
        for q in queued:
            is_cold = q.get("status") == "queued_cold_recruiter"
            label = q.get("slug") or ("(cold-recruiter)" if is_cold else "?")
            print(f"  [{q['thread_id']}] {label:25s} <{q.get('from_email','?')}>")
            print(f"      subject:  {q.get('subject','')}")
            print(f"      date:     {q.get('date','')}")
            print(f"      snippet:  {q.get('snippet','')[:140]}")
            print()
    else:
        print("(nothing to draft)")

    if not args.dry_run and queued:
        args.queue_json.parent.mkdir(parents=True, exist_ok=True)
        args.queue_json.write_text(
            json.dumps({"queued": queued, "generated_at": None}, indent=2),
            encoding="utf-8",
        )
        print(f"Wrote queue to {args.queue_json}")

    print()
    envelope = {
        "status": "ok",
        "window_days": args.window_days,
        "threads_scanned": len(threads),
        "queued": len(queued),
        **buckets,
    }
    if promoted_count:
        envelope["auto_promoted"] = promoted_count
    samples = build_skipped_samples(classified_results, max_samples=10)
    if samples:
        envelope["skipped_samples"] = samples
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
