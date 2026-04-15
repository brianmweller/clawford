"""Tests for agents/shared/tool_use.py — the two-turn tool-use loop.

Contract: run(instructions, tools, tool_executors, initial_items, max_iters)
  1. Calls llm.infer(input_items=initial_items, tools=tools, instructions=...)
  2. If the result is text → return (text, updated_items)
  3. If the result is a function_call → execute the matching executor,
     append function_call + function_call_output items, recurse
  4. After max_iters consecutive tool calls with no text → return a
     bounded-loop error

Every hop appends items to a running list so the caller can persist
them to the conversation log in one atomic batch.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _text_result(text: str):
    from llm import InferResult
    return InferResult(text=text, model="gpt-5.4", returncode=0,
                       response_id=f"resp_{hash(text) % 1000}")


def _tool_call_result(name: str, args: dict, call_id: str = "call_x"):
    from llm import InferResult
    return InferResult(
        text="",
        model="gpt-5.4",
        returncode=0,
        function_call={"call_id": call_id, "name": name, "arguments": args},
        response_id=f"resp_{call_id}",
    )


def _reload_tool_use():
    for mod in list(sys.modules):
        if mod in ("tool_use", "llm"):
            del sys.modules[mod]
    import tool_use  # type: ignore
    return tool_use


def test_text_only_reply_returns_text_and_appended_items():
    tool_use = _reload_tool_use()
    initial = [{"role": "user", "content": "hi"}]
    with patch("tool_use.llm_infer", return_value=_text_result("hi back")):
        result = tool_use.run(
            instructions="be terse",
            tools=[],
            tool_executors={},
            initial_items=initial,
        )
    assert result.ok
    assert result.text == "hi back"
    assert result.new_items == [{"role": "assistant", "content": "hi back"}]
    assert result.error is None


def test_tool_call_then_text_completes_in_two_turns():
    tool_use = _reload_tool_use()

    infer_calls = []
    def fake_infer(**kwargs):
        infer_calls.append(kwargs)
        if len(infer_calls) == 1:
            return _tool_call_result("get_weather", {"location": "SF"}, call_id="call_1")
        return _text_result("SF is sunny and 68F.")

    executed = []
    def get_weather(location: str):
        executed.append(location)
        return "sunny, 68F"

    with patch("tool_use.llm_infer", side_effect=fake_infer):
        result = tool_use.run(
            instructions="be helpful",
            tools=[{"type": "function", "name": "get_weather"}],
            tool_executors={"get_weather": get_weather},
            initial_items=[{"role": "user", "content": "weather in SF?"}],
        )

    assert result.ok
    assert result.text == "SF is sunny and 68F."
    assert executed == ["SF"]
    assert len(infer_calls) == 2

    # new_items should contain the function_call, function_call_output,
    # and the final assistant reply — IN THAT ORDER.
    types = [
        item.get("type") or item.get("role")
        for item in result.new_items
    ]
    assert types == ["function_call", "function_call_output", "assistant"]

    assert result.new_items[0]["call_id"] == "call_1"
    assert result.new_items[0]["name"] == "get_weather"
    assert result.new_items[1]["call_id"] == "call_1"
    assert result.new_items[1]["output"] == "sunny, 68F"
    assert result.new_items[2]["content"] == "SF is sunny and 68F."


def test_second_llm_call_receives_history_plus_tool_exchange():
    """The second infer() call must include initial_items + echoed
    function_call + function_call_output in its input_items."""
    tool_use = _reload_tool_use()

    captured = []
    def fake_infer(**kwargs):
        captured.append(kwargs)
        if len(captured) == 1:
            return _tool_call_result("t", {}, call_id="cid")
        return _text_result("done")

    with patch("tool_use.llm_infer", side_effect=fake_infer):
        tool_use.run(
            instructions="",
            tools=[{"type": "function", "name": "t"}],
            tool_executors={"t": lambda: "ok"},
            initial_items=[{"role": "user", "content": "go"}],
        )

    second = captured[1]
    items = second["input_items"]
    assert items[0] == {"role": "user", "content": "go"}
    assert items[1]["type"] == "function_call"
    assert items[1]["call_id"] == "cid"
    assert items[2]["type"] == "function_call_output"
    assert items[2]["output"] == "ok"


def test_unknown_tool_returns_error_to_llm_not_crash():
    tool_use = _reload_tool_use()

    def fake_infer(**kwargs):
        if not hasattr(fake_infer, "count"):
            fake_infer.count = 0
        fake_infer.count += 1
        if fake_infer.count == 1:
            return _tool_call_result("nope", {}, call_id="c1")
        return _text_result("oh well")

    with patch("tool_use.llm_infer", side_effect=fake_infer):
        result = tool_use.run(
            instructions="",
            tools=[{"type": "function", "name": "nope"}],
            tool_executors={"known": lambda: "x"},
            initial_items=[{"role": "user", "content": "hi"}],
        )

    # Loop survives: the unknown-tool error is fed BACK to the model
    # as the function_call_output, and the model's next text becomes
    # the final reply.
    assert result.ok
    assert result.text == "oh well"
    assert any(
        "unknown tool" in (item.get("output") or "").lower()
        for item in result.new_items
        if item.get("type") == "function_call_output"
    )


def test_tool_executor_exception_feeds_error_back_to_llm():
    tool_use = _reload_tool_use()

    def fake_infer(**kwargs):
        if not hasattr(fake_infer, "n"):
            fake_infer.n = 0
        fake_infer.n += 1
        if fake_infer.n == 1:
            return _tool_call_result("boom", {}, call_id="c1")
        return _text_result("handled")

    def boom():
        raise RuntimeError("tool crashed")

    with patch("tool_use.llm_infer", side_effect=fake_infer):
        result = tool_use.run(
            instructions="",
            tools=[{"type": "function", "name": "boom"}],
            tool_executors={"boom": boom},
            initial_items=[{"role": "user", "content": "go"}],
        )

    assert result.ok
    assert result.text == "handled"
    # The error message must land in function_call_output so the model
    # sees what went wrong.
    outputs = [
        item["output"] for item in result.new_items
        if item.get("type") == "function_call_output"
    ]
    assert outputs and "tool crashed" in outputs[0]


def test_max_iterations_guard_returns_error():
    """If the model keeps calling tools forever, the loop must break
    after max_iters and return an error result."""
    tool_use = _reload_tool_use()

    def always_tool(**kwargs):
        # Cycle call_ids so the loop doesn't stall on dedup (if we add any).
        return _tool_call_result("t", {}, call_id=f"c{fake_infer.n}")
    fake_infer = always_tool
    fake_infer.n = 0

    def wrapped(**kwargs):
        fake_infer.n += 1
        return always_tool(**kwargs)

    with patch("tool_use.llm_infer", side_effect=wrapped):
        result = tool_use.run(
            instructions="",
            tools=[{"type": "function", "name": "t"}],
            tool_executors={"t": lambda: "ok"},
            initial_items=[{"role": "user", "content": "go"}],
            max_iters=3,
        )

    assert not result.ok
    assert "max" in (result.error or "").lower()


def test_infer_error_propagates():
    tool_use = _reload_tool_use()
    from llm import InferResult
    bad = InferResult(returncode=500, error="network down")

    with patch("tool_use.llm_infer", return_value=bad):
        result = tool_use.run(
            instructions="",
            tools=[],
            tool_executors={},
            initial_items=[{"role": "user", "content": "x"}],
        )

    assert not result.ok
    assert "network" in (result.error or "").lower()
