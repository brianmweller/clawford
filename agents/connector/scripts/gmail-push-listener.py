#!/usr/bin/env python3
"""gmail-push-listener.py — long-running daemon that consumes Gmail
push notifications via a Pub/Sub pull subscription and triggers the
inbox-triage + auto-compose pipeline in real time.

Run under systemd (ops/systemd/clawford-huckle-push.service). The
existing 30-min `connector-inbox-triage` cron stays enabled as a
belt-and-suspenders fallback; this listener gives sub-minute latency
when healthy.

Architectural notes
-------------------

PULL, not PUSH. The VPS has no public HTTPS endpoint (SSH is Tailscale-
only), and the fleet's architectural stance is "long-polling over
webhooks" (guide chapter 18). Pub/Sub pull keeps that posture — the
listener initiates the connection outbound, no new attack surface, no
Caddy, no TLS, no JWT validation.

Two cursors, distinct lifetimes:

  * gmail-watch-state.json — baseline historyId returned by the last
    successful users.watch() call. Written by gmail-watch-renew.py on
    a daily cron. Read by this listener on first boot when there's no
    live cursor yet.

  * gmail-history-cursor.json — the listener's rolling cursor. Updated
    after every successful history.list() tick. On SIGTERM the listener
    persists the cursor before exit so redelivery stays idempotent.

Idempotency. Pub/Sub is at-least-once; auto-compose-log.json already
dedupes on thread_id. Re-processing the same thread is cheap (auto-
compose no-ops if the thread is logged and --force is not passed).

Failure modes handled here:

  * `historyId invalid` (Gmail invalidates cursors >~7 days old) →
    re-seed from users.getProfile() and skip one tick's delta (can't
    reconstruct; the polling cron catches anything missed).
  * Unparseable Pub/Sub message → ack anyway to break the redelivery
    loop; log.
  * Subprocess failure → log, ack, continue. The errored thread will
    be picked up by the next polling cron or manual retry.

Environment variables (from /home/openclaw/clawford/.env):
  GMAIL_PUSH_TOPIC           e.g. projects/my-proj/topics/gmail-huckle-inbox
  GMAIL_PUSH_SUBSCRIPTION    e.g. projects/my-proj/subscriptions/huckle-gmail-pull
  CONNECTOR_BOT_TOKEN        Telegram token for error alerts
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable


_SCRIPTS_DIR = Path(__file__).resolve().parent


# --- shared library sys.path shim (matches other connector scripts) ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break


DEFAULT_TOKEN = Path(os.path.expanduser("~/.clawford/connector-workspace/token.json"))
DEFAULT_CREDS = Path(os.path.expanduser("~/.clawford/connector-workspace/credentials.json"))
DEFAULT_CURSOR = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/gmail-history-cursor.json"))
DEFAULT_WATCH_STATE = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/gmail-watch-state.json"))

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/pubsub",
]

# Per-tick pull cap. Pub/Sub returns up to this many messages per call;
# the listener processes them sequentially and acks each as it goes.
DEFAULT_PULL_BATCH = 10

# Per-subprocess timeout. inbox-triage is fast; auto-compose hits the
# LLM and can legitimately run 2-3 minutes.
TRIAGE_TIMEOUT_S = 120
COMPOSE_TIMEOUT_S = 600

# SIGTERM handshake. systemctl stop sends SIGTERM; we flip this and
# return from the main loop cleanly after the current tick.
_SHOULD_STOP = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _SHOULD_STOP
        _SHOULD_STOP = True
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# ---- cursor file helpers --------------------------------------------


def load_cursor(path: Path) -> str | None:
    """Read the rolling history cursor. Returns None if absent/corrupt."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data["history_id"])
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def save_cursor(path: Path, history_id: str) -> None:
    """Atomic write of the rolling history cursor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"history_id": str(history_id)}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def seed_cursor_from_watch_state(watch_state_path: Path) -> str | None:
    """On first boot (or after a cursor wipe) the listener seeds itself
    from the last successful users.watch() response."""
    if not watch_state_path.exists():
        return None
    try:
        data = json.loads(watch_state_path.read_text(encoding="utf-8"))
        return str(data["history_id"])
    except (json.JSONDecodeError, KeyError, OSError):
        return None


# ---- Gmail history delta --------------------------------------------


def gmail_history_thread_ids(
    service: Any,
    *,
    start_history_id: str,
    label_id: str = "INBOX",
) -> tuple[list[str], str]:
    """Call users.history.list() and return (thread_ids, new_cursor).

    thread_ids: deduplicated, in the order they first appear, filtered
        to only those whose message currently carries `label_id`.
    new_cursor: the Gmail-reported latest historyId. Safe to advance
        the cursor to this value even if no threads matched — that's
        the point of the historyId.
    """
    response = service.users().history().list(
        userId="me",
        startHistoryId=start_history_id,
        labelId=label_id,
        historyTypes=["messageAdded"],
    ).execute()

    seen: set[str] = set()
    thread_ids: list[str] = []
    for history in response.get("history") or []:
        for added in history.get("messagesAdded") or []:
            msg = added.get("message") or {}
            tid = msg.get("threadId")
            if not tid:
                continue
            if label_id not in (msg.get("labelIds") or []):
                continue
            if tid in seen:
                continue
            seen.add(tid)
            thread_ids.append(tid)

    new_cursor = str(response.get("historyId") or start_history_id)
    return thread_ids, new_cursor


def fetch_current_history_id(service: Any) -> str:
    """Fall-back cursor re-seed via users.getProfile(). Use after an
    'historyId invalid' error from history.list()."""
    profile = service.users().getProfile(userId="me").execute()
    return str(profile["historyId"])


# ---- per-thread orchestration ---------------------------------------


def process_thread_id(
    *,
    thread_id: str,
    scripts_dir: Path,
    runner: Callable[..., Any] = subprocess.run,
) -> dict:
    """Run inbox-triage --thread-id <tid>, then (iff triage exits 0)
    auto-compose --force <tid>. Returns a summary dict.

    The triage invocation appends the thread to the queue; the compose
    invocation picks it up (even with --force, auto-compose respects
    its --max cap, which is fine — a single thread always fits).
    """
    triage_script = str(scripts_dir / "inbox-triage.py")
    compose_script = str(scripts_dir / "auto-compose.py")

    triage_cmd = [
        sys.executable, triage_script,
        "--thread-id", thread_id,
    ]
    triage = runner(
        triage_cmd,
        capture_output=True, text=True,
        timeout=TRIAGE_TIMEOUT_S,
    )
    if triage.returncode != 0:
        return {
            "thread_id": thread_id,
            "triage_rc": triage.returncode,
            "compose_rc": None,
            "stderr": (triage.stderr or "")[-500:],
        }

    compose_cmd = [
        sys.executable, compose_script,
        "--force", thread_id,
    ]
    compose = runner(
        compose_cmd,
        capture_output=True, text=True,
        timeout=COMPOSE_TIMEOUT_S,
    )
    return {
        "thread_id": thread_id,
        "triage_rc": triage.returncode,
        "compose_rc": compose.returncode,
        "stderr": (compose.stderr or "")[-500:] if compose.returncode != 0 else "",
    }


# ---- tick: pull → history → subprocess → ack → advance cursor -------


def run_tick(
    *,
    gmail_service: Any,
    pubsub_service: Any,
    subscription_path: str,
    cursor_path: Path,
    scripts_dir: Path,
    runner: Callable[..., Any] = subprocess.run,
    pull_batch: int = DEFAULT_PULL_BATCH,
) -> dict:
    """One full tick: pull messages, walk Gmail history for each,
    shell triage+compose per new thread, ack, advance cursor.

    Returns a summary dict. Never raises for per-message failures —
    those land in the summary.unparseable/errored counts.
    """
    try:
        from pubsub_pull import (  # type: ignore
            acknowledge_messages, pull_messages,
        )
    except ImportError:
        from agents.shared.pubsub_pull import (  # type: ignore
            acknowledge_messages, pull_messages,
        )

    pulled = pull_messages(
        pubsub_service,
        subscription_path=subscription_path,
        max_messages=pull_batch,
    )

    if not pulled:
        return {
            "pulled": 0, "threads_processed": 0, "unparseable": 0,
            "errored": 0, "cursor": load_cursor(cursor_path),
        }

    cursor = load_cursor(cursor_path)

    unparseable = 0
    threads_seen: list[str] = []
    per_thread_results: list[dict] = []
    errored = 0
    ack_ids: list[str] = []

    for msg in pulled:
        ack_ids.append(msg.ack_id)
        if msg.gmail_push is None:
            unparseable += 1
            continue

        try:
            if cursor is None:
                # First tick ever with no cursor on disk. Use the
                # notification's historyId as the baseline; the actual
                # delta will come on the next tick.
                cursor = str(msg.gmail_push.history_id)
                continue

            thread_ids, new_cursor = gmail_history_thread_ids(
                gmail_service, start_history_id=cursor,
            )
            cursor = new_cursor
        except Exception as e:  # noqa: BLE001
            # Most common: 404 "startHistoryId not found" if cursor is
            # too old. Re-seed from the current profile; skip this
            # tick's delta (the polling cron catches anything missed).
            if "historyId" in str(e) or "404" in str(e):
                try:
                    cursor = fetch_current_history_id(gmail_service)
                except Exception:
                    pass
            errored += 1
            continue

        for tid in thread_ids:
            if tid in threads_seen:
                continue
            threads_seen.append(tid)
            try:
                res = process_thread_id(
                    thread_id=tid,
                    scripts_dir=scripts_dir,
                    runner=runner,
                )
                per_thread_results.append(res)
                if res.get("triage_rc") != 0 or (
                    res.get("compose_rc") is not None
                    and res.get("compose_rc") != 0
                ):
                    errored += 1
            except Exception:  # noqa: BLE001
                errored += 1

    # Ack everything — successful, unparseable, and errored. We've
    # already logged the errors; redelivery would just repeat them.
    acknowledge_messages(
        pubsub_service,
        subscription_path=subscription_path,
        ack_ids=ack_ids,
    )

    # Persist the advanced cursor even if no threads matched.
    if cursor is not None:
        save_cursor(cursor_path, cursor)

    return {
        "pulled": len(pulled),
        "threads_processed": len(threads_seen),
        "unparseable": unparseable,
        "errored": errored,
        "cursor": cursor,
        "per_thread": per_thread_results,
    }


# ---- main (long-running systemd entrypoint) -------------------------


def _build_services(token_path: Path, creds_path: Path):
    from googleapiclient.discovery import build
    from agents.shared.google_oauth import get_credentials
    creds = get_credentials(str(creds_path), str(token_path), GMAIL_SCOPES)
    gmail = build("gmail", "v1", credentials=creds)
    pubsub = build("pubsub", "v1", credentials=creds)
    return gmail, pubsub


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    ap.add_argument("--creds", type=Path, default=DEFAULT_CREDS)
    ap.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR)
    ap.add_argument("--watch-state", type=Path, default=DEFAULT_WATCH_STATE)
    ap.add_argument("--scripts-dir", type=Path, default=_SCRIPTS_DIR)
    ap.add_argument("--subscription",
                    default=os.environ.get("GMAIL_PUSH_SUBSCRIPTION"))
    ap.add_argument("--one-tick", action="store_true",
                    help="Run exactly one tick and exit (for manual test)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    if not args.subscription:
        print("ERROR: GMAIL_PUSH_SUBSCRIPTION env var (or --subscription) required",
              file=sys.stderr)
        return 1
    if not args.token.exists():
        print(f"ERROR: token.json not found at {args.token}", file=sys.stderr)
        return 1

    # Seed cursor from watch state if empty (first boot).
    if load_cursor(args.cursor) is None:
        seed = seed_cursor_from_watch_state(args.watch_state)
        if seed:
            save_cursor(args.cursor, seed)
            print(f"[listener] seeded cursor from watch state: {seed}", flush=True)
        else:
            print("[listener] no cursor and no watch state — "
                  "will initialize on first message", flush=True)

    gmail, pubsub = _build_services(args.token, args.creds)

    _install_signal_handlers()

    print(f"[listener] starting. subscription={args.subscription} "
          f"cursor={args.cursor}", flush=True)

    while not _SHOULD_STOP:
        try:
            summary = run_tick(
                gmail_service=gmail,
                pubsub_service=pubsub,
                subscription_path=args.subscription,
                cursor_path=args.cursor,
                scripts_dir=args.scripts_dir,
            )
            if summary["pulled"] > 0 or summary["threads_processed"] > 0:
                print(f"[listener] tick: {json.dumps(summary)}", flush=True)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc().splitlines()[-3:]
            print(f"[listener] tick error: {type(e).__name__}: {e}\n"
                  f"  {' | '.join(tb)}", flush=True, file=sys.stderr)
            # Back off to avoid a tight failure loop if Gmail or
            # Pub/Sub is down. 30s is short enough that recovery is
            # fast, long enough that we don't burn 1000 retries/min.
            time.sleep(30)

        if args.one_tick:
            break

    print("[listener] SIGTERM — exiting cleanly", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
