#!/usr/bin/env python3
"""gmail-facts-mine.py — mine durable facts from recent Gmail traffic.

Daily cron. Scans inbound + outbound mail within the cursor window
(fallback: last 24h), runs each message through the shared fact
extractor, and upserts high-confidence facts into the Huckle brain.
Low-confidence (0.3-0.6) facts also get flagged in
`brain/facts/_pending_review.md` for periodic triage.

Cursor lives at `~/.clawford/connector-workspace/cache/gmail-mine-cursor.json`.
Idempotency key: Gmail message-id + subject slug + content hash — safe
to re-run without duplicating brain entries.

Safety: defaults to dry-run. Pass `--commit` to actually write to the
brain. The cron launcher passes `--commit` explicitly.

Usage:
  python3 gmail-facts-mine.py --commit
  python3 gmail-facts-mine.py --window-days 1 --max-messages 50
  python3 gmail-facts-mine.py --dry-run --verbose
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
from agents.shared.gmail_api import extract_plain_body             # noqa: E402
from flux_import_lib import build_email_to_slug_map                # noqa: E402
from gmail_facts_mine_lib import (                                 # noqa: E402
    build_candidate_slugs,
    build_gmail_query,
    load_cursor,
    message_metadata,
    save_cursor,
)


BRIAN_ADDRESSES = {
    "sam.smith@example.com",
    "sam.smith+backup@example.com",
    "sam.smith+work@example.com",
}

DEFAULT_TOKEN = Path(os.path.expanduser("~/.clawford/connector-workspace/token.json"))
DEFAULT_CREDS = Path(os.path.expanduser("~/.clawford/connector-workspace/credentials.json"))
DEFAULT_CURSOR = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/cache/gmail-mine-cursor.json"
))


# ---------------------------------------------------------------------------
# Thin wrappers — monkeypatched in tests, exercised live in prod
# ---------------------------------------------------------------------------


def _build_service(token_path: Path, creds_path: Path):
    from googleapiclient.discovery import build
    from agents.shared.google_oauth import get_credentials

    scopes = [
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    creds = get_credentials(str(creds_path), str(token_path), scopes)
    return build("gmail", "v1", credentials=creds)


def _fetch_messages(service, q: str, max_messages: int) -> list[dict]:
    """List + fetch messages matching the query in format=full."""
    resp = service.users().messages().list(
        userId="me", q=q, maxResults=max_messages,
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    out = []
    for mid in ids:
        msg = service.users().messages().get(
            userId="me", id=mid, format="full",
        ).execute()
        out.append(msg)
    return out


def _extract_body(msg: dict) -> str:
    return extract_plain_body(msg)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    *,
    token_path: Path,
    creds_path: Path,
    people_dir: Path,
    facts_dir: Path,
    cursor_path: Path,
    window_days: int,
    max_messages: int,
    commit: bool,
    verbose: bool = False,
) -> dict:
    """Run one miner pass. Returns the SCRIPT_CONTRACT envelope as a dict."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = load_cursor(cursor_path)
    email_to_slug = build_email_to_slug_map(people_dir)
    query = build_gmail_query(
        cursor=cursor, fallback_days=window_days, now_iso=now_iso,
    )

    service = _build_service(token_path, creds_path)
    messages = _fetch_messages(service, query, max_messages)

    stats = {
        "status": "ok",
        "query": query,
        "messages_scanned": 0,
        "messages_skipped_no_candidates": 0,
        "facts_minted": 0,
        "facts_reinforced": 0,
        "skipped_dup": 0,
        "facts_flagged_low_conf": 0,
        "extract_errors": 0,
    }
    max_internal_date = cursor.get("last_internalDate") or "0"

    for msg in messages:
        stats["messages_scanned"] += 1
        internal_date = str(msg.get("internalDate") or "0")
        if internal_date and internal_date > str(max_internal_date):
            max_internal_date = internal_date

        slugs = build_candidate_slugs(
            msg,
            email_to_slug=email_to_slug,
            operator_emails=BRIAN_ADDRESSES,
        )
        if not slugs:
            stats["messages_skipped_no_candidates"] += 1
            continue

        body = _extract_body(msg)
        if not body.strip():
            stats["messages_skipped_no_candidates"] += 1
            continue

        meta = message_metadata(msg, operator_emails=BRIAN_ADDRESSES)
        try:
            facts = extract_facts_from_text(
                text=body,
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
                    source_agent="connector",
                    source_type="mined",
                    source_detail=f["source_detail"],
                    confidence=f["confidence"],
                    idempotency_key=f["idempotency_key"],
                    recorded_at=now_iso,
                    audience_scope=f["audience_scope"],
                )
                if result["status"] == "reinforced":
                    stats["facts_reinforced"] += 1
                    continue
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
        save_cursor(cursor_path, {
            "last_internalDate": str(max_internal_date),
            "last_run_at": now_iso,
        })

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    ap.add_argument("--creds", type=Path, default=DEFAULT_CREDS)
    ap.add_argument("--cursor-path", type=Path, default=DEFAULT_CURSOR)
    ap.add_argument("--people-dir", type=Path, default=None)
    ap.add_argument("--facts-dir", type=Path, default=None)
    ap.add_argument("--window-days", type=int, default=1,
                    help="Fallback window when cursor is empty or stale")
    ap.add_argument("--max-messages", type=int, default=200)
    ap.add_argument("--commit", action="store_true",
                    help="Write facts to the brain. Default is dry-run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Explicit dry-run (same as omitting --commit)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)  # None → argparse uses sys.argv[1:]

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
            token_path=args.token,
            creds_path=args.creds,
            people_dir=people_dir,
            facts_dir=facts_dir,
            cursor_path=args.cursor_path,
            window_days=args.window_days,
            max_messages=args.max_messages,
            commit=commit,
            verbose=args.verbose,
        )
    except Exception as exc:  # noqa: BLE001 — final-line envelope is the contract
        traceback.print_exc(file=sys.stderr)
        print()
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 0

    print()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
