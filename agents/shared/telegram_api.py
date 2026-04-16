"""agents/shared/telegram_api.py — shared Telegram Bot API helpers.

Replaces 168 lines of byte-identical delivery logic duplicated across
all five per-agent timed-deliver.py scripts, plus news-digest's
deliver-digest.py custom send_telegram helper (which also needed
reply_markup support for inline keyboards).

Named telegram_api rather than telegram to avoid collision with the
python-telegram-bot package that pytest may preload from site-packages.

Exposes:

    send_message(token, chat_id, text, *, silent, reply_markup, ...)
        Direct HTTP POST to sendMessage. Returns True on Telegram's
        ok=true, False on any failure (network error, ok=false). Does
        not raise.

    send_chunked(token, chat_id, text, *, silent, ...)
        Splits text >4000 chars into line-aligned chunks and sends
        each. Only the final chunk respects the caller's silent flag;
        intermediate chunks are always silent to avoid a buzz per
        chunk. Returns count of chunks successfully sent.

    wait_for_top_of_hour(*, gather_window_start=40)
        Sleeps until :00 of the next hour if now.minute is in the
        gather window (:40-:59). Logs a warning and returns immediately
        if we've overshot :00.

    resolve_credentials(token_env) -> (token, chat_id)
        Loads bot token from the given env var and chat id from
        TELEGRAM_CHAT_ID. Raises RuntimeError if either missing.

    deliver_from_file(msg_file, *, token_env, silent, wait_for_hour)
        End-to-end orchestration: resolve creds, read file, optionally
        wait for :00, chunk and send. Returns a script-contract dict
        with status/chars/chunks_sent. Raises on unrecoverable error.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

TELEGRAM_API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_CHAT_ACTION_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendChatAction"
TELEGRAM_ANSWER_CBQ_URL_TEMPLATE = "https://api.telegram.org/bot{token}/answerCallbackQuery"
MAX_MESSAGE_CHARS = 4000
CHUNK_CHARS = 3900
INTER_CHUNK_DELAY_S = 0.3
DEFAULT_TIMEOUT_S = 10
DEFAULT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "TELEGRAM_CHAT_ID"


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    silent: bool = False,
    disable_web_preview: bool = True,
    reply_markup: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> bool:
    """POST a single message to Telegram sendMessage.

    Returns True on Telegram's ok=true, False on network error or
    ok=false. Never raises — callers can batch-send and check counts.
    """
    url = TELEGRAM_API_URL_TEMPLATE.format(token=token)
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_preview,
        "disable_notification": silent,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        if not body.get("ok", False):
            print(
                f"telegram returned not-ok: {body.get('description', '<no description>')}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)
        return False


def send_chat_action(
    token: str,
    chat_id: str,
    action: str = "typing",
    *,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> bool:
    """POST to sendChatAction — used by the inbox dispatcher to show a
    "typing..." indicator while the LLM is composing a reply.

    Returns True on Telegram's ok=true, False on any failure. Never
    raises — the indicator is cosmetic; a failure here shouldn't block
    the actual reply.
    """
    url = TELEGRAM_CHAT_ACTION_URL_TEMPLATE.format(token=token)
    payload = {"chat_id": chat_id, "action": action}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return bool(body.get("ok", False))
    except Exception:
        return False


def answer_callback_query(
    token: str,
    callback_query_id: str,
    *,
    text: str | None = None,
    show_alert: bool = False,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> bool:
    """POST to answerCallbackQuery — dismisses the loading spinner on
    an inline keyboard button tap. Optionally shows a toast (text) or
    modal alert (show_alert=True).

    Returns True on Telegram's ok=true, False on any failure. Never
    raises — the ACK is cosmetic.
    """
    url = TELEGRAM_ANSWER_CBQ_URL_TEMPLATE.format(token=token)
    payload: dict = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = True
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return bool(body.get("ok", False))
    except Exception:
        return False


def send_chunked(
    token: str,
    chat_id: str,
    text: str,
    *,
    silent: bool = False,
    disable_web_preview: bool = True,
    chunk_chars: int = CHUNK_CHARS,
) -> int:
    """Split `text` into line-aligned chunks that fit Telegram's 4000-char
    message limit and send them in order.

    Only the final chunk respects the `silent` flag; intermediate chunks
    are always silent, so a long message produces at most one notification
    sound regardless of how many chunks it takes.

    Returns the count of chunks successfully sent (0 <= n <= chunk_count).
    """
    if len(text) <= MAX_MESSAGE_CHARS:
        return (
            1
            if send_message(
                token, chat_id, text, silent=silent, disable_web_preview=disable_web_preview
            )
            else 0
        )

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > chunk_chars:
            if current:
                chunks.append(current)
            current = line
        else:
            current = (current + "\n" + line) if current else line
    if current:
        chunks.append(current)

    sent = 0
    last_idx = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        is_last = i == last_idx
        chunk_silent = silent if is_last else True
        if send_message(
            token,
            chat_id,
            chunk,
            silent=chunk_silent,
            disable_web_preview=disable_web_preview,
        ):
            sent += 1
        if not is_last:
            time.sleep(INTER_CHUNK_DELAY_S)
    return sent


def wait_for_top_of_hour(*, gather_window_start: int = 40) -> None:
    """Hold until :00 of the next hour if we're in the gather window.

    The window is [gather_window_start, 59] in minutes. Inside the
    window, sleep until the top of the next hour. Outside the window,
    return immediately. If we've overshot :00 into :01-:10, log a
    warning so operators notice the late delivery.

    Capped at 20 minutes of sleep to defend against clock weirdness.
    """
    now = datetime.now(timezone.utc)
    if now.minute >= gather_window_start:
        wait_seconds = (60 - now.minute) * 60 - now.second
        if 0 < wait_seconds <= 1200:
            print(
                f"Holding delivery for {wait_seconds}s until :00", file=sys.stderr
            )
            time.sleep(wait_seconds)
    elif now.minute <= 10:
        print(
            f"WARNING: arrived at :{now.minute:02d} — overshot :00 target, delivering now",
            file=sys.stderr,
        )


def resolve_credentials(token_env: str = DEFAULT_TOKEN_ENV) -> tuple[str, str]:
    """Return (bot_token, chat_id) from environment. Raises RuntimeError
    with a clear message if either env var is missing or empty.

    The token_env parameter is load-bearing: never silently fall back to
    TELEGRAM_BOT_TOKEN from another agent's bot — that sends to the wrong
    destination. Caller passes the per-agent env var name explicitly.
    """
    token = os.environ.get(token_env, "")
    if not token:
        raise RuntimeError(f"{token_env} not set in environment")
    chat_id = os.environ.get(CHAT_ID_ENV, "")
    if not chat_id:
        raise RuntimeError(f"{CHAT_ID_ENV} not set in environment")
    return token, chat_id


def deliver_from_file(
    msg_file: str,
    *,
    token_env: str = DEFAULT_TOKEN_ENV,
    silent: bool = False,
    wait_for_hour: bool = True,
) -> dict:
    """End-to-end delivery for the standard `timed-deliver.py` cron flow.

    Reads the message from `msg_file`, optionally waits for :00 if we're
    in the gather window, chunks if the message exceeds 4000 chars, and
    sends via Telegram. Returns a script-contract dict with status,
    chars, chunks_sent, silent. Raises on unrecoverable error so the
    caller's main() wrapper can produce the error-shaped JSON line.
    """
    token, chat_id = resolve_credentials(token_env)

    if not os.path.exists(msg_file):
        raise FileNotFoundError(f"message file not found: {msg_file}")
    with open(msg_file, encoding="utf-8") as f:
        message = f.read().strip()
    if not message:
        raise ValueError(f"empty message file: {msg_file}")

    if wait_for_hour:
        wait_for_top_of_hour()

    sent_chunks = send_chunked(token, chat_id, message, silent=silent)

    return {
        "status": "ok",
        "chars": len(message),
        "chunks_sent": sent_chunks,
        "silent": silent,
    }


# ---------------------------------------------------------------------------
# CLI entry point — used by per-agent timed-deliver.py shims
# ---------------------------------------------------------------------------


def parse_cli_argv(argv: list[str]) -> tuple[str, bool, str]:
    """Parse the standard timed-deliver CLI argv.

    Usage: <msg_file> [--silent] [--token-env ENV_VAR_NAME]

    Returns (msg_file, silent, token_env). Raises ValueError on bad args.
    """
    token_env = DEFAULT_TOKEN_ENV
    positional: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--token-env":
            if i + 1 >= len(argv):
                raise ValueError("--token-env requires an argument")
            token_env = argv[i + 1]
            i += 2
            continue
        positional.append(arg)
        i += 1

    if not positional or positional[0].startswith("--"):
        raise ValueError(
            "usage: timed-deliver.py <message_file> [--silent] [--token-env ENV_VAR]"
        )
    msg_file = positional[0]
    silent = "--silent" in positional
    return msg_file, silent, token_env


def cli_main(argv: list[str]) -> int:
    """Full script-contract-compliant CLI entry point.

    Parses argv, runs deliver_from_file, prints a single JSON line to
    stdout, always returns 0 so the cron exec-preflight doesn't get
    a nonzero exit code that triggers retries (see SCRIPT_CONTRACT.md).
    """
    try:
        msg_file, silent, token_env = parse_cli_argv(argv)
        result = deliver_from_file(msg_file, token_env=token_env, silent=silent)
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
