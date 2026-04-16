"""Tests for agents/shared/dispatcher.py — the per-agent request handler.

Contract: dispatch(agent_id, update) is called by telegram_inbox for
each inbound update. It:

  1. Filters on effective_chat.id == TELEGRAM_CHAT_ID (drops otherwise)
  2. Extracts the message text or callback_query data
  3. Loads the per-agent AgentConfig (tools, executors, system prompt)
  4. Loads the conversation window
  5. Sends a chat_action(typing)
  6. Appends the user turn to the window + builds input_items
  7. Runs tool_use.run(...)
  8. Persists every new item to conversation
  9. Sends the final text reply to Telegram

Tests mock all I/O at the seams: telegram_api, conversation, tool_use,
and the per-agent config loader.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload():
    for mod in list(sys.modules):
        if mod in ("dispatcher", "conversation", "tool_use", "llm", "telegram_api"):
            del sys.modules[mod]
    import dispatcher  # type: ignore
    return dispatcher


def _text_update(text: str, chat_id: int = 111111111, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 100 + update_id,
            "from": {"id": chat_id, "first_name": "the operator"},
            "chat": {"id": chat_id, "type": "private"},
            "date": 1776000000,
            "text": text,
        },
    }


def _callback_update(data: str, chat_id: int = 111111111) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cbq_1",
            "from": {"id": chat_id, "first_name": "the operator"},
            "message": {
                "message_id": 200,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data,
        },
    }


@pytest.fixture
def disp(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    monkeypatch.setenv("CLAWFORD_INBOX_DIR", str(inbox_dir))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111111111")
    monkeypatch.setenv("FIXIT_BOT_TOKEN", "FAKE_FIXIT_TOKEN")
    return _reload()


def test_drops_update_from_unauthorized_chat(disp):
    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config") as mock_cfg, \
         patch.object(disp, "tool_use") as mock_tu:
        disp.dispatch("fix-it", _text_update("hi", chat_id=999))
        mock_tg.send_message.assert_not_called()
        mock_tg.send_chat_action.assert_not_called()
        mock_cfg.assert_not_called()
        mock_tu.run.assert_not_called()


def test_text_message_routes_through_tool_use_and_sends_reply(disp):
    from tool_use import ToolUseResult

    mock_cfg = MagicMock()
    mock_cfg.token = "FAKE_FIXIT_TOKEN"
    mock_cfg.system_prompt = "You are Mr Fixit."
    mock_cfg.tools = [{"type": "function", "name": "get_fleet_health"}]
    mock_cfg.executors = {"get_fleet_health": lambda: "ok"}

    tu_result = ToolUseResult(
        text="All six agents nominal.",
        new_items=[{"role": "assistant", "content": "All six agents nominal."}],
    )

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=mock_cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("fix-it", _text_update("how's the fleet?"))

        mock_tg.send_chat_action.assert_called_once()
        args, kwargs = mock_tg.send_chat_action.call_args
        assert args[0] == "FAKE_FIXIT_TOKEN"
        assert args[1] == "111111111"
        assert "typing" in (args[2],) or kwargs.get("action") == "typing"

        mock_tu.run.assert_called_once()
        _, tu_kwargs = mock_tu.run.call_args
        assert tu_kwargs["instructions"] == "You are Mr Fixit."
        assert tu_kwargs["tools"] == mock_cfg.tools
        assert tu_kwargs["tool_executors"] == mock_cfg.executors
        initial_items = tu_kwargs["initial_items"]
        assert initial_items[-1]["role"] == "user"
        assert initial_items[-1]["content"] == "how's the fleet?"

        mock_tg.send_message.assert_called_once()
        send_args, send_kwargs = mock_tg.send_message.call_args
        assert send_args[0] == "FAKE_FIXIT_TOKEN"
        assert send_args[1] == "111111111"
        assert send_args[2] == "All six agents nominal."


def test_user_turn_and_new_items_are_persisted_to_conversation(disp):
    from tool_use import ToolUseResult
    import conversation

    mock_cfg = MagicMock()
    mock_cfg.token = "FAKE_FIXIT_TOKEN"
    mock_cfg.system_prompt = "Be terse."
    mock_cfg.tools = []
    mock_cfg.executors = {}

    tu_result = ToolUseResult(
        text="noted.",
        new_items=[{"role": "assistant", "content": "noted."}],
    )

    with patch.object(disp, "telegram_api"), \
         patch.object(disp, "load_agent_config", return_value=mock_cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("fix-it", _text_update("remember this"))

    saved = conversation.load("fix-it")
    # The dispatcher should have persisted the user turn AND the assistant reply.
    assert len(saved) == 2
    assert saved[0] == {"role": "user", "content": "remember this"}
    assert saved[1] == {"role": "assistant", "content": "noted."}


def test_conversation_window_is_prepended_to_initial_items(disp):
    import conversation
    from tool_use import ToolUseResult

    conversation.append("fix-it", {"role": "user", "content": "first"})
    conversation.append("fix-it", {"role": "assistant", "content": "first reply"})

    mock_cfg = MagicMock()
    mock_cfg.token = "FAKE_FIXIT_TOKEN"
    mock_cfg.system_prompt = ""
    mock_cfg.tools = []
    mock_cfg.executors = {}

    tu_result = ToolUseResult(text="second reply", new_items=[
        {"role": "assistant", "content": "second reply"},
    ])

    with patch.object(disp, "telegram_api"), \
         patch.object(disp, "load_agent_config", return_value=mock_cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("fix-it", _text_update("second"))

        tu_kwargs = mock_tu.run.call_args[1]
        items = tu_kwargs["initial_items"]
        assert len(items) == 3
        assert items[0]["content"] == "first"
        assert items[1]["content"] == "first reply"
        assert items[2] == {"role": "user", "content": "second"}


def test_tool_use_error_sends_user_friendly_error_reply(disp):
    from tool_use import ToolUseResult

    mock_cfg = MagicMock()
    mock_cfg.token = "FAKE_FIXIT_TOKEN"
    mock_cfg.system_prompt = ""
    mock_cfg.tools = []
    mock_cfg.executors = {}

    tu_result = ToolUseResult(error="network down", returncode=500)

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=mock_cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("fix-it", _text_update("hi"))

        mock_tg.send_message.assert_called_once()
        sent_text = mock_tg.send_message.call_args[0][2]
        # User-facing message must mention the error but not crash.
        assert "error" in sent_text.lower() or "couldn" in sent_text.lower() or "failed" in sent_text.lower()


def test_callback_query_routes_as_user_message(disp):
    """Tapping an inline keyboard button with an unrecognized prefix
    becomes a synthetic user turn — the dispatcher routes it through
    tool_use like any other message. (Known prefixes like confirm:/
    cancel:/like: are handled by the callback shortcut path instead.)"""
    from tool_use import ToolUseResult

    mock_cfg = MagicMock()
    mock_cfg.token = "FAKE_FIXIT_TOKEN"
    mock_cfg.system_prompt = ""
    mock_cfg.tools = []
    mock_cfg.executors = {}

    tu_result = ToolUseResult(text="done", new_items=[
        {"role": "assistant", "content": "done"},
    ])

    with patch.object(disp, "telegram_api"), \
         patch.object(disp, "load_agent_config", return_value=mock_cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("fix-it", _callback_update("custom_action:42"))

        tu_kwargs = mock_tu.run.call_args[1]
        user_turn = tu_kwargs["initial_items"][-1]
        assert user_turn["role"] == "user"
        assert "custom_action:42" in user_turn["content"]


def test_empty_message_text_is_ignored(disp):
    mock_cfg_obj = MagicMock()
    mock_cfg_obj.token = "FAKE_FIXIT_TOKEN"
    mock_cfg_obj.executors = {}

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=mock_cfg_obj), \
         patch.object(disp, "tool_use") as mock_tu:
        # Update with no text and no callback_query
        update = {"update_id": 5, "message": {
            "from": {"id": 111111111}, "chat": {"id": 111111111},
        }}
        disp.dispatch("fix-it", update)
        mock_tg.send_message.assert_not_called()
        mock_tu.run.assert_not_called()


def test_unknown_agent_id_logs_but_doesnt_crash(disp):
    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", side_effect=KeyError("no such agent")):
        disp.dispatch("nonsense", _text_update("hi"))
        mock_tg.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# System prompt assembly — ensure all 5 conversational docs are loaded
# ---------------------------------------------------------------------------


def test_system_prompt_loads_identity_md(disp, tmp_path):
    """IDENTITY.md contains persona/voice/catchphrases — must be in prompt."""
    agent_dir = tmp_path / "fake-agent"
    agent_dir.mkdir()
    (agent_dir / "IDENTITY.md").write_text(
        "I am Hilda Hippo. Catchphrase: 'Let me check the cart!'",
        encoding="utf-8",
    )
    prompt = disp._build_system_prompt("fake-agent", agent_dir, [])
    assert "Hilda Hippo" in prompt
    assert "IDENTITY.md" in prompt
    assert "Let me check the cart" in prompt


def test_system_prompt_loads_agents_md(disp, tmp_path):
    """AGENTS.md contains fleet map + cross-agent routing rules."""
    agent_dir = tmp_path / "fake-agent"
    agent_dir.mkdir()
    (agent_dir / "AGENTS.md").write_text(
        "Mistress Mouse handles family calendar. Sergeant Murphy handles meetings.",
        encoding="utf-8",
    )
    prompt = disp._build_system_prompt("fake-agent", agent_dir, [])
    assert "Mistress Mouse" in prompt
    assert "AGENTS.md" in prompt
    assert "Sergeant Murphy" in prompt


def test_system_prompt_loads_all_five_conversational_docs(disp, tmp_path):
    """SOUL + IDENTITY + USER + AGENTS + MEMORY all present when files exist."""
    agent_dir = tmp_path / "fake-agent"
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (agent_dir / "IDENTITY.md").write_text("IDENTITY_CONTENT", encoding="utf-8")
    (agent_dir / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (agent_dir / "AGENTS.md").write_text("AGENTS_CONTENT", encoding="utf-8")
    (agent_dir / "MEMORY.md").write_text("MEMORY_CONTENT", encoding="utf-8")

    prompt = disp._build_system_prompt("fake-agent", agent_dir, [])
    assert "SOUL_CONTENT" in prompt
    assert "IDENTITY_CONTENT" in prompt
    assert "USER_CONTENT" in prompt
    assert "AGENTS_CONTENT" in prompt
    assert "MEMORY_CONTENT" in prompt

    # Operational docs should NOT be loaded into the conversational prompt
    (agent_dir / "HEARTBEAT.md").write_text("HEARTBEAT_CONTENT", encoding="utf-8")
    (agent_dir / "CRONS.md").write_text("CRONS_CONTENT", encoding="utf-8")
    (agent_dir / "TOOLS.md").write_text("TOOLS_DOC_CONTENT", encoding="utf-8")
    prompt2 = disp._build_system_prompt("fake-agent", agent_dir, [])
    assert "HEARTBEAT_CONTENT" not in prompt2
    assert "CRONS_CONTENT" not in prompt2
    assert "TOOLS_DOC_CONTENT" not in prompt2


def test_system_prompt_skips_missing_docs(disp, tmp_path):
    """Missing docs should be silently skipped, not error."""
    agent_dir = tmp_path / "fake-agent"
    agent_dir.mkdir()
    (agent_dir / "SOUL.md").write_text("ONLY_SOUL", encoding="utf-8")
    # No other docs
    prompt = disp._build_system_prompt("fake-agent", agent_dir, [])
    assert "ONLY_SOUL" in prompt
    assert "IDENTITY.md" not in prompt
    assert "AGENTS.md" not in prompt
