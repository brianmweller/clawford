#!/usr/bin/env python3
"""
timed-deliver.py — Hold and deliver a message at the top of the hour.

Reads a message from a file, waits until :00 UTC, then sends via Telegram
Bot API with disable_web_page_preview and disable_notification.

Usage: python3 timed-deliver.py <message_file> [--silent] [--token-env BOT_TOKEN_ENV_VAR]

The agent writes its formatted output to the message file, then this
script handles the timed delivery.

The --token-env flag specifies which env var holds the bot token.
If omitted, falls back to TELEGRAM_BOT_TOKEN. Never falls back to
another agent's token — that sends messages to the wrong bot.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
import urllib.request


def get_config():
    """Parse args and resolve bot token. Fail loudly if token is missing."""
    token_env = "TELEGRAM_BOT_TOKEN"
    for i, arg in enumerate(sys.argv):
        if arg == "--token-env" and i + 1 < len(sys.argv):
            token_env = sys.argv[i + 1]

    bot_token = os.environ.get(token_env, "")
    if not bot_token:
        print(f"ERROR: {token_env} not set in environment", file=sys.stderr)
        sys.exit(1)

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        print("ERROR: TELEGRAM_CHAT_ID not set in environment", file=sys.stderr)
        sys.exit(1)

    return bot_token, chat_id


BOT_TOKEN, CHAT_ID = get_config()


def send_telegram(text, silent=False):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[dry-run] {text[:100]}...", file=sys.stderr)
        return True

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"Send failed: {e}", file=sys.stderr)
        return False


def wait_for_top_of_hour():
    """Wait until :00 of the next hour if we're past :50."""
    now = datetime.now(timezone.utc)
    if now.minute >= 50:
        wait_seconds = (60 - now.minute) * 60 - now.second
        if 0 < wait_seconds <= 600:
            print(f"Holding delivery for {wait_seconds}s until :00", file=sys.stderr)
            time.sleep(wait_seconds)


def main():
    if len(sys.argv) < 2:
        print("Usage: timed-deliver.py <message_file> [--silent]")
        sys.exit(1)

    msg_file = sys.argv[1]
    silent = "--silent" in sys.argv

    if not os.path.exists(msg_file):
        print(f"Message file not found: {msg_file}", file=sys.stderr)
        sys.exit(1)

    with open(msg_file) as f:
        message = f.read().strip()

    if not message:
        print("Empty message file", file=sys.stderr)
        sys.exit(1)

    wait_for_top_of_hour()

    # Split on double newlines to send as separate messages if needed
    # (Telegram has 4096 char limit)
    if len(message) <= 4000:
        send_telegram(message, silent=silent)
    else:
        # Split into chunks at paragraph boundaries
        chunks = []
        current = ""
        for line in message.split("\n"):
            if len(current) + len(line) + 1 > 3900:
                chunks.append(current)
                current = line
            else:
                current += ("\n" + line) if current else line
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            send_telegram(chunk, silent=(not is_last) if not silent else True)
            time.sleep(0.3)

    print(json.dumps({"status": "ok", "chars": len(message)}))


if __name__ == "__main__":
    main()
