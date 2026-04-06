#!/usr/bin/env python3
"""Telegram <-> local Claude Code relay bot.

Replaces Termius + Tailscale + Wispr Flow for remote Claude Code access.
Text and voice messages sent via Telegram are relayed to the local Claude Code CLI.

Usage:
    export RELAY_BOT_TOKEN="your-bot-token"
    export TELEGRAM_CHAT_ID="your-chat-id"
    export OPENAI_API_KEY="your-key"  # optional, enables voice messages
    python bot.py [--cwd /path/to/project]

The bot runs Claude Code in whatever directory you specify with --cwd,
or the current working directory if omitted.

Commands:
    /ping       - Check bot is alive
    /status     - Claude Code version, cwd, session info
    /cwd <path> - Change working directory
    /new        - Start a fresh conversation (new session)
"""

import argparse
import asyncio
import logging
import os
import tempfile
import uuid

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

BOT_TOKEN = os.environ["RELAY_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

MAX_MSG = 4096  # Telegram message length limit
CLAUDE_TIMEOUT = 300  # 5 minutes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def authorized(update: Update) -> bool:
    return update.effective_chat.id == CHAT_ID


def get_session(bot_data: dict) -> tuple[str, bool]:
    """Return (session_id, is_new). Creates a new session if none exists."""
    sid = bot_data.get("session_id")
    if sid:
        return sid, False
    sid = str(uuid.uuid4())
    bot_data["session_id"] = sid
    bot_data["turn_count"] = 0
    return sid, True


async def run_claude(prompt: str, cwd: str, bot_data: dict) -> str:
    """Run claude -p with session continuity."""
    sid, is_new = get_session(bot_data)

    if bot_data.get("turn_count", 0) == 0:
        cmd = ["claude", "-p", "--session-id", sid, prompt]
    else:
        cmd = ["claude", "-p", "--resume", sid, prompt]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return "Timed out (5 min)."

    bot_data["turn_count"] = bot_data.get("turn_count", 0) + 1

    output = stdout.decode().strip()
    if proc.returncode != 0 and not output:
        output = stderr.decode().strip() or f"Exit code {proc.returncode}"
    return output or "(empty response)"


async def send_long(update: Update, text: str, reply_to: int | None = None):
    """Send a message, splitting into chunks if needed."""
    chunks = [text[i:i + MAX_MSG] for i in range(0, len(text), MAX_MSG)]
    for i, chunk in enumerate(chunks):
        if i == 0 and reply_to:
            await update.effective_chat.send_message(chunk, reply_to_message_id=reply_to)
        else:
            await update.effective_chat.send_message(chunk)


async def handle_text(update: Update, context) -> None:
    if not authorized(update):
        return
    cwd = context.bot_data["cwd"]
    prompt = update.message.text

    thinking = await update.message.reply_text("...")
    response = await run_claude(prompt, cwd, context.bot_data)
    await thinking.delete()
    await send_long(update, response, reply_to=update.message.message_id)


async def handle_voice(update: Update, context) -> None:
    if not authorized(update):
        return
    if not OPENAI_API_KEY:
        await update.message.reply_text("Voice disabled -- set OPENAI_API_KEY.")
        return

    from openai import OpenAI

    voice = update.message.voice or update.message.audio
    tg_file = await voice.get_file()

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        await tg_file.download_to_drive(f.name)
        tmp_path = f.name

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        with open(tmp_path, "rb") as audio:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=audio)
        text = transcript.text
    finally:
        os.unlink(tmp_path)

    await update.message.reply_text(f">> {text}")

    thinking = await update.message.reply_text("...")
    cwd = context.bot_data["cwd"]
    response = await run_claude(text, cwd, context.bot_data)
    await thinking.delete()
    await send_long(update, response, reply_to=update.message.message_id)


async def cmd_ping(update: Update, context) -> None:
    if not authorized(update):
        return
    await update.message.reply_text("pong")


async def cmd_status(update: Update, context) -> None:
    if not authorized(update):
        return
    proc = await asyncio.create_subprocess_exec(
        "claude", "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    version = stdout.decode().strip() or "unknown"
    cwd = context.bot_data["cwd"]
    sid = context.bot_data.get("session_id", "none")
    turns = context.bot_data.get("turn_count", 0)
    await update.message.reply_text(
        f"Online\nClaude Code {version}\ncwd: {cwd}\nsession: {sid[:8]}... ({turns} turns)"
    )


async def cmd_cwd(update: Update, context) -> None:
    """Change the working directory for Claude Code."""
    if not authorized(update):
        return
    if not context.args:
        await update.message.reply_text(f"cwd: {context.bot_data['cwd']}")
        return
    new_cwd = " ".join(context.args)
    if not os.path.isdir(new_cwd):
        await update.message.reply_text(f"Not a directory: {new_cwd}")
        return
    context.bot_data["cwd"] = new_cwd
    await update.message.reply_text(f"cwd: {new_cwd}")


async def cmd_new(update: Update, context) -> None:
    """Start a fresh conversation (new session)."""
    if not authorized(update):
        return
    context.bot_data.pop("session_id", None)
    context.bot_data["turn_count"] = 0
    await update.message.reply_text("New session. Next message starts fresh.")


def main():
    parser = argparse.ArgumentParser(description="Telegram <-> Claude Code relay")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for Claude Code")
    args = parser.parse_args()

    cwd = os.path.abspath(args.cwd)
    if not os.path.isdir(cwd):
        raise SystemExit(f"Not a directory: {cwd}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["cwd"] = cwd

    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Relay bot running | chat_id=%s | cwd=%s", CHAT_ID, cwd)
    voice_status = "enabled" if OPENAI_API_KEY else "disabled (no OPENAI_API_KEY)"
    log.info("Voice: %s", voice_status)
    app.run_polling()


if __name__ == "__main__":
    main()
