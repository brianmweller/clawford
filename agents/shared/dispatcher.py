"""agents/shared/dispatcher.py — per-message handler for the inbox daemon.

dispatch(agent_id, update) is called by telegram_inbox for each inbound
update. Stateless entry point, hammered from the async poll loop:

  1. chat_id gate: drop anything not from the operator (TELEGRAM_CHAT_ID env)
  2. Callback shortcut: confirm/cancel/engagement callbacks bypass
     the LLM and call executors directly (Phase C)
  3. Extract the user's text from update.message.text OR the synthetic
     text from update.callback_query.data
  4. Load the agent's config (tools + executors + system prompt)
  5. Fire chat_action(typing) so the user knows we're working
  6. Load the conversation window; append the user turn
  7. Run tool_use.run() with the full input_items
  8. Auto-attach inline keyboard buttons on pending action markers
  9. Persist the user turn + every new item returned from tool_use
  10. Send the final text reply (with reply_markup if buttons present)

Single-user by design (TELEGRAM_CHAT_ID is a single int). Multi-user
support would need a per-chat state partitioning layer; not built.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — py<3.9
    ZoneInfo = None  # type: ignore

import brain  # type: ignore
import conversation  # type: ignore
import pending_actions  # type: ignore
import telegram_api  # type: ignore
import tool_use  # type: ignore
from scan_fields import scan_fields  # type: ignore

log = logging.getLogger(__name__)

CHAT_ID_ENV = "TELEGRAM_CHAT_ID"
USER_TZ_ENV = "CLAWFORD_USER_TZ"
DEFAULT_USER_TZ = "America/Los_Angeles"
REPO_ROOT = Path(__file__).resolve().parents[2]

# P0.4: inbound-scan wire-in. Telegram text is authenticated to the operator's
# chat ID, but the operator regularly pastes / forwards content from external
# sources (emails, LinkedIn DMs, news leads) into agent chats. That
# pasted content can carry prompt injection aimed at the agent's LLM.
# We scan every inbound text and attach warnings; in enforce mode,
# blocked text is replaced with a placeholder before reaching the LLM.
# the operator's direct typing almost never trips the regexes — the scanner's
# bias toward SAFE + the explicit false-positive corpus in
# test_inbound_scanner.py keep normal chat unaffected.
INBOX_QUARANTINE_ROOT = Path(
    os.path.expanduser("~/.clawford/inbox-quarantine")
)


def _current_user_context() -> str:
    """Render a short 'current time in user's timezone' block to
    inject into the system prompt. The VPS runs UTC but the user is
    in Pacific — without this the LLM mislabels 'today'/'tomorrow'
    and fights the tool results that are already in PT."""
    tz_name = os.environ.get(USER_TZ_ENV, DEFAULT_USER_TZ)
    if ZoneInfo is None:
        now = datetime.now()
        return f"Current time: {now.strftime('%Y-%m-%d %H:%M %A')} (timezone unknown)"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_USER_TZ)
        tz_name = DEFAULT_USER_TZ
    now = datetime.now(tz)
    return (
        f"Current time in user's timezone: "
        f"{now.strftime('%Y-%m-%d %H:%M %Z')} "
        f"({now.strftime('%A')})\n"
        f"User timezone: {tz_name}"
    )


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


def _read_agent_doc(agent_id: str, name: str) -> str:
    """Read a conversational doc from the Dropbox brain.

    Canonical location for SOUL/IDENTITY/USER/AGENTS/MEMORY since the
    migration to ~/Dropbox/openclaw-backup/agents/<agent_id>/. Returns
    '' if the file is missing so missing docs degrade gracefully.
    """
    try:
        path = brain.agent_config_path(agent_id, name)
    except ValueError:
        return ""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


_META_QA_PREAMBLE = (
    "# Answering questions about yourself\n\n"
    "When the operator asks about your recent activity, state, or why "
    "something did or didn't happen (e.g. \"did you run today?\", "
    "\"what did you draft?\", \"why is X empty?\"), your first "
    "action must be to call your state-inspection tools — typically "
    "`get_recent_runs` — before composing a reply. Do not "
    "confabulate from the static description of your role: the "
    "description tells you what you aim to do, the tools tell you "
    "what actually happened. If the tools return no matching "
    "activity, say so plainly rather than inventing history."
)


def _build_system_prompt(agent_id: str, tools: list[dict]) -> str:
    """Concatenate the 5 conversational docs + tool manifest + context.

    Loaded from Dropbox brain: SOUL (principles), IDENTITY (persona/voice),
    USER (who the operator is), AGENTS (fleet map for cross-agent routing),
    MEMORY (learned rules, writable via the remember tool).

    Prepends the fleet-wide meta-QA preamble so every agent grounds
    its answers in tool output rather than static prompt knowledge.
    """
    parts = [_META_QA_PREAMBLE]
    soul = _read_agent_doc(agent_id, "SOUL.md")
    if soul:
        parts.append("# Your identity (SOUL.md)\n\n" + soul.strip())
    identity = _read_agent_doc(agent_id, "IDENTITY.md")
    if identity:
        parts.append("# Your persona (IDENTITY.md)\n\n" + identity.strip())
    user = _read_agent_doc(agent_id, "USER.md")
    if user:
        parts.append("# The user you serve (USER.md)\n\n" + user.strip())
    agents_doc = _read_agent_doc(agent_id, "AGENTS.md")
    if agents_doc:
        parts.append("# The fleet (AGENTS.md)\n\n" + agents_doc.strip())
    memory = _read_agent_doc(agent_id, "MEMORY.md")
    if memory:
        parts.append("# Your persistent memory (MEMORY.md)\n\n" + memory.strip())

    if tools:
        tool_lines = ["# Tools available to you", ""]
        for t in tools:
            name = t.get("name", "?")
            desc = t.get("description", "")
            tool_lines.append(f"- `{name}`: {desc}")
        parts.append("\n".join(tool_lines))

    parts.append("# Current context\n\n" + _current_user_context())

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

    system_prompt = _build_system_prompt(agent_id, tools)
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


def _is_forwarded(update: dict) -> bool:
    """Telegram marks forwarded messages with forward_* fields. the operator's
    direct typing never has those; forwarded content (from unknown
    senders, groups, or channels) always does."""
    msg = update.get("message") or {}
    return bool(
        msg.get("forward_date")
        or msg.get("forward_from")
        or msg.get("forward_from_chat")
        or msg.get("forward_sender_name")
    )


def _scan_inbound_text(
    text: str, agent_id: str, update: dict
) -> tuple[str, list[dict]]:
    """Scan the inbound Telegram text and return (possibly-sanitized
    text, warnings). Warnings are logged to the per-agent workspace
    quarantine dir regardless of outcome so weekly operator review
    has the full forensic trail.

    Callback synthesis ('[callback: ...]') is not scanned — the text
    is produced by our own code, not the user.
    """
    if text.startswith("[callback:") and text.endswith("]"):
        return text, []
    msg_id = str(
        (update.get("message") or {}).get("message_id", "")
    ) or "unknown"
    # Put the quarantine dir under a dispatcher-owned root so no
    # individual agent workspace has to exist for this wire-in (and
    # so cross-agent review is one grep away).
    workspace = INBOX_QUARANTINE_ROOT / agent_id
    sanitized, warnings = scan_fields(
        fields={"text": text},
        source_type=f"telegram-inbox-forwarded" if _is_forwarded(update)
        else "telegram-inbox",
        source_id=msg_id,
        workspace=workspace,
    )
    return sanitized["text"], warnings


# ---------------------------------------------------------------------------
# Callback shortcut — confirm/cancel/engagement bypass the LLM
# ---------------------------------------------------------------------------

ENGAGEMENT_MAP = {
    "like": "thumbs_up",
    "dislike": "thumbs_down",
    "more": "more",
}


def _extract_callback_query(update: dict) -> tuple[str, str] | None:
    """If this update is a callback_query, return (cbq_id, data).
    Otherwise return None."""
    cbq = update.get("callback_query")
    if not cbq:
        return None
    cbq_id = cbq.get("id", "")
    data = cbq.get("data", "")
    if not data:
        return None
    return cbq_id, data


def _handle_confirm(
    cfg: AgentConfig, agent_id: str, chat_id: str,
    action_id: str, cbq_id: str,
) -> None:
    telegram_api.answer_callback_query(cfg.token, cbq_id)

    action = pending_actions.load_by_id(agent_id, action_id)
    if action is None:
        telegram_api.send_message(
            cfg.token, chat_id,
            "That action has expired or was already handled.",
            skip_review=True,
        )
        return

    kind = action.get("kind", "")
    executor_name = f"confirm_{kind}"
    executor = cfg.executors.get(executor_name)
    if executor is None:
        pending_actions.remove(agent_id, action_id)
        telegram_api.send_message(
            cfg.token, chat_id,
            f"No handler for action kind '{kind}'.",
            skip_review=True,
        )
        return

    try:
        result = executor(**action.get("payload", {}))
        pending_actions.remove(agent_id, action_id)
        if isinstance(result, dict):
            status = result.get("status", "ok")
            if status == "error":
                msg = f"Failed: {result.get('error', result.get('message', 'unknown'))}"
            else:
                summary = action.get("summary", action_id)
                msg = f"Done: {summary}"
        else:
            msg = str(result) if result else f"Done: {action.get('summary', action_id)}"
        telegram_api.send_message(cfg.token, chat_id, msg, skip_review=True)
    except Exception as exc:
        log.error("confirm executor %s failed: %s", executor_name, exc)
        telegram_api.send_message(
            cfg.token, chat_id,
            f"Failed: {exc}",
            skip_review=True,
        )


def _handle_cancel(
    cfg: AgentConfig, agent_id: str, chat_id: str,
    action_id: str, cbq_id: str,
) -> None:
    telegram_api.answer_callback_query(cfg.token, cbq_id)

    action = pending_actions.remove(agent_id, action_id)
    if action is None:
        telegram_api.send_message(
            cfg.token, chat_id,
            "Already handled or expired.",
            skip_review=True,
        )
        return

    summary = action.get("summary", action_id)
    telegram_api.send_message(
        cfg.token, chat_id, f"Cancelled: {summary}", skip_review=True,
    )


def _handle_confirm_all(
    cfg: AgentConfig, agent_id: str, chat_id: str,
    batch_id: str, cbq_id: str,
) -> None:
    telegram_api.answer_callback_query(cfg.token, cbq_id)

    actions = pending_actions.load_by_batch(agent_id, batch_id)
    if not actions:
        telegram_api.send_message(
            cfg.token, chat_id,
            "No pending actions in that batch (expired or already handled).",
            skip_review=True,
        )
        return

    results = []
    for action in actions:
        kind = action.get("kind", "")
        executor = cfg.executors.get(f"confirm_{kind}")
        if executor is None:
            results.append(f"- {action.get('summary', '?')}: no handler")
            pending_actions.remove(agent_id, action["id"])
            continue
        try:
            executor(**action.get("payload", {}))
            pending_actions.remove(agent_id, action["id"])
            results.append(f"- {action.get('summary', '?')}: done")
        except Exception as exc:
            results.append(f"- {action.get('summary', '?')}: failed ({exc})")

    telegram_api.send_message(
        cfg.token, chat_id,
        f"Batch confirmed ({len(results)} items):\n" + "\n".join(results),
        skip_review=True,
    )


def _handle_cancel_all(
    cfg: AgentConfig, agent_id: str, chat_id: str,
    batch_id: str, cbq_id: str,
) -> None:
    telegram_api.answer_callback_query(cfg.token, cbq_id)

    actions = pending_actions.load_by_batch(agent_id, batch_id)
    if not actions:
        telegram_api.send_message(
            cfg.token, chat_id,
            "No pending actions in that batch.",
            skip_review=True,
        )
        return

    for action in actions:
        pending_actions.remove(agent_id, action["id"])

    telegram_api.send_message(
        cfg.token, chat_id,
        f"Cancelled all {len(actions)} items.",
        skip_review=True,
    )


# Standardized acknowledgement for any silent-state-write button press
# (engagement / nudge / task). Without this, the only feedback for a
# tap is the brief Telegram toast — which the operator routinely misses. The
# in-place edit collapses the button row and prepends a status banner
# so the message itself becomes the durable indicator.
def _ack_button_press(
    cfg: AgentConfig,
    update: dict,
    cbq_id: str,
    *,
    toast: str,
    banner: str,
) -> None:
    telegram_api.answer_callback_query(cfg.token, cbq_id, text=toast)
    msg = (update.get("callback_query") or {}).get("message") or {}
    message_id = msg.get("message_id")
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    original = msg.get("text") or ""
    if message_id is None or chat_id is None:
        return
    new_text = f"{banner}\n\n{original}".rstrip() if original else banner
    telegram_api.edit_message_text(
        cfg.token, str(chat_id), message_id, new_text,
    )


_ENGAGEMENT_BANNER = {
    "thumbs_up": "\U0001f44d Liked",
    "thumbs_down": "\U0001f44e Disliked",
    "more": "\U0001f4d6 Opened",
}


def _handle_engagement(
    cfg: AgentConfig, chat_id: str,
    action_type: str, article_id: str, cbq_id: str, update: dict,
) -> None:
    _ack_button_press(
        cfg, update, cbq_id,
        toast="Noted!",
        banner=_ENGAGEMENT_BANNER.get(action_type, "Noted"),
    )

    executor = cfg.executors.get("record_engagement")
    if executor is None:
        return

    try:
        executor(article_id=article_id, action=action_type)
    except Exception as exc:
        log.warning("engagement executor failed: %s", exc)


# nudge_{done,snoozed,ignored}:<slug> → handle_nudge_action(slug, action)
# "snooze" is the button/user-facing word; we normalize to the status
# value people-scan.py / snoozes.json use ("snoozed").
_NUDGE_CALLBACK_PREFIXES = {
    "nudge_done": "done",
    "nudge_snooze": "snoozed",
    "nudge_ignore": "ignored",
}

_NUDGE_TOAST = {
    "done": "\u2705 Marked done",
    "snoozed": "\U0001f515 Snoozed",
    "ignored": "\U0001f648 Ignored",
}


def _handle_nudge_callback(
    cfg: AgentConfig, chat_id: str,
    action: str, slug: str, cbq_id: str, update: dict,
) -> None:
    """Route a Relationship-Check button press to the agent's
    handle_nudge_action executor, which writes to snoozes.json."""
    _ack_button_press(
        cfg, update, cbq_id,
        toast=_NUDGE_TOAST.get(action, "Noted"),
        banner=_NUDGE_TOAST.get(action, "Noted"),
    )
    executor = cfg.executors.get("handle_nudge_action")
    if executor is None:
        log.warning("no handle_nudge_action executor on %s", cfg.agent_id)
        return
    try:
        executor(slug=slug, action=action)
    except Exception as exc:
        log.warning("handle_nudge_action failed: %s", exc)


# task_{done,snooze,ignore}:<task_id> — Mistress Mouse task reminders.
# The executor (family-calendar/task_callback.py) flips status in queue.md
# or advances due_at for snoozes. Propagation to Google Tasks happens at
# the next family-calendar-tasks-sync cron tick (≤5 min).
_TASK_CALLBACK_PREFIXES = {
    "task_done": ("done", "\u2705 Marked done"),
    "task_snooze": ("snooze", "\u23ed Snoozed"),
    "task_ignore": ("ignore", "\U0001f6ab Ignored"),
}


def _handle_task_callback(
    cfg: AgentConfig, chat_id: str,
    action: str, task_id: str, cbq_id: str, update: dict,
) -> None:
    toast = next(
        (t for p, (a, t) in _TASK_CALLBACK_PREFIXES.items() if a == action),
        "Noted",
    )
    _ack_button_press(
        cfg, update, cbq_id, toast=toast, banner=toast,
    )
    executor = cfg.executors.get("handle_task_callback")
    if executor is None:
        log.warning("no handle_task_callback executor on %s", cfg.agent_id)
        return
    try:
        executor(action=action, task_id=task_id)
    except Exception as exc:
        log.warning("handle_task_callback failed: %s", exc)


# debrief_{save,dismiss}:<event_id> — Sergeant Murphy post-meeting
# buttons. Save appends action items to commitments/active.md; Dismiss
# deletes the pending file. 'See more' is a native Telegram URL button
# and doesn't need a callback handler. Modify was removed — the operator
# edits via chat (LLM calls replace_action_items).
_DEBRIEF_CALLBACK_PREFIXES = {
    "debrief_save": ("save_debrief", "\u2705 Saving..."),
    "debrief_dismiss": ("dismiss_debrief", "\u274c Dismissed"),
}


def _handle_debrief_callback(
    cfg: AgentConfig, chat_id: str,
    prefix: str, event_id: str, cbq_id: str,
) -> None:
    executor_name, toast = _DEBRIEF_CALLBACK_PREFIXES[prefix]
    telegram_api.answer_callback_query(cfg.token, cbq_id, text=toast)

    executor = cfg.executors.get(executor_name)
    if executor is None:
        log.warning("no %s executor on %s", executor_name, cfg.agent_id)
        telegram_api.send_message(
            cfg.token, chat_id,
            f"No handler for {prefix}.",
            skip_review=True,
        )
        return

    try:
        result = executor(event_id=event_id)
    except Exception as exc:
        log.warning("%s failed: %s", executor_name, exc)
        telegram_api.send_message(
            cfg.token, chat_id,
            f"Failed: {exc}",
            skip_review=True,
        )
        return

    if not isinstance(result, dict):
        return

    status = result.get("status", "ok")
    if status != "ok":
        telegram_api.send_message(
            cfg.token, chat_id,
            f"Failed: {result.get('error', 'unknown')}",
            skip_review=True,
        )
        return

    if prefix == "debrief_save":
        if result.get("already_saved"):
            msg = "Already saved earlier — no changes written."
        elif result.get("written"):
            n = result["written"]
            msg = f"\u2705 Saved {n} item{'s' if n != 1 else ''} to commitments."
        else:
            msg = "Nothing to save — pending cleared."
        telegram_api.send_message(cfg.token, chat_id, msg, skip_review=True)
    elif prefix == "debrief_dismiss":
        telegram_api.send_message(
            cfg.token, chat_id,
            "\u274c Debrief dismissed.", skip_review=True,
        )


def _try_callback_shortcut(
    cfg: AgentConfig, agent_id: str, chat_id: str, update: dict,
) -> bool:
    """Try to handle the update as a callback shortcut. Returns True if
    handled (caller should return), False if it should fall through to
    the normal LLM path."""
    cbq = _extract_callback_query(update)
    if cbq is None:
        return False
    cbq_id, data = cbq

    if data.startswith("confirm_all:"):
        batch_id = data[len("confirm_all:"):]
        _handle_confirm_all(cfg, agent_id, chat_id, batch_id, cbq_id)
        return True

    if data.startswith("cancel_all:"):
        batch_id = data[len("cancel_all:"):]
        _handle_cancel_all(cfg, agent_id, chat_id, batch_id, cbq_id)
        return True

    if data.startswith("confirm:"):
        action_id = data[len("confirm:"):]
        _handle_confirm(cfg, agent_id, chat_id, action_id, cbq_id)
        return True

    if data.startswith("cancel:"):
        action_id = data[len("cancel:"):]
        _handle_cancel(cfg, agent_id, chat_id, action_id, cbq_id)
        return True

    # Engagement callbacks: like:N, dislike:N, more:N
    for prefix, action_type in ENGAGEMENT_MAP.items():
        if data.startswith(prefix + ":"):
            article_id = data[len(prefix) + 1:]
            _handle_engagement(cfg, chat_id, action_type, article_id, cbq_id, update)
            return True

    # Relationship-Check nudge callbacks: nudge_done:<slug>,
    # nudge_snooze:<slug>, nudge_ignore:<slug>
    for prefix, action in _NUDGE_CALLBACK_PREFIXES.items():
        if data.startswith(prefix + ":"):
            slug = data[len(prefix) + 1:]
            _handle_nudge_callback(cfg, chat_id, action, slug, cbq_id, update)
            return True

    # Task reminder buttons (Mistress Mouse): task_{done,snooze,ignore}:<task_id>
    for prefix, (action, _toast) in _TASK_CALLBACK_PREFIXES.items():
        if data.startswith(prefix + ":"):
            task_id = data[len(prefix) + 1:]
            _handle_task_callback(cfg, chat_id, action, task_id, cbq_id, update)
            return True

    # Debrief buttons (Sergeant Murphy): debrief_{save,dismiss,modify}:<event_id>
    for prefix in _DEBRIEF_CALLBACK_PREFIXES:
        if data.startswith(prefix + ":"):
            event_id = data[len(prefix) + 1:]
            _handle_debrief_callback(cfg, chat_id, prefix, event_id, cbq_id)
            return True

    return False


# ---------------------------------------------------------------------------
# Auto-attach inline keyboard on pending action markers
# ---------------------------------------------------------------------------


def _scan_pending_markers(new_items: list[dict]) -> list[dict]:
    """Extract __pending_action__ markers from tool outputs."""
    markers = []
    for item in new_items:
        if item.get("type") != "function_call_output":
            continue
        try:
            data = json.loads(item.get("output", ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        marker = data.get("__pending_action__")
        if isinstance(marker, dict) and "id" in marker:
            markers.append(marker)
    return markers


def _build_reply_markup(
    agent_id: str, markers: list[dict], batch_id: str | None,
) -> dict | None:
    """Build an inline_keyboard reply_markup from pending action markers."""
    if not markers:
        return None

    rows = []
    for marker in markers:
        action_id = marker["id"]
        action = pending_actions.load_by_id(agent_id, action_id)
        if action is None:
            continue
        confirm_label = action.get("confirm_label", "\u2705 Confirm")
        cancel_label = action.get("cancel_label", "\u274c Cancel")
        rows.append([
            {"text": confirm_label, "callback_data": f"confirm:{action_id}"},
            {"text": cancel_label, "callback_data": f"cancel:{action_id}"},
        ])

    if batch_id and len(markers) >= 2:
        rows.append([
            {"text": f"\u2705 Confirm all {len(markers)}",
             "callback_data": f"confirm_all:{batch_id}"},
            {"text": "\u274c Cancel all",
             "callback_data": f"cancel_all:{batch_id}"},
        ])

    if not rows:
        return None
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def dispatch(agent_id: str, update: dict) -> None:
    """Handle one inbound update. Sync; the async poll loop wraps this
    in asyncio.to_thread."""
    if not _authorized(update):
        log.debug("dropping unauthorized update: %s", update.get("update_id"))
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

    # Callback shortcut — confirm/cancel/engagement skip the LLM.
    if _try_callback_shortcut(cfg, agent_id, chat_id, update):
        return

    text = _extract_user_text(update)
    if not text:
        return

    # P0.4: scan before the text enters the LLM prompt. Warnings are
    # logged to the per-agent quarantine dir; in enforce mode blocked
    # text is already replaced with a placeholder when it returns
    # from _scan_inbound_text.
    text, inbound_scan_warnings = _scan_inbound_text(text, agent_id, update)
    if inbound_scan_warnings:
        log.warning(
            "dispatcher.inbound_scan: agent=%s warnings=%s",
            agent_id, inbound_scan_warnings,
        )

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

    # Auto-attach inline keyboard if tool outputs contain pending actions.
    reply_markup = None
    if result.ok:
        markers = _scan_pending_markers(result.new_items)
        if markers:
            batch_id = None
            if len(markers) >= 2:
                batch_id = "batch_" + secrets.token_hex(4)
                action_ids = [m["id"] for m in markers]
                pending_actions.assign_batch(agent_id, action_ids, batch_id)
            reply_markup = _build_reply_markup(agent_id, markers, batch_id)

    # Pass agent_id so the outbound reviewer (P0.1) can classify
    # this LLM-composed reply against the agent's declared role
    # before it leaves the process.
    telegram_api.send_message(
        cfg.token, chat_id, reply_text,
        reply_markup=reply_markup,
        agent_id=agent_id,
    )
