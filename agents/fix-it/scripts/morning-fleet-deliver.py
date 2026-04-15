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
    ("connector", "CONNECTOR_BOT_TOKEN", "Huckle Cat"),
]

CACHE_FILENAME = "cache/morning-brief-ready.txt"
FRESHNESS_SECONDS = 2 * 60 * 60  # 2 hours

# news-digest uses a structured items file to enable per-item inline
# keyboard buttons instead of plain text chunks. Format:
#   [{"num": 1, "category": "🤖 AI & Tech", "title": "...",
#     "extended_headline": "...", "url": "...",
#     "source_label": "Google AI"}, ...]
# When this file exists AND its mtime is within FRESHNESS_SECONDS,
# news-digest's brief is delivered item-by-item with 👍/👎/📖 buttons.
# Otherwise we fall back to sending morning-brief-ready.txt as plain
# chunks.
ITEMS_FILENAME = "cache/morning-items.json"
NEWS_DIGEST_AGENT_ID = "news-digest"

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

# Workspace path inside the container. Set the CLAWFORD_WORKSPACE_BASE
# env var to override for tests.
WORKSPACE_BASE = os.environ.get(
    "CLAWFORD_WORKSPACE_BASE",
    os.path.expanduser("~/.clawford"),
)


def workspace_brief_path(agent_id: str) -> Path:
    return Path(WORKSPACE_BASE) / f"{agent_id}-workspace" / CACHE_FILENAME


def workspace_items_path(agent_id: str) -> Path:
    return Path(WORKSPACE_BASE) / f"{agent_id}-workspace" / ITEMS_FILENAME


def read_items(agent_id: str) -> tuple[list[dict] | None, str]:
    """Return (items_list, status_code) for an agent's structured items file.

    Only news-digest uses this. Format:
      [{"num": 1, "category": "🤖 AI & Tech",
        "title": "...", "extended_headline": "...",
        "url": "...", "source_label": "Google AI"}, ...]

    status_code is one of:
      ok      — file exists, fresh, parseable, non-empty list
      missing — file does not exist
      stale   — file exists but older than FRESHNESS_SECONDS
      empty   — file exists, fresh, but list is empty
      error   — unreadable or malformed
    """
    path = workspace_items_path(agent_id)
    if not path.exists():
        return None, "missing"
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None, "error"
    if age > FRESHNESS_SECONDS:
        return None, "stale"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "error"
    if not isinstance(data, list) or not data:
        return None, "empty"
    return data, "ok"


def _format_item_message(item: dict) -> str:
    """Render a single digest item as the text of one Telegram message.

    Format:
        {category_emoji} {Category}

        {num}. {extended_headline}
        {url}

    Everything except num and extended_headline is optional — missing
    fields are simply dropped. category is shown only when present
    (the caller can skip repeating the same heading across consecutive
    items; we include it unconditionally because each message stands
    alone in the Telegram chat).
    """
    lines: list[str] = []
    category = (item.get("category") or "").strip()
    if category:
        lines.append(category)
        lines.append("")
    num = item.get("num")
    headline = (item.get("extended_headline") or item.get("title") or "").strip()
    source_label = (item.get("source_label") or "").strip()
    head = f"{num}. {headline}" if num is not None else headline
    if source_label:
        head = f"{head} — {source_label}"
    lines.append(head)
    url = (item.get("url") or item.get("link") or "").strip()
    if url:
        lines.append(url)
    return "\n".join(lines)


def _buttons_for_item(num: int) -> dict:
    """Build the inline keyboard for a single digest item's reactions.

    callback_data uses the `like:N` / `dislike:N` / `more:N` form that
    engagement-poller.py's extract_engagement() parses out of the
    openclaw session transcript (openclaw forwards callback_data into
    the agent's session as text).
    """
    return {
        "inline_keyboard": [
            [
                {"text": "👍 like",    "callback_data": f"like:{num}"},
                {"text": "👎 dislike", "callback_data": f"dislike:{num}"},
                {"text": "📖 more",    "callback_data": f"more:{num}"},
            ]
        ]
    }


