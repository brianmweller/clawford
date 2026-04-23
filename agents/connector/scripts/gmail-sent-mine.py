#!/usr/bin/env python3
"""gmail-sent-mine.py — stamp last_interaction from the operator's Sent folder.

Huckle's cadence engine only knew about Google Messages (SMS) and the
Krisp/GCal signal merge in daily-refresh. Email — the dominant medium
for Example Corp colleagues, many friends, and family logistics — was
invisible. That caused the 2026-04-21 complaint: Thomas and Yendrick
kept surfacing in the morning nudge even though the operator had emailed
them days before.

This cron walks the Sent folder on a 2-hour cadence, maps each
recipient email to a people/<slug>.md file, and stamps
last_interaction = message_date via the shared
daily-refresh.update_last_interaction helper (max-merged so fresher
signals aren't rewound).

Scope:
  - Sent side only. Inbound X→the operator is a separate cron.
  - To + Cc. Bcc is intentionally omitted — Gmail's Sent headers
    don't reliably carry it.
  - Skip noreply/bounce recipients.

Cursor: ~/.clawford/connector-workspace/cache/gmail-sent-mine-cursor.json
Bootstrap window (no cursor): last 90 days.

SCRIPT_CONTRACT-compliant: exits 0, prints one JSON line on the last
line of stdout.

Usage:
  python3 gmail-sent-mine.py --commit
  python3 gmail-sent-mine.py --window-days 90 --max-messages 200
  python3 gmail-sent-mine.py --dry-run --verbose
"""
from __future__ import annotations

import argparse
import importlib.util
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

from agents.shared.brain import dropbox_brain_root              # noqa: E402
from agents.shared.operator import load_operator                # noqa: E402
from flux_import_lib import build_email_to_slug_map             # noqa: E402
from gmail_sent_mine_lib import (                               # noqa: E402
    build_gmail_query,
    extract_recipient_emails,
    internal_date_to_iso_date,
    load_cursor,
    save_cursor,
)


DEFAULT_TOKEN = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/token.json"
))
DEFAULT_CREDS = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/credentials.json"
))
DEFAULT_CURSOR = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/cache/gmail-sent-mine-cursor.json"
))
DEFAULT_SUMMARY = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/cache/mined-gmail-sent.json"
))

# 90-day bootstrap window. Large enough to catch "we talk every couple
# of months" cadences, small enough that the first run doesn't hammer
# Gmail API quota.
DEFAULT_WINDOW_DAYS = 90


# ---------------------------------------------------------------------------
# Injectable wrappers — monkeypatched in tests
# ---------------------------------------------------------------------------


def _build_service(token_path: Path, creds_path: Path):
    from googleapiclient.discovery import build
    from agents.shared.google_oauth import get_credentials

    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = get_credentials(str(creds_path), str(token_path), scopes)
    return build("gmail", "v1", credentials=creds)


def _fetch_messages(service, q: str, max_messages: int) -> list[dict]:
    """List messages matching q, then fetch each in metadata format
    (headers only — we don't need bodies for this cron)."""
    resp = service.users().messages().list(
        userId="me", q=q, maxResults=max_messages,
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    out: list[dict] = []
    for mid in ids:
        msg = service.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["To", "Cc", "Bcc", "From", "Date", "Subject"],
        ).execute()
        out.append(msg)
    return out


# ---------------------------------------------------------------------------
# update_last_interaction — load at call time so the dashed filename
# resolves correctly regardless of how this script was invoked.
# ---------------------------------------------------------------------------


_daily_refresh_mod = None


