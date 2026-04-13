#!/usr/bin/env python3
"""timed-deliver.py — Hold and deliver a message at the top of the hour.

Reads a message from a file, waits until :00 UTC, then sends via Telegram
Bot API with disable_web_page_preview and disable_notification.

Usage: python3 timed-deliver.py <message_file> [--silent] [--token-env BOT_TOKEN_ENV_VAR]

The agent writes its formatted output to the message file, then this
script handles the timed delivery.

The --token-env flag specifies which env var holds the bot token.
If omitted, falls back to TELEGRAM_BOT_TOKEN. Never falls back to
another agent's token — that sends messages to the wrong bot.

Conforms to agents/shared/SCRIPT_CONTRACT.md: always exits 0, prints
one JSON line to stdout with a `status` field. All error paths raise
and are caught in main().
"""

import json
import os
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone


def _parse_argv(argv: list[str]) -> tuple[str, bool, str]:
    """Return (msg_file, silent, token_env_name). Raises on bad args."""
    token_env = "TELEGRAM_BOT_TOKEN"
    positional: list[str] = []
    skip_next = False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg == "--token-env":
            if i + 1 >= len(argv):
                raise ValueError("--token-env requires an argument")
            token_env = argv[i + 1]
            skip_next = True
            continue
        positional.append(arg)

    if not positional or positional[0].startswith("--"):
        raise ValueError(
            "usage: timed-deliver.py <message_file> [--silent] [--token-env ENV_VAR]"
        )
    msg_file = positional[0]
    silent = "--silent" in positional
    return msg_file, silent, token_env


def _resolve_creds(token_env: str) -> tuple[str, str]:
    """Return (bot_token, chat_id). Raises if either is missing."""
    bot_token = os.environ.get(token_env, "")
    if not bot_token:
        raise RuntimeError(f"{token_env} not set in environment")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID not set in environment")
    return bot_token, chat_id


def _send_telegram(bot_token: str, chat_id: str, text: str, silent: bool) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"Send failed: {e}", file=sys.stderr)
        return False


def _wait_for_top_of_hour() -> None:
    """Hold until :00 of the next hour if we're in the gather window (:40-:59).

    If we arrive past :00 (processing overshot the window), log a warning
    and deliver immediately — the message is already late.
    """
    now = datetime.now(timezone.utc)
    if now.minute >= 40:
        wait_seconds = (60 - now.minute) * 60 - now.second
        if 0 < wait_seconds <= 1200:
            print(f"Holding delivery for {wait_seconds}s until :00", file=sys.stderr)
            time.sleep(wait_seconds)
    elif now.minute <= 10:
        print(
            f"WARNING: arrived at :{now.minute:02d} — overshot :00 target, delivering now",
            file=sys.stderr,
        )


def run() -> dict:
    msg_file, silent, token_env = _parse_argv(sys.argv[1:])
    bot_token, chat_id = _resolve_creds(token_env)

    if not os.path.exists(msg_file):
        raise FileNotFoundError(f"message file not found: {msg_file}")

    with open(msg_file, encoding="utf-8") as f:
        message = f.read().strip()

    if not message:
        raise ValueError(f"empty message file: {msg_file}")

    _wait_for_top_of_hour()

    sent_chunks = 0
    if len(message) <= 4000:
        if _send_telegram(bot_token, chat_id, message, silent=silent):
            sent_chunks = 1
    else:
        chunks: list[str] = []
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
            is_last = i == len(chunks) - 1
            if _send_telegram(
                bot_token, chat_id, chunk, silent=(not is_last) if not silent else True
            ):
                sent_chunks += 1
            time.sleep(0.3)

    return {
        "status": "ok",
        "chars": len(message),
        "chunks_sent": sent_chunks,
        "silent": silent,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
