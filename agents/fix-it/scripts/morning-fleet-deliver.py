#!/usr/bin/env python3
"""morning-fleet-deliver.py — Send all agents' morning briefs at exactly 5 AM PT.

Fleet-wide delivery orchestrator. The gather crons (family-calendar,
meetings-coach, fix-it, shopping, news-digest) fire at 11:30 UTC and
write their formatted briefs to `<workspace>/cache/morning-brief-ready.txt`
WITHOUT calling timed-deliver.py. This script fires at 12:00 UTC sharp
and sends each cache file via the agent's dedicated bot token, bypassing
OpenClaw's cron LLM sessions for the delivery step.

Why this exists: the gateway runs LLM cron sessions sequentially (see
R8 in the 2026-04-12 Telegram audit). The old design — each agent
gathers then holds via timed-deliver.py until :00 UTC — broke because
only the first session to enter the "gather window" could actually
deliver on time. By decoupling delivery from the LLM gather, every
agent hits 5:00 AM PT regardless of how long its gather session took.

Exit codes:
  0  — all successful, or no cache files present (silent)
  1  — partial failure (some delivered, some failed)
  2  — all failures

Freshness: a cache file older than 2 hours is skipped (the gather
cron probably ran yesterday and nobody cleaned up).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Per-agent fleet config. Tuple of (agent_id, bot_token_env_var, display_name).
# The display_name is just for log output; the actual message is whatever
# the gather cron wrote to cache/morning-brief-ready.txt.
FLEET = [
    ("family-calendar", "FAMILYCAL_BOT_TOKEN", "Mistress Mouse"),
    ("meetings-coach", "MEETINGS_BOT_TOKEN", "Sergeant Murphy"),
    ("fix-it", "TELEGRAM_BOT_TOKEN", "Mr Fixit"),
    ("shopping", "SHOPPING_BOT_TOKEN", "Hilda Hippo"),
    ("news-digest", "NEWSDIGEST_BOT_TOKEN", "Lowly Worm"),
]

CACHE_FILENAME = "cache/morning-brief-ready.txt"
FRESHNESS_SECONDS = 2 * 60 * 60  # 2 hours

# Target delivery hour. The cron is scheduled a few minutes early (55 11 * * *)
# to buffer against fix-it queue serialization; the script holds until this
# UTC hour before calling send_telegram so all 5 bots fire at the same
# consistent wall-clock moment regardless of how long the LLM session
# spent in queue.
TARGET_UTC_HOUR = 12

# Max time we'll ever hold. Hard-caps pathological sleeps if the cron
# fires way earlier than expected (e.g., manual trigger during debugging).
MAX_HOLD_SECONDS = 20 * 60  # 20 minutes

# Chunking: Telegram's hard message limit is 4096 characters. We split at
# 3900 to leave headroom for any trailing ellipsis or counter the script
# adds.
MAX_CHUNK_CHARS = 3900

# Workspace path inside the container. Set the OPENCLAW_WORKSPACE_BASE
# env var to override for tests.
WORKSPACE_BASE = os.environ.get(
    "OPENCLAW_WORKSPACE_BASE",
    os.path.expanduser("~/.openclaw"),
)


def workspace_brief_path(agent_id: str) -> Path:
    return Path(WORKSPACE_BASE) / f"{agent_id}-workspace" / CACHE_FILENAME


def read_brief(agent_id: str) -> tuple[str | None, str]:
    """Return (message_text, status_code) for an agent's cache file.

    status_code is one of:
      ok       — file exists, fresh, readable, non-empty
      missing  — file does not exist
      stale    — file exists but older than FRESHNESS_SECONDS
      empty    — file exists, fresh, but empty
      error    — unreadable
    """
    path = workspace_brief_path(agent_id)
    if not path.exists():
        return None, "missing"
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None, "error"
    if age > FRESHNESS_SECONDS:
        return None, "stale"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None, "error"
    if not text:
        return None, "empty"
    return text, "ok"


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a long brief into chunks that fit in a single Telegram send,
    preferring blank-line paragraph boundaries and then single newlines.

    Returns one chunk per send_telegram call. If the input is under
    max_chars, returns a list with the original text as its only element.

    No content is dropped: the concatenation of all chunks equals the
    original minus chunk-boundary whitespace.
    """
    text = text.rstrip()
    if len(text) <= max_chars:
        return [text] if text else []

    # Split into paragraphs (separated by blank lines). Paragraphs that
    # are themselves oversized get further split at single-newline
    # boundaries, then hard-sliced as a last resort.
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    def _push_current() -> None:
        nonlocal current
        if current:
            chunks.append(current.rstrip())
            current = ""

    def _split_oversized(p: str) -> list[str]:
        """Paragraph itself exceeds max_chars: split at \\n, then hard-slice."""
        out: list[str] = []
        acc = ""
        for line in p.split("\n"):
            candidate = f"{acc}\n{line}" if acc else line
            if len(candidate) > max_chars:
                if acc:
                    out.append(acc)
                    acc = line
                else:
                    # Single line longer than max_chars — hard slice
                    while len(line) > max_chars:
                        out.append(line[:max_chars])
                        line = line[max_chars:]
                    acc = line
            else:
                acc = candidate
        if acc:
            out.append(acc)
        return out

    for p in paragraphs:
        sep = "\n\n" if current else ""
        candidate = current + sep + p
        if len(candidate) <= max_chars:
            current = candidate
            continue
        # Paragraph doesn't fit.
        if len(p) > max_chars:
            _push_current()
            for piece in _split_oversized(p):
                if not current:
                    current = piece
                    continue
                if len(current) + 2 + len(piece) <= max_chars:
                    current = current + "\n\n" + piece
                else:
                    _push_current()
                    current = piece
            continue
        # Paragraph fits on its own but not when appended to current.
        _push_current()
        current = p

    _push_current()
    return [c for c in chunks if c]