def _load_daily_refresh():
    global _daily_refresh_mod
    if _daily_refresh_mod is not None:
        return _daily_refresh_mod
    path = Path(__file__).parent / "daily-refresh.py"
    spec = importlib.util.spec_from_file_location(
        "_huckle_daily_refresh_for_sent_mine", path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _daily_refresh_mod = mod
    return mod


def _stamp_person(
    people_dir: Path, slug: str, date_iso: str,
) -> bool:
    """Wrapper over daily-refresh's update_last_interaction that
    tolerates missing files and transient OSError, returning a bool
    suitable for stat-counter incrementing."""
    fp = people_dir / f"{slug}.md"
    if not fp.exists():
        return False
    try:
        return bool(_load_daily_refresh().update_last_interaction(fp, date_iso))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    *,
    service,
    people_dir: Path,
    cursor_path: Path,
    summary_path: Path,
    window_days: int,
    max_messages: int,
    commit: bool,
    now_iso: str,
    operator_emails: set[str],
    verbose: bool = False,
) -> dict:
    """Run one sent-mine pass. `service` is injectable so tests can
    pass a fake Gmail client."""
    cursor = load_cursor(cursor_path)
    email_to_slug = build_email_to_slug_map(people_dir)
    query = build_gmail_query(
        cursor=cursor, fallback_days=window_days, now_iso=now_iso,
    )
    messages = _fetch_messages(service, query, max_messages)

    stats = {
        "status": "ok",
        "query": query,
        "messages_scanned": 0,
        "recipients_total": 0,
        "recipients_matched": 0,
        "recipients_unmatched": 0,
        "recipients_skipped": 0,   # noreply etc.
        "stamps_written": 0,
        "stamps_declined_max_merge": 0,
    }

    unmatched_samples: list[str] = []
    max_internal_date = str(cursor.get("last_internalDate") or "0")

    for msg in messages:
        stats["messages_scanned"] += 1
        internal_date = str(msg.get("internalDate") or "0")
        date_iso = internal_date_to_iso_date(internal_date)
        if date_iso is None:
            continue
        if internal_date > max_internal_date:
            max_internal_date = internal_date

        recipients = extract_recipient_emails(
            msg, operator_emails=operator_emails,
        )
        stats["recipients_total"] += len(recipients)

        for addr in recipients:
            slug = email_to_slug.get(addr)
            if not slug:
                stats["recipients_unmatched"] += 1
                if len(unmatched_samples) < 20:
                    unmatched_samples.append(addr)
                continue
            stats["recipients_matched"] += 1
            if commit:
                if _stamp_person(people_dir, slug, date_iso):
                    stats["stamps_written"] += 1
                else:
                    stats["stamps_declined_max_merge"] += 1

    # Persist cursor + summary only when committing — dry-run leaves
    # the cursor alone so a follow-up --commit re-scans the same
    # window.
    if commit:
        save_cursor(cursor_path, {
            "last_internalDate": max_internal_date,
            "last_run_at": now_iso,
        })
        try:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                json.dumps({
                    **stats,
                    "unmatched_samples": unmatched_samples,
                    "written_at": now_iso,
                }, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass  # Dropbox EROFS transient — don't fail the run.

    if verbose:
        stats["unmatched_samples"] = unmatched_samples

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    ap.add_argument("--creds", type=Path, default=DEFAULT_CREDS)
    ap.add_argument("--cursor-path", type=Path, default=DEFAULT_CURSOR)
    ap.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY)
    ap.add_argument("--people-dir", type=Path, default=None)
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                    help="Bootstrap window when cursor is empty/stale")
    ap.add_argument("--max-messages", type=int, default=200)
    ap.add_argument("--commit", action="store_true",
                    help="No-op — commit is now the default. Kept for "
                    "invocation compatibility.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Scan + report stats, but don't stamp "
                    "last_interaction, advance the cursor, or write "
                    "the summary file.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    # Default is commit; the cron wrapper (script-contract-host.sh)
    # invokes with no extra args, and a dry-run cron is worthless.
    # Use --dry-run for manual smoke tests.
    commit = not args.dry_run
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        brain = dropbox_brain_root()
        people_dir = args.people_dir or (brain / "people")
        operator_emails = set(load_operator().emails)
        service = _build_service(args.token, args.creds)
        result = run(
            service=service,
            people_dir=people_dir,
            cursor_path=args.cursor_path,
            summary_path=args.summary_path,
            window_days=args.window_days,
            max_messages=args.max_messages,
            commit=commit,
            now_iso=now_iso,
            operator_emails=operator_emails,
            verbose=args.verbose,
        )
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print()
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 0

    print()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