def deliver_items_with_buttons(
    bot_token: str,
    chat_id: str,
    items: list[dict],
    footer_text: str | None = None,
) -> tuple[int, int]:
    """Send each digest item as its own Telegram message with an inline
    keyboard for per-item engagement (👍 like / 👎 dislike / 📖 more).

    Returns (sent_count, failed_count). Every message except the last
    is sent silently so Sam gets exactly one notification ding — the
    footer message (or the final item, if no footer) is the single
    audible delivery.
    """
    sent = 0
    failed = 0
    n = len(items)
    for i, item in enumerate(items):
        is_last = i == n - 1 and not footer_text
        text = _format_item_message(item)
        num = item.get("num")
        buttons = _buttons_for_item(num) if num is not None else None
        ok = send_telegram(
            bot_token, chat_id, text,
            silent=not is_last,
            reply_markup=buttons,
        )
        if ok:
            sent += 1
        else:
            failed += 1
        # Rate-limit gap between sends. Telegram's per-chat limit is
        # 1 msg/sec for regular messages; 300ms gives us headroom and
        # avoids "Too Many Requests: retry after N".
        if not is_last:
            time.sleep(0.3)

    if footer_text:
        ok = send_telegram(bot_token, chat_id, footer_text, silent=False)
        if ok:
            sent += 1
        else:
            failed += 1

    return sent, failed


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


def send_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    silent: bool = False,
    reply_markup: dict | None = None,
) -> bool:
    """Send a single message via the Telegram Bot API. Returns True on success.

    The caller is responsible for chunking long briefs via chunk_text()
    — this function sends whatever it's given up to Telegram's 4096-char
    hard limit. Anything larger will fail at the API boundary rather
    than be silently truncated.

    Pass `reply_markup` to attach an inline keyboard (used by the
    per-item news-digest delivery path for 👍/👎/📖 engagement buttons).
    """
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    payload = json.dumps(body).encode("utf-8")
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
    # Each brief is one of two shapes:
    #   ("items", list_of_item_dicts)  — news-digest per-item + buttons
    #   ("text",  plain_text_string)   — all other agents, chunked
    briefs: list[tuple[str, str, str, str, object]] = []  # (agent_id, token_env, display, kind, payload)
    skipped: list[tuple[str, str]] = []
    for agent_id, token_env, display in FLEET:
        if agent_id == NEWS_DIGEST_AGENT_ID:
            items, items_status = read_items(agent_id)
            if items_status == "ok" and items:
                briefs.append((agent_id, token_env, display, "items", items))
                continue
            # Fall through to plain-text brief if items file is missing
            # or stale — backwards compatibility while the morning-edition
            # cron is still catching up on writing morning-items.json.
        text, status = read_brief(agent_id)
        if status != "ok":
            skipped.append((agent_id, status))
            continue
        briefs.append((agent_id, token_env, display, "text", text))

    # Hold until the target UTC hour so all 5 bots fire at the same
    # wall-clock moment regardless of cron queue delay.
    if briefs:
        wait_until_target_utc_hour(TARGET_UTC_HOUR)

    delivered: list[str] = []
    failed: list[tuple[str, str]] = []

    for agent_id, token_env, display, kind, payload in briefs:
        bot_token = os.environ.get(token_env, "")
        if not bot_token:
            failed.append((agent_id, f"{token_env} not in env"))
            continue

        all_ok = True
        if kind == "items":
            # news-digest: per-item messages with inline 👍/👎/📖 buttons.
            items: list[dict] = payload  # type: ignore[assignment]
            footer = (
                f"🐛 {len(items)} items · tap 👍 👎 📖 on each to tune tomorrow"
            )
            sent_n, fail_n = deliver_items_with_buttons(
                bot_token, chat_id, items, footer_text=footer,
            )
            if fail_n > 0:
                all_ok = False
                failed.append(
                    (agent_id, f"{fail_n}/{len(items)+1} item sends failed")
                )
        else:
            # Plain text brief — chunk at paragraph boundaries and send
            # each chunk as its own Telegram message. Non-final chunks
            # are silent so Sam hears one ding per agent, not 3 ×.
            text: str = payload  # type: ignore[assignment]
            chunks = chunk_text(text)
            if not chunks:
                failed.append((agent_id, "empty after chunking"))
                continue
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                chunk_ok = send_telegram(
                    bot_token, chat_id, chunk, silent=not is_last
                )
                if not chunk_ok:
                    all_ok = False
                    failed.append(
                        (agent_id, f"telegram API failure on chunk {i+1}/{len(chunks)}")
                    )
                    break
                if not is_last:
                    time.sleep(0.3)

        if all_ok:
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
