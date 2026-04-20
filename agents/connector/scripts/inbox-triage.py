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


_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root                  # noqa: E402
from flux_import_lib import build_email_to_slug_map                 # noqa: E402
from inbox_triage_lib import classify_thread_for_triage             # noqa: E402


BRIAN_ADDRESSES = {
    "sam.smith@example.com",
    "sam.smith+backup@example.com",
    "sam.smith+work@example.com",
}

DEFAULT_TOKEN = Path(os.path.expanduser("~/.clawford/connector-workspace/token.json"))
DEFAULT_CREDS = Path(os.path.expanduser("~/.clawford/connector-workspace/credentials.json"))
DEFAULT_QUEUE = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/triage-queue.json"))


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
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    people_dir = args.people_dir or (dropbox_brain_root() / "people")
    email_to_slug = build_email_to_slug_map(people_dir)
    print(f"People map: {len(email_to_slug)} email->slug entries from {people_dir}")

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

    print(f"Fetching threads: in:inbox newer_than:{args.window_days}d -from:me "
          f"(max {args.max_threads})")
    threads = fetch_recent_threads(service, args.window_days, args.max_threads)
    print(f"Fetched {len(threads)} threads.")
    print()

    buckets: dict[str, int] = {}
    queued: list[dict] = []
    for t in threads:
        result = classify_thread_for_triage(
            t,
            operator_emails=BRIAN_ADDRESSES,
            email_to_slug=email_to_slug,
        )
        buckets[result["status"]] = buckets.get(result["status"], 0) + 1
        if result["status"] == "queued":
            queued.append(result)
        elif args.verbose:
            print(f"  {result['status']:26s} {result.get('from_email','?'):40s}  "
                  f"{result.get('subject','')[:50]}")

    print("=" * 72)
    print("TRIAGE SUMMARY")
    print("=" * 72)
    for k in sorted(buckets.keys()):
        print(f"  {k:26s} {buckets[k]}")
    print()

    if queued:
        print(f"QUEUED ({len(queued)}):")
        for q in queued:
            print(f"  [{q['thread_id']}] {q['slug']:25s} <{q['from_email']}>")
            print(f"      subject:  {q['subject']}")
            print(f"      date:     {q['date']}")
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
    print(json.dumps({
        "status": "ok",
        "window_days": args.window_days,
        "threads_scanned": len(threads),
        "queued": len(queued),
        **buckets,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