def wait_until_target_utc_hour(target_hour: int = TARGET_UTC_HOUR) -> None:
    """Hold the script until UTC time hits target_hour:00:00.

    Behavior matrix:
      - now ≤ target AND (target − now) ≤ MAX_HOLD_SECONDS → sleep the delta
      - now ≤ target AND (target − now) >  MAX_HOLD_SECONDS → skip (not our window)
      - now > target (overshot) → no-op, log a warning to stderr

    Called once from main() after caches are read but before the first
    send_telegram. The goal is to make delivery land at exactly the target
    hour even when the cron queue pushed us 3-5 minutes late.
    """
    now = datetime.now(timezone.utc)
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    delta_s = (target - now).total_seconds()

    if delta_s < 0:
        overshot_s = -delta_s
        print(
            f"WARNING: morning-fleet-deliver arrived {overshot_s:.0f}s past "
            f"{target_hour:02d}:00 UTC target — delivering late",
            file=sys.stderr,
        )
        return

    if delta_s == 0:
        return

    if delta_s > MAX_HOLD_SECONDS:
        print(
            f"skipping hold: {delta_s:.0f}s until {target_hour:02d}:00 UTC is beyond "
            f"max hold window ({MAX_HOLD_SECONDS}s). Manual trigger? Sending now.",
            file=sys.stderr,
        )
        return

    print(
        f"holding for {delta_s:.0f}s until {target_hour:02d}:00 UTC",
        file=sys.stderr,
    )
    time.sleep(delta_s)


def send_telegram(bot_token: str, chat_id: str, text: str, silent: bool = False) -> bool:
    """Send a single message via the Telegram Bot API. Returns True on success.

    The caller is responsible for chunking long briefs via chunk_text()
    — this function sends whatever it's given up to Telegram's 4096-char
    hard limit. Anything larger will fail at the API boundary rather
    than be silently truncated.
    """
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = json.loads(resp.read())
        return bool(body.get("ok"))
    except Exception as e:
        print(f"  send failed: {e}", file=sys.stderr)
        return False


