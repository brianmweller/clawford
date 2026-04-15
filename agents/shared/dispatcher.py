"""agents/shared/dispatcher.py — per-message handler for the inbox daemon.

dispatch(agent_id, update) is called by telegram_inbox for each inbound
update. Stateless entry point, hammered from the async poll loop:

  1. chat_id gate: drop anything not from the operator (TELEGRAM_CHAT_ID env)
  2. Extract the user's text from update.message.text OR the synthetic
     text from update.callback_query.data
  3. Load the agent's config (tools + executors + system prompt)
  4. Fire chat_action(typing) so the user knows we're working
  5. Load the conversation window; append the user turn
  6. Run tool_use.run() with the full input_items
  7. Persist the user turn + every new item returned from tool_use
  8. Send the final text reply

Single-user by design (TELEGRAM_CHAT_ID is a single int). Multi-user
support would need a per-chat state partitioning layer; not built.
"""
from __future__ import annotations

import logging
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import conversation  # type: ignore
import telegram_api  # type: ignore
import tool_use  # type: ignore

log = logging.getLogger(__name__)

CHAT_ID_ENV = "TELEGRAM_CHAT_ID"
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class AgentConfig:
    agent_id: str
    token: str
    system_prompt: str
    tools: list[dict] = field(default_factory=list)
    executors: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config loading — per-agent tools + system prompt assembly
# ---------------------------------------------------------------------------


AGENT_TOKEN_ENV = {
    # Order matters: first env var that's set wins. Supports both the
    # liberation-era per-agent names (FIXIT_BOT_TOKEN) and the
    # historical production name (TELEGRAM_BOT_TOKEN for fix-it).
    "fix-it": ["FIXIT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"],
    "news-digest": ["NEWSDIGEST_BOT_TOKEN"],
    "shopping": ["SHOPPING_BOT_TOKEN"],
    "family-calendar": ["FAMILYCAL_BOT_TOKEN"],
    "meetings-coach": ["MEETINGS_BOT_TOKEN"],
    "connector": ["CONNECTOR_BOT_TOKEN"],
}


def _resolve_token(agent_id: str) -> str:
    for name in AGENT_TOKEN_ENV.get(agent_id, []):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def _read_agent_doc(agent_dir: Path, name: str) -> str:
    """Read SOUL.md / USER.md / MEMORY.md if present. Returns '' on miss."""
    path = agent_dir / name
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _build_system_prompt(agent_id: str, agent_dir: Path, tools: list[dict]) -> str:
    """Concatenate SOUL + USER + MEMORY + a machine-generated tools doc."""
    parts = []
    soul = _read_agent_doc(agent_dir, "SOUL.md")
    if soul:
        parts.append("# Your identity (SOUL.md)\n\n" + soul.strip())
    user = _read_agent_doc(agent_dir, "USER.md")
    if user:
        parts.append("# The user you serve (USER.md)\n\n" + user.strip())
    memory = _read_agent_doc(agent_dir, "MEMORY.md")
    if memory:
        parts.append("# Your persistent memory (MEMORY.md)\n\n" + memory.strip())

    if tools:
        tool_lines = ["# Tools available to you", ""]
        for t in tools:
            name = t.get("name", "?")
            desc = t.get("description", "")
            tool_lines.append(f"- `{name}`: {desc}")
        parts.append("\n".join(tool_lines))

    parts.append(
        "# Response style\n\n"
        "You are replying via Telegram to a single user. Keep replies "
        "terse and conversational — no markdown headers, no wall of "
        "text. Use tools when the operator asks about real-time state; reply "
        "directly from conversation context for chat. Do not invite "
        "the user to tap buttons, type /confirm, or use any affordance "
        "that isn't already wired up."
    )
    return "\n\n".join(parts)


def load_agent_config(agent_id: str) -> AgentConfig:
    """Import the agent's tools.py module, read its SOUL/USER/MEMORY,
    and return a populated AgentConfig. Raises KeyError if agent_id is
    unknown, OSError/ImportError if its files are missing."""
    if agent_id not in AGENT_TOKEN_ENV:
        raise KeyError(f"unknown agent: {agent_id}")
    token = _resolve_token(agent_id)
    if not token:
        candidates = "/".join(AGENT_TOKEN_ENV[agent_id])
        raise KeyError(f"missing env {candidates} for {agent_id}")

    agent_dir = REPO_ROOT / "agents" / agent_id

    tools: list[dict] = []
    executors: dict = {}
    tools_mod_path = agent_dir / "tools.py"
    if tools_mod_path.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"{agent_id}_tools", tools_mod_path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            tools = getattr(mod, "TOOLS", [])
            executors = getattr(mod, "EXECUTORS", {})
        except Exception as exc:
            log.warning("failed to load %s tools.py: %s", agent_id, exc)

    system_prompt = _build_system_prompt(agent_id, agent_dir, tools)
    return AgentConfig(
        agent_id=agent_id,
        token=token,
        system_prompt=system_prompt,
        tools=tools,
        executors=executors,
    )


# ---------------------------------------------------------------------------
# Update extraction
# ---------------------------------------------------------------------------


def _authorized(update: dict) -> bool:
    expected = os.environ.get(CHAT_ID_ENV, "")
    if not expected:
        return False
    try:
        expected_int = int(expected)
    except ValueError:
        return False

    msg = update.get("message") or update.get("callback_query") or {}
    chat = msg.get("chat") or (msg.get("message") or {}).get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        # callback_query.from.id fallback
        chat_id = (msg.get("from") or {}).get("id")
    return chat_id == expected_int


def _extract_user_text(update: dict) -> str | None:
    if "message" in update:
        text = (update["message"] or {}).get("text")
        if text:
            return text.strip()
    if "callback_query" in update:
        data = (update["callback_query"] or {}).get("data")
        if data:
            return f"[callback: {data}]"
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def dispatch(agent_id: str, update: dict) -> None:
    """Handle one inbound update. Sync; the async poll loop wraps this
    in asyncio.to_thread."""
    if not _authorized(update):
        log.debug("dropping unauthorized update: %s", update.get("update_id"))
        return

    text = _extract_user_text(update)
    if not text:
        return

    try:
        cfg = load_agent_config(agent_id)
    except KeyError as exc:
        log.warning("dispatch: %s", exc)
        return
    except Exception:
        log.error("dispatch failed to load config for %s:\n%s", agent_id, traceback.format_exc())
        return

    chat_id = os.environ.get(CHAT_ID_ENV, "")

    # Typing indicator — cosmetic, best-effort, keep going on failure.
    telegram_api.send_chat_action(cfg.token, chat_id, "typing")

    user_item = {"role": "user", "content": text}

    # Load existing conversation window and assemble full input_items.
    history = conversation.load(agent_id)
    initial_items = history + [user_item]

    result = tool_use.run(
        instructions=cfg.system_prompt,
        tools=cfg.tools,
        tool_executors=cfg.executors,
        initial_items=initial_items,
    )

    # Persist the user turn regardless of outcome — we want it in the
    # window so follow-ups see it, even if the reply failed.
    conversation.append(agent_id, user_item)

    if result.ok:
        reply_text = result.text or "(no reply)"
        for new_item in result.new_items:
            conversation.append(agent_id, new_item)
    else:
        reply_text = (
            f"⚠️ couldn't respond: {result.error or 'unknown error'}"
        )
        # Still persist any partial items from the failed turn.
        for new_item in result.new_items:
            conversation.append(agent_id, new_item)

    telegram_api.send_message(cfg.token, chat_id, reply_text)
