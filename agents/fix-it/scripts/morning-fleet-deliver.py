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


def send_telegram(bot_token: str, chat_id: str, text: str, silent: bool = False) -> bool:
    """Send a message via the Telegram Bot API. Returns True on success."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:4000],  # Telegram hard limit is 4096; leave headroom
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


def main() -> int:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        print("ERROR: TELEGRAM_CHAT_ID not set in environment", file=sys.stderr)
        return 2

    delivered: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    for agent_id, token_env, display in FLEET:
        text, status = read_brief(agent_id)
        if status != "ok":
            skipped.append((agent_id, status))
            continue
        bot_token = os.environ.get(token_env, "")
        if not bot_token:
            failed.append((agent_id, f"{token_env} not in env"))
            continue
        ok = send_telegram(bot_token, chat_id, text)
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
        else:
            failed.append((agent_id, "telegram API failure"))

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
