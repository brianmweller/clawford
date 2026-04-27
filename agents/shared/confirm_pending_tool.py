"""agents/shared/confirm_pending_tool.py — text-based approval tool.

Inline buttons remain the primary approval path. This shared tool is
the natural-language fallback so when the operator replies "yes" / "do it" /
"I approve" instead of tapping the Confirm button, the LLM can route
that approval to the same execution path.

Both this tool and the dispatcher's _handle_confirm flow through
`pending_actions.execute()`, so semantics are identical regardless of
which surface the operator uses.

Each agent registers a thin closure over its AGENT_ID and EXECUTORS
dict in its own tools.py — see the integration in
agents/<agent>/tools.py for the wiring pattern.
"""
from __future__ import annotations

import pending_actions  # type: ignore


TOOL_SCHEMA: dict = {
    "type": "function",
    "name": "confirm_pending",
    "description": (
        "Confirm and execute a pending action when the operator has approved "
        "it in natural language (e.g. 'yes', 'do it', 'I approve', "
        "'try it'). Inline buttons are the primary approval path; use "
        "this tool only when (a) the operator's text is clearly an approval, "
        "AND (b) the target action is unambiguous in your recent "
        "context. If multiple pending actions exist, ask which one. "
        "Pass the action_id from the prior propose_* tool's "
        "__pending_action__ output, plus a brief reason citing what "
        "the operator said (kept for the audit trail)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action_id": {
                "type": "string",
                "description": (
                    "The action_id from a prior propose_* tool result "
                    "(format: act_<hex>)."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Brief — what the operator said that authorized this. "
                    "Example: 'the operator replied \"do it\"'."
                ),
            },
        },
        "required": ["action_id", "reason"],
        "additionalProperties": False,
    },
}


def confirm_pending(
    action_id: str,
    reason: str,
    *,
    agent_id: str,
    executors: dict,
) -> dict:
    """Look up the pending action by id, then execute it.

    Returns a structured dict the LLM can summarize back to the operator:

      success: {status: 'ok', summary, action_id, reason, ...executor_result}
      missing: {status: 'error', error, action_id}
      raised:  {status: 'error', error, action_id}

    On success the action is removed from the queue (via
    pending_actions.execute). On exception the action is preserved so
    the operator can retry (e.g. "try again").
    """
    action = pending_actions.load_by_id(agent_id, action_id)
    if action is None:
        return {
            "status": "error",
            "error": (
                f"no pending action with id '{action_id}' "
                "(expired, already handled, or wrong id)"
            ),
            "action_id": action_id,
        }
    summary = action.get("summary", action_id)
    result = pending_actions.execute(agent_id, action, executors)

    out: dict = {
        "action_id": action_id,
        "summary": summary,
        "reason": reason,
    }
    out.update(result)
    return out
