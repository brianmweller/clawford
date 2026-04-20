#!/usr/bin/env python3
"""gmail-watch-renew.py — daily cron that re-calls Gmail users.watch()
so the Pub/Sub subscription to the INBOX label stays alive.

Gmail invalidates watches after 7 days regardless of the expiration
returned in the watch response. A daily renewal cron keeps the listener
ticking; if renewal fails, the contract wrapper sends a Telegram alert
and the 30-min polling cron (inbox-triage) continues as belt-and-
suspenders until the next renewal succeeds.

Topic comes from the GMAIL_PUSH_TOPIC env var, populated from the host
.env file by script-contract-host.sh. Expected format:
    projects/<gcp-project>/topics/gmail-huckle-inbox

Exit code is always 0 — the contract envelope on the final line of
stdout carries the actual status.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


# --- shared library sys.path shim (matches other connector scripts) ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break


DEFAULT_TOKEN = Path(os.path.expanduser("~/.clawford/connector-workspace/token.json"))
DEFAULT_CREDS = Path(os.path.expanduser("~/.clawford/connector-workspace/credentials.json"))
DEFAULT_STATE = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/gmail-watch-state.json"))

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/pubsub",
]


def run_renewal(*, service: Any, topic: str, state_path: Path) -> dict:
    """Call users.watch(), persist WatchState, return summary dict.

    Pure-ish: takes a pre-built service so tests can inject a fake.
    Returns {status, history_id, expiration_ms, topic}. Raises KeyError
    or ValueError if Gmail's response is missing required fields — the
    calling envelope maps the exception to status=error.
    """
    # Local import so tests don't need the shared module available
    # until the function is actually called.
    try:
        from gmail_watch import (  # type: ignore
            WatchState,
            create_watch,
            save_watch_state,
        )
    except ImportError:
        from agents.shared.gmail_watch import (  # type: ignore
            WatchState,
            create_watch,
            save_watch_state,
        )

    response = create_watch(service, topic_name=topic)

    history_id = str(response["historyId"])
    expiration_ms = int(response["expiration"])

    state = WatchState(
        history_id=history_id,
        expiration_ms=expiration_ms,
        topic=topic,
    )
    save_watch_state(state_path, state)

    return {
        "status": "ok",
        "history_id": history_id,
        "expiration_ms": expiration_ms,
        "topic": topic,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    ap.add_argument("--creds", type=Path, default=DEFAULT_CREDS)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--topic", default=os.environ.get("GMAIL_PUSH_TOPIC"),
                    help="Fully-qualified Pub/Sub topic (projects/.../topics/...). "
                         "Defaults to $GMAIL_PUSH_TOPIC.")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    if not args.topic:
        result = {
            "status": "error",
            "error": "GMAIL_PUSH_TOPIC env var (or --topic) is required",
            "alert": "🐱 huckle-watch-renew: GMAIL_PUSH_TOPIC not set",
        }
        print(json.dumps(result))
        return 0

    if not args.token.exists():
        result = {
            "status": "error",
            "error": f"token.json not found at {args.token}",
            "alert": f"🐱 huckle-watch-renew: token missing at {args.token}",
        }
        print(json.dumps(result))
        return 0

    from googleapiclient.discovery import build
    from agents.shared.google_oauth import get_credentials

    try:
        creds = get_credentials(str(args.creds), str(args.token), GMAIL_SCOPES)
        service = build("gmail", "v1", credentials=creds)
        result = run_renewal(
            service=service,
            topic=args.topic,
            state_path=args.state,
        )
    except Exception as e:  # noqa: BLE001
        result = {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "alert": f"🐱 huckle-watch-renew failed: {type(e).__name__}: {str(e)[:200]}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