def today_idempotency_marker() -> Path:
    """Path to the per-day idempotency marker. Prevents double-delivery
    if the cron re-fires (observed R1 pattern on gather crons)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Path(WORKSPACE_BASE) / "fix-it-workspace" / "cache" / f"morning-fleet-delivered-{today}.json"


def already_delivered_today() -> bool:
    return today_idempotency_marker().exists()


def mark_delivered_today(delivered: list[str], failed: list[tuple[str, str]]) -> None:
    marker = today_idempotency_marker()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "delivered": delivered,
                "failed": failed,
            }, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"  marker write failed: {e}", file=sys.stderr)


def main() -> int:
    # Idempotency: refuse to re-deliver if today's marker exists.
    # This prevents R1 (morning double-fire) from producing duplicate
    # Telegram messages even if the delivery cron is re-triggered.
    if already_delivered_today():
        print(json.dumps({
            "status": "skipped",
            "reason": "already_delivered_today",
            "marker": str(today_idempotency_marker()),
        }))
        return 0

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        print("ERROR: TELEGRAM_CHAT_ID not set in environment", file=sys.stderr)
        return 2

    # Pre-read all briefs BEFORE waiting. The caches are already written
    # by the 11:30 UTC gather crons; we don't need to burn the hold window
    # doing I/O.
    briefs: list[tuple[str, str, str, str]] = []  # (agent_id, token_env, display, text)
    skipped: list[tuple[str, str]] = []
    for agent_id, token_env, display in FLEET:
        text, status = read_brief(agent_id)
        if status != "ok":
            skipped.append((agent_id, status))
            continue
        briefs.append((agent_id, token_env, display, text))

    # Hold until the target UTC hour so all 5 bots fire at the same
    # wall-clock moment regardless of cron queue delay.
    if briefs:
        wait_until_target_utc_hour(TARGET_UTC_HOUR)

    delivered: list[str] = []
    failed: list[tuple[str, str]] = []

    for agent_id, token_env, display, text in briefs:
        bot_token = os.environ.get(token_env, "")
        if not bot_token:
            failed.append((agent_id, f"{token_env} not in env"))
            continue

        # Chunk at paragraph boundaries; send each chunk as its own
        # Telegram message. Non-final chunks are silent so Sam hears
        # one ding per agent, not 3 ×.
        chunks = chunk_text(text)
        if not chunks:
            failed.append((agent_id, "empty after chunking"))
            continue

        all_ok = True
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            chunk_ok = send_telegram(
                bot_token, chat_id, chunk, silent=not is_last
            )
            if not chunk_ok:
                all_ok = False
                failed.append((agent_id, f"telegram API failure on chunk {i+1}/{len(chunks)}"))
                break
            # Small delay between chunks to stay under Telegram's rate limits
            if not is_last:
                time.sleep(0.3)

        ok = all_ok
        if ok:
            delivered.append(agent_id)
            # Optional: truncate the cache file to mark it consumed so a
            # freshness-broken re-fire doesn't re-send. Using .consumed
            # marker rather than deletion so status can still be inspected.
            consumed_path = workspace_brief_path(agent_id).with_suffix(".consumed")
            try:
                consumed_path.write_text(
                    datetime.now(timezone.utc).isoformat(),
                    encoding="utf-8",
                )
            except Exception:
                pass

    # Write the per-day idempotency marker after attempting delivery.
    # Writing on BOTH success and failure — a re-fire shouldn't retry
    # a failed send (that could double-send if the first send actually
    # succeeded but the API response was garbled). Explicit manual
    # rerun is done by deleting the marker.
    if delivered or failed:
        mark_delivered_today(delivered, failed)

    # Emit a compact result summary to stdout for the cron LLM to see.
    # Not to Telegram — the individual messages are already sent.
    report = {
        "delivered": delivered,
        "failed": failed,
        "skipped": skipped,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(report))

    if failed:
        return 1 if delivered else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
