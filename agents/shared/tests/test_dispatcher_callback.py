"""Tests for dispatcher callback shortcut paths and auto-attach buttons.

Extends the base dispatcher tests with Phase C behavior:
  - confirm/cancel callback shortcuts (skip LLM, call executor directly)
  - batch confirm/cancel
  - engagement callback shortcuts (like/dislike/more for Lowly Worm)
  - auto-attach inline keyboard when tool outputs contain __pending_action__
  - expired action handling
  - subprocess failure keeps action pending
  - double-tap returns "already handled"
  - unknown callback data falls through to LLM
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload():
    for mod in list(sys.modules):
        if mod in ("dispatcher", "conversation", "tool_use", "llm",
                    "telegram_api", "pending_actions"):
            del sys.modules[mod]
    import dispatcher
    return dispatcher


def _callback_update(data: str, chat_id: int = 111111111) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cbq_test_1",
            "from": {"id": chat_id, "first_name": "the operator"},
            "message": {
                "message_id": 200,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data,
        },
    }


def _text_update(text: str, chat_id: int = 111111111) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "from": {"id": chat_id, "first_name": "the operator"},
            "chat": {"id": chat_id, "type": "private"},
            "date": 1776000000,
            "text": text,
        },
    }


@pytest.fixture
def disp(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    monkeypatch.setenv("CLAWFORD_INBOX_DIR", str(inbox_dir))
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111111111")
    monkeypatch.setenv("SHOPPING_BOT_TOKEN", "FAKE_SHOPPING_TOKEN")
    monkeypatch.setenv("FIXIT_BOT_TOKEN", "FAKE_FIXIT_TOKEN")
    monkeypatch.setenv("NEWSDIGEST_BOT_TOKEN", "FAKE_NEWS_TOKEN")
    # Pre-create workspace dirs
    (tmp_path / "shopping-workspace").mkdir()
    (tmp_path / "fix-it-workspace").mkdir()
    (tmp_path / "news-digest-workspace").mkdir()
    return _reload()


def _mock_config(agent_id="shopping", token="FAKE_SHOPPING_TOKEN", executors=None):
    cfg = MagicMock()
    cfg.agent_id = agent_id
    cfg.token = token
    cfg.system_prompt = "You are Hilda."
    cfg.tools = []
    cfg.executors = executors or {}
    return cfg


# ── confirm callback shortcut ────────────────────────────────────


def test_confirm_callback_skips_llm_and_calls_executor(disp):
    import pending_actions

    result = pending_actions.stage(
        "shopping", "reorder",
        {"source": "costco", "item_number": "123", "quantity": 1},
        "Kirkland Water",
        confirm_label="\U0001f6d2 Add to cart", cancel_label="Skip",
    )
    action_id = result["action_id"]

    confirm_executor = MagicMock(return_value={"status": "ok", "item_number": "123"})
    cfg = _mock_config(executors={"confirm_reorder": confirm_executor})

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        disp.dispatch("shopping", _callback_update(f"confirm:{action_id}"))

        # LLM should NOT be called
        mock_tu.run.assert_not_called()

        # Executor should be called with the payload
        confirm_executor.assert_called_once_with(
            source="costco", item_number="123", quantity=1
        )

        # Should send a result message
        mock_tg.send_message.assert_called_once()

        # Should answer the callback query to dismiss spinner
        mock_tg.answer_callback_query.assert_called_once()
        assert mock_tg.answer_callback_query.call_args[0][1] == "cbq_test_1"


def test_confirm_calls_answer_callback_query(disp):
    import pending_actions

    result = pending_actions.stage(
        "shopping", "reorder", {"source": "costco"}, "Water",
        confirm_label="OK", cancel_label="No",
    )

    cfg = _mock_config(executors={
        "confirm_reorder": MagicMock(return_value={"status": "ok"})
    })

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg):
        disp.dispatch("shopping", _callback_update(f"confirm:{result['action_id']}"))
        mock_tg.answer_callback_query.assert_called_once()


def test_confirm_expired_action_sends_error(disp):
    """Tapping confirm on an expired action should send a user-friendly error."""
    cfg = _mock_config()

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg):
        disp.dispatch("shopping", _callback_update("confirm:act_doesnotexist"))

        mock_tg.send_message.assert_called_once()
        sent_text = mock_tg.send_message.call_args[0][2]
        assert "expired" in sent_text.lower() or "already" in sent_text.lower()


def test_confirm_subprocess_failure_keeps_action_pending(disp):
    import pending_actions

    result = pending_actions.stage(
        "shopping", "reorder",
        {"source": "costco", "item_number": "123", "quantity": 1},
        "Water",
        confirm_label="OK", cancel_label="No",
    )
    action_id = result["action_id"]

    # Executor raises an exception (simulating subprocess failure)
    confirm_executor = MagicMock(side_effect=RuntimeError("session expired"))
    cfg = _mock_config(executors={"confirm_reorder": confirm_executor})

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg):
        disp.dispatch("shopping", _callback_update(f"confirm:{action_id}"))

        # Action should still be pending (not removed)
        action = pending_actions.load_by_id("shopping", action_id)
        assert action is not None

        # Error message should be sent
        mock_tg.send_message.assert_called_once()
        sent_text = mock_tg.send_message.call_args[0][2]
        assert "session expired" in sent_text.lower() or "failed" in sent_text.lower()


def test_double_tap_confirm_second_is_noop(disp):
    import pending_actions

    result = pending_actions.stage(
        "shopping", "reorder",
        {"source": "costco", "item_number": "123", "quantity": 1},
        "Water",
        confirm_label="OK", cancel_label="No",
    )
    action_id = result["action_id"]

    confirm_executor = MagicMock(return_value={"status": "ok"})
    cfg = _mock_config(executors={"confirm_reorder": confirm_executor})

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg):
        # First tap — should succeed
        disp.dispatch("shopping", _callback_update(f"confirm:{action_id}"))
        assert confirm_executor.call_count == 1

        # Second tap — action already removed, should not call executor
        disp.dispatch("shopping", _callback_update(f"confirm:{action_id}"))
        assert confirm_executor.call_count == 1  # still 1

        # Two send_message calls: one success, one "already handled"
        assert mock_tg.send_message.call_count == 2


# ── cancel callback shortcut ────────────────────────────────────


def test_cancel_callback_removes_action(disp):
    import pending_actions

    result = pending_actions.stage(
        "shopping", "reorder", {"source": "costco"}, "Cancel me",
        confirm_label="OK", cancel_label="No",
    )
    action_id = result["action_id"]
    cfg = _mock_config()

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        disp.dispatch("shopping", _callback_update(f"cancel:{action_id}"))

        mock_tu.run.assert_not_called()
        mock_tg.send_message.assert_called_once()
        sent_text = mock_tg.send_message.call_args[0][2]
        assert "cancel" in sent_text.lower()

        assert pending_actions.load_by_id("shopping", action_id) is None


# ── batch confirm/cancel ────────────────────────────────────────


def test_confirm_all_batch(disp):
    import pending_actions

    r1 = pending_actions.stage("shopping", "reorder", {"source": "costco", "item_number": "1"}, "Item 1",
                               confirm_label="OK", cancel_label="No")
    r2 = pending_actions.stage("shopping", "reorder", {"source": "costco", "item_number": "2"}, "Item 2",
                               confirm_label="OK", cancel_label="No")

    pending_actions.assign_batch("shopping",
                                 [r1["action_id"], r2["action_id"]],
                                 "batch_test1")

    confirm_executor = MagicMock(return_value={"status": "ok"})
    cfg = _mock_config(executors={"confirm_reorder": confirm_executor})

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg):
        disp.dispatch("shopping", _callback_update("confirm_all:batch_test1"))

        assert confirm_executor.call_count == 2
        mock_tg.send_message.assert_called_once()

        # All actions should be removed
        assert pending_actions.load("shopping") == []


def test_cancel_all_batch(disp):
    import pending_actions

    r1 = pending_actions.stage("shopping", "reorder", {"source": "costco"}, "A",
                               confirm_label="OK", cancel_label="No")
    r2 = pending_actions.stage("shopping", "reorder", {"source": "costco"}, "B",
                               confirm_label="OK", cancel_label="No")

    pending_actions.assign_batch("shopping",
                                 [r1["action_id"], r2["action_id"]],
                                 "batch_cancel1")

    cfg = _mock_config()

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg):
        disp.dispatch("shopping", _callback_update("cancel_all:batch_cancel1"))

        mock_tg.send_message.assert_called_once()
        sent_text = mock_tg.send_message.call_args[0][2]
        assert "cancel" in sent_text.lower()
        assert pending_actions.load("shopping") == []


# ── engagement callback shortcut ─────────────────────────────────


def test_engagement_like_routes_to_executor(disp):
    record_engagement = MagicMock(return_value="ok")
    cfg = _mock_config(
        agent_id="news-digest", token="FAKE_NEWS_TOKEN",
        executors={"record_engagement": record_engagement},
    )

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        disp.dispatch("news-digest", _callback_update("like:3"))

        mock_tu.run.assert_not_called()
        record_engagement.assert_called_once_with(article_id="3", action="thumbs_up")
        mock_tg.answer_callback_query.assert_called_once()


def test_engagement_dislike_routes_to_executor(disp):
    record_engagement = MagicMock(return_value="ok")
    cfg = _mock_config(
        agent_id="news-digest", token="FAKE_NEWS_TOKEN",
        executors={"record_engagement": record_engagement},
    )

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg):
        disp.dispatch("news-digest", _callback_update("dislike:7"))
        record_engagement.assert_called_once_with(article_id="7", action="thumbs_down")


def test_engagement_more_routes_to_executor(disp):
    record_engagement = MagicMock(return_value="ok")
    cfg = _mock_config(
        agent_id="news-digest", token="FAKE_NEWS_TOKEN",
        executors={"record_engagement": record_engagement},
    )

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg):
        disp.dispatch("news-digest", _callback_update("more:5"))
        record_engagement.assert_called_once_with(article_id="5", action="more")


# ── debrief callbacks (Sergeant Murphy) ──────────────────────────


def test_debrief_save_routes_to_save_executor(disp, monkeypatch):
    monkeypatch.setenv("MEETINGS_BOT_TOKEN", "FAKE_MEETINGS_TOKEN")
    (Path(disp.__file__).resolve().parent.parent / "meetings-coach-workspace").mkdir(
        exist_ok=True
    )
    save_exec = MagicMock(return_value={"status": "ok", "written": 2})
    cfg = _mock_config(
        agent_id="meetings-coach", token="FAKE_MEETINGS_TOKEN",
        executors={"save_debrief": save_exec},
    )

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        disp.dispatch(
            "meetings-coach", _callback_update("debrief_save:evt-xyz"),
        )
        mock_tu.run.assert_not_called()
        save_exec.assert_called_once_with(event_id="evt-xyz")
        mock_tg.answer_callback_query.assert_called_once()


def test_debrief_dismiss_routes_to_dismiss_executor(disp, monkeypatch):
    monkeypatch.setenv("MEETINGS_BOT_TOKEN", "FAKE_MEETINGS_TOKEN")
    dismiss_exec = MagicMock(return_value={"status": "ok", "dismissed": True})
    cfg = _mock_config(
        agent_id="meetings-coach", token="FAKE_MEETINGS_TOKEN",
        executors={"dismiss_debrief": dismiss_exec},
    )

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        disp.dispatch(
            "meetings-coach", _callback_update("debrief_dismiss:evt-xyz"),
        )
        mock_tu.run.assert_not_called()
        dismiss_exec.assert_called_once_with(event_id="evt-xyz")


def test_debrief_modify_prefix_no_longer_routed(disp, monkeypatch):
    """The Modify button was removed — the operator edits via chat ('change
    item 1 to ...') and Murphy's LLM calls replace_action_items.
    Ensure the dispatcher no longer special-cases debrief_modify:* so
    any stale callback falls through to the LLM path rather than
    raising or sending a ghost reply."""
    monkeypatch.setenv("MEETINGS_BOT_TOKEN", "FAKE_MEETINGS_TOKEN")
    cfg = _mock_config(
        agent_id="meetings-coach", token="FAKE_MEETINGS_TOKEN",
        executors={},
    )
    with patch.object(disp, "telegram_api"), \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        disp.dispatch(
            "meetings-coach", _callback_update("debrief_modify:evt-xyz"),
        )
        # Falls through to the LLM path (unknown callback).
        mock_tu.run.assert_called_once()


# ── unknown callback falls through to LLM ────────────────────────


def test_unknown_callback_falls_through_to_llm(disp):
    from tool_use import ToolUseResult

    cfg = _mock_config()
    tu_result = ToolUseResult(text="hmm?", new_items=[
        {"role": "assistant", "content": "hmm?"},
    ])

    with patch.object(disp, "telegram_api"), \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("shopping", _callback_update("some_unknown_data"))

        # Should fall through to LLM path
        mock_tu.run.assert_called_once()


# ── auto-attach buttons on pending markers ────────────────────────


def test_single_pending_marker_attaches_buttons(disp):
    from tool_use import ToolUseResult

    marker_output = json.dumps({
        "__pending_action__": {"id": "act_test123"},
        "action_id": "act_test123",
        "summary": "Add water",
    })

    tu_result = ToolUseResult(
        text="I've staged a reorder for Kirkland Water.",
        new_items=[
            {"type": "function_call", "call_id": "fc1", "name": "propose_reorder",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "fc1", "output": marker_output},
            {"role": "assistant", "content": "I've staged a reorder for Kirkland Water."},
        ],
    )

    import pending_actions
    pending_actions.stage(
        "shopping", "reorder",
        {"source": "costco", "item_number": "123", "quantity": 1},
        "Add water",
        confirm_label="\U0001f6d2 Add to cart", cancel_label="Skip",
    )
    # Overwrite the ID to match our marker
    pa_path = pending_actions._pending_path("shopping")
    with open(pa_path, encoding="utf-8") as f:
        data = json.load(f)
    data["actions"][0]["id"] = "act_test123"
    with open(pa_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    cfg = _mock_config()

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("shopping", _text_update("reorder the kirkland water"))

        mock_tg.send_message.assert_called_once()
        _, send_kwargs = mock_tg.send_message.call_args
        reply_markup = send_kwargs.get("reply_markup")
        assert reply_markup is not None
        keyboard = reply_markup["inline_keyboard"]
        assert len(keyboard) >= 1
        # First row should have confirm and cancel buttons
        row = keyboard[0]
        assert any("confirm:" in btn["callback_data"] for btn in row)
        assert any("cancel:" in btn["callback_data"] for btn in row)


def test_multiple_pending_markers_attach_batch_buttons(disp):
    from tool_use import ToolUseResult

    marker1 = json.dumps({
        "__pending_action__": {"id": "act_aaa"},
        "action_id": "act_aaa",
        "summary": "Item A",
    })
    marker2 = json.dumps({
        "__pending_action__": {"id": "act_bbb"},
        "action_id": "act_bbb",
        "summary": "Item B",
    })

    tu_result = ToolUseResult(
        text="Staged 2 items.",
        new_items=[
            {"type": "function_call", "call_id": "fc1", "name": "propose_reorder",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "fc1", "output": marker1},
            {"type": "function_call", "call_id": "fc2", "name": "propose_reorder",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "fc2", "output": marker2},
            {"role": "assistant", "content": "Staged 2 items."},
        ],
    )

    import pending_actions
    # Stage two actions and fix their IDs
    pending_actions.stage("shopping", "reorder", {"item": "a"}, "Item A",
                          confirm_label="OK", cancel_label="No")
    pending_actions.stage("shopping", "reorder", {"item": "b"}, "Item B",
                          confirm_label="OK", cancel_label="No")
    pa_path = pending_actions._pending_path("shopping")
    with open(pa_path, encoding="utf-8") as f:
        data = json.load(f)
    data["actions"][0]["id"] = "act_aaa"
    data["actions"][1]["id"] = "act_bbb"
    with open(pa_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    cfg = _mock_config()

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("shopping", _text_update("reorder everything"))

        _, send_kwargs = mock_tg.send_message.call_args
        reply_markup = send_kwargs.get("reply_markup")
        assert reply_markup is not None
        keyboard = reply_markup["inline_keyboard"]

        # Should have per-item rows + batch row
        assert len(keyboard) >= 3  # 2 items + 1 batch row

        # Last row should be batch confirm/cancel
        last_row = keyboard[-1]
        batch_data = [btn["callback_data"] for btn in last_row]
        assert any("confirm_all:" in d for d in batch_data)
        assert any("cancel_all:" in d for d in batch_data)


def test_no_markers_no_reply_markup(disp):
    from tool_use import ToolUseResult

    tu_result = ToolUseResult(
        text="Just a chat reply.",
        new_items=[{"role": "assistant", "content": "Just a chat reply."}],
    )

    cfg = _mock_config()

    with patch.object(disp, "telegram_api") as mock_tg, \
         patch.object(disp, "load_agent_config", return_value=cfg), \
         patch.object(disp, "tool_use") as mock_tu:
        mock_tu.run.return_value = tu_result
        disp.dispatch("shopping", _text_update("hello"))

        _, send_kwargs = mock_tg.send_message.call_args
        assert send_kwargs.get("reply_markup") is None
