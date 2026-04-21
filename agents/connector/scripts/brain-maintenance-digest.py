#!/usr/bin/env python3
"""brain-maintenance-digest.py — render pending-review queue items as
tap-to-resolve Telegram messages, appended to the morning brief.

The queue is written by the Gmail miner (low-confidence extractions),
the structured-type migration (ambiguous classifications), and the
cross-run dedupe pass (suspected duplicates). the operator resolves items in
one tap; resolution handlers live in agents/shared/dispatcher.py behind
the `facts:*` callback prefix.

Design per the Phase 4 plan:
  - 0 items → suppress (stdout empty so morning-brief composer skips
    the section entirely).
  - 1-5 items → one message per item with a three-button keyboard.
    When ≥ 3 items, a bulk footer with "Approve remaining" /
    "Silence today" appears.
  - 6+ items → single summary message with an "Expand" button.

Rendering is pure (testable without network). main() reads the queue
from the canonical path, calls the renderer, and ships each message
via telegram_api.send_message.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))


DEFAULT_QUEUE = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/cache/pending-review-queue.jsonl"
))
CONNECTOR_TOKEN_ENV = "CONNECTOR_BOT_TOKEN"

# Threshold: batches at or above this many items collapse to a summary
# message with an "Expand" button. Below the threshold, one message per
# item with inline-keyboard resolves.
SUMMARY_THRESHOLD = 6

# Minimum batch size at which the bulk "Approve remaining" / "Silence
# today" footer appears alongside per-item messages.
BULK_FOOTER_MIN = 3


# ─── Rendering (pure) ────────────────────────────────────────────────


def _item_text(entry: dict) -> str:
    fact = entry.get("fact") or {}
    subject = fact.get("subject", "?")
    content = (fact.get("content") or "").strip()
    source = entry.get("source", "miner")
    conf_raw = fact.get("confidence")
    try:
        conf = float(conf_raw) if conf_raw is not None else None
    except (TypeError, ValueError):
        conf = None

    if source == "miner":
        header = "🧠 New fact"
        if conf is not None:
            header += f" — conf {conf:.2f}"
    elif source == "migration":
        header = "🔁 Migration classification"
    elif source == "dedupe":
        header = "🧩 Possible duplicate — merge or keep separate?"
    else:
        header = f"🧠 Pending review ({source})"

    body = f"Subject: {subject}\n{content}" if content else f"Subject: {subject}"
    return f"{header}\n{body}"


def _item_keyboard(entry_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Yes", "callback_data": f"facts:approve:{entry_id}"},
            {"text": "❌ No", "callback_data": f"facts:reject:{entry_id}"},
            {"text": "⏭ Skip", "callback_data": f"facts:skip:{entry_id}"},
        ]],
    }


def _bulk_footer(n_items: int, batch_id: str) -> dict:
    return {
        "text": (
            f"☑️ {n_items} items above. Approve remaining to accept all, "
            "or silence today to defer."
        ),
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "☑️ Approve remaining",
                 "callback_data": f"facts:approve_remaining:{batch_id}"},
                {"text": "🔕 Silence today",
                 "callback_data": f"facts:silence_today:{batch_id}"},
            ]],
        },
    }


def _summary_message(entries: list[dict], batch_id: str) -> dict:
    return {
        "text": (
            f"🧠 {len(entries)} items pending review — tap to expand."
        ),
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "📋 Expand",
                 "callback_data": f"facts:expand:{batch_id}"},
            ]],
        },
    }


def render_digest_messages(entries: list[dict], *, batch_id: str) -> list[dict]:
    """Pure renderer. Returns a list of {text, reply_markup} dicts to
    send. Empty list when no entries.

    Batch_id is embedded in bulk-action and expand callbacks so the
    resolver can scope the action to the specific digest run (a later
    queue refill shouldn't be silenced by an old button press)."""
    if not entries:
        return []

    if len(entries) >= SUMMARY_THRESHOLD:
        return [_summary_message(entries, batch_id)]

    msgs: list[dict] = []
    for e in entries:
        entry_id = str(e.get("id") or "")
        if not entry_id:
            continue
        msgs.append({
            "text": _item_text(e),
            "reply_markup": _item_keyboard(entry_id),
        })
    if len(msgs) >= BULK_FOOTER_MIN:
        msgs.append(_bulk_footer(len(msgs), batch_id))
    return msgs


# ─── Orchestration (I/O) ─────────────────────────────────────────────


def _send_messages(messages: list[dict]) -> int:
    """Send each message via telegram_api. Returns the count of
    successful sends. Never raises."""
    if not messages:
        return 0
    try:
        from agents.shared.telegram_api import resolve_credentials, send_message
    except ImportError:
        print("telegram_api import failed; skipping send", file=sys.stderr)
        return 0
    try:
        token, chat_id = resolve_credentials(token_env=CONNECTOR_TOKEN_ENV)
    except RuntimeError as exc:
        print(f"telegram credentials unavailable: {exc}", file=sys.stderr)
        return 0

    sent = 0
    for m in messages:
        ok = send_message(
            token, chat_id, m["text"],
            reply_markup=m.get("reply_markup"),
            agent_id="connector",
            role_summary="brain-maintenance",
        )
        if ok:
            sent += 1
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-path", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--dry-run", action="store_true",
                    help="Render but don't send; print messages to stdout")
    ap.add_argument("--batch-id", default=None,
                    help="Override the batch id (defaults to a new uuid)")
    args = ap.parse_args(argv)

    try:
        from agents.shared import pending_queue
    except ImportError:
        import pending_queue  # type: ignore

    entries = pending_queue.load_active(args.queue_path)
    batch_id = args.batch_id or uuid.uuid4().hex[:12]
    messages = render_digest_messages(entries, batch_id=batch_id)

    if not messages:
        # Morning-brief composer reads stdout lines as "additional brief
        # sections" — empty stdout signals "no section to append."
        print(json.dumps({
            "status": "ok", "pending_items": 0, "messages_rendered": 0,
            "messages_sent": 0,
        }))
        return 0

    if args.dry_run:
        for m in messages:
            print("=" * 60)
            print(m["text"])
            print(json.dumps(m.get("reply_markup") or {}, indent=2))
        print(json.dumps({
            "status": "ok",
            "pending_items": len(entries),
            "messages_rendered": len(messages),
            "messages_sent": 0,
            "dry_run": True,
        }))
        return 0

    sent = _send_messages(messages)
    print(json.dumps({
        "status": "ok",
        "pending_items": len(entries),
        "messages_rendered": len(messages),
        "messages_sent": sent,
        "batch_id": batch_id,
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"status": "error", "error": str(exc)}))
        sys.exit(0)
