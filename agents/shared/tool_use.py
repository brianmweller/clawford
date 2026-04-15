"""agents/shared/tool_use.py — two-turn tool-use loop.

One function: run(). Given instructions, a tool manifest, a dict of
tool executors, and an initial list of input_items (user turn plus
whatever conversation history you want prepended), it:

  1. Calls llm.infer with the items + tools
  2. If the response is text → return it, done
  3. If the response is a function_call → execute the tool, append
     the function_call and function_call_output items to the running
     list, and loop back to step 1
  4. Bounds the loop at max_iters so a runaway tool-picking model
     can't spiral forever

Returns a ToolUseResult with .ok, .text, .new_items, .error. new_items
contains everything the caller should persist to the conversation
window *in addition to* the items they already passed as initial_items
— i.e. the echoed function_calls, function_call_outputs, and the
final assistant text reply.

This module is intentionally sync — the dispatcher wraps it in
asyncio.to_thread() for the async event loop.
"""
from __future__ import annotations

import inspect
import json
import traceback
from dataclasses import dataclass, field

from llm import infer as llm_infer

DEFAULT_MAX_ITERS = 6
DEFAULT_TIMEOUT_S = 90


@dataclass
class ToolUseResult:
    text: str = ""
    new_items: list[dict] = field(default_factory=list)
    error: str | None = None
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None


def _execute_tool(name: str, args: dict, executors: dict) -> str:
    """Run the named tool with the given arguments. Always returns a
    string suitable for function_call_output.output. Catches all
    exceptions and returns their message so the model can see what
    happened."""
    if name not in executors:
        return f"ERROR: unknown tool '{name}'"
    fn = executors[name]
    try:
        sig = inspect.signature(fn)
        # Filter args to just those accepted by the executor's signature.
        # Tools should document what they accept in their manifest; this
        # is a soft safety net for a model that hallucinates an arg.
        accepted = {
            k: v for k, v in (args or {}).items()
            if k in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
        }
        result = fn(**accepted)
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result) if result is not None else ""
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def run(
    *,
    instructions: str,
    tools: list[dict],
    tool_executors: dict,
    initial_items: list[dict],
    max_iters: int = DEFAULT_MAX_ITERS,
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> ToolUseResult:
    """Run the tool-use loop. See module docstring for semantics."""
    new_items: list[dict] = []
    current_items = list(initial_items)

    for iteration in range(max_iters):
        kwargs = {
            "input_items": current_items,
            "tools": tools if tools else None,
            "instructions": instructions,
            "timeout": timeout,
        }
        if model is not None:
            kwargs["model"] = model

        result = llm_infer(**kwargs)

        if not result.ok:
            return ToolUseResult(
                error=result.error or f"infer returned code {result.returncode}",
                returncode=result.returncode or 500,
                new_items=new_items,
            )

        if result.function_call is None:
            # Text terminal state.
            assistant_item = {"role": "assistant", "content": result.text}
            new_items.append(assistant_item)
            return ToolUseResult(text=result.text, new_items=new_items)

        # Tool call — execute, append both halves, continue loop.
        fc = result.function_call
        call_id = fc.get("call_id", "")
        name = fc.get("name", "")
        args = fc.get("arguments") or {}

        # Echo the assistant's function_call as an input item. Arguments
        # must be a JSON STRING in this shape, not a dict — confirmed
        # empirically against the codex backend.
        echoed = {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(args),
        }
        output_str = _execute_tool(name, args, tool_executors)
        tool_output_item = {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output_str,
        }

        new_items.append(echoed)
        new_items.append(tool_output_item)

        current_items = current_items + [echoed, tool_output_item]

    return ToolUseResult(
        error=f"tool-use loop exceeded max_iters={max_iters}",
        returncode=408,
        new_items=new_items,
    )
