"""agents/news-digest/tools.py — Lowly Worm's tool manifest.

Read-only tools that expose the news digest state the operator accumulates
each morning. Most tools are cache-only: they read files written by
the scheduled crons (morning-edition.py, deliver-digest.py,
engagement-poller.py, update-preferences.py) and return structured
data.

Exception: ``ask_topic`` shells out to scripts/on-demand.py to fulfill
`/ask [topic]` queries. That's the single tool that touches the network
(via the script's RSS + Google News + Brave fetches).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import memory_writer  # type: ignore
import state_introspection  # type: ignore
import subprocess_helpers  # type: ignore
from confirm_pending_tool import (  # type: ignore
    TOOL_SCHEMA as _CONFIRM_PENDING_SCHEMA,
    confirm_pending as _confirm_pending_impl,
)

AGENT_ID = "news-digest"

WORKSPACE = os.path.expanduser("~/.clawford/news-digest-workspace")
CACHE = os.path.join(WORKSPACE, "cache")
PREFS = os.path.join(WORKSPACE, "preferences")

MORNING_ITEMS_PATH = os.path.join(CACHE, "morning-items.json")
MORNING_BRIEF_PATH = os.path.join(CACHE, "morning-brief-ready.txt")
MODEL_PATH = os.path.join(PREFS, "model.json")
ENGAGEMENT_PATH = os.path.join(PREFS, "engagement.jsonl")


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def get_todays_digest() -> dict:
    """Return the most recent morning digest: up to the first 20 ranked
    items with title, source, category, topics. Falls back to the
    formatted brief if the structured items file is missing."""
    items = _read_json(MORNING_ITEMS_PATH, default=[])
    if items:
        return {
            "count": len(items),
            "items": [
                {
                    "num": it.get("num"),
                    "title": it.get("title") or it.get("headline"),
                    "category": it.get("category"),
                    "source": it.get("source"),
                    "topics": it.get("topics", []),
                    "url": it.get("url"),
                }
                for it in items[:20]
            ],
        }
    brief = _read_text(MORNING_BRIEF_PATH)
    if brief:
        return {"format": "text", "brief": brief[:4000]}
    return {"error": "no digest available yet"}


def get_topic_weights() -> dict:
    """Return the current learned topic weights — the preference model
    that ranks each morning's digest. Higher weight = more interesting
    to the operator. Updated nightly by engagement-poller + update-preferences."""
    model = _read_json(MODEL_PATH, default=None)
    if not model:
        return {"error": "no topic model found"}
    weights = model.get("topic_weights", {})
    sorted_weights = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "version": model.get("version"),
        "updated_at": model.get("updated_at"),
        "top_topics": sorted_weights[:20],
    }


def recent_engagements(limit: int = 15) -> dict:
    """Return the most recent N engagement events the operator has logged —
    thumbs up / thumbs down / more on past articles. Useful when he
    asks 'what have I been reading lately' or 'what did I like most
    this week'."""
    if not os.path.exists(ENGAGEMENT_PATH):
        return {"count": 0, "engagements": []}
    lines = []
    try:
        with open(ENGAGEMENT_PATH, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {"error": "engagement log unreadable"}
    lines = lines[-limit:]
    return {
        "count": len(lines),
        "engagements": [
            {
                "ts": ln.get("ts"),
                "action": ln.get("action"),
                "title": ln.get("title"),
                "topics": ln.get("topics", []),
                "source": ln.get("source"),
            }
            for ln in lines
        ],
    }


# ---------------------------------------------------------------------------
# Phase C producer tools
# ---------------------------------------------------------------------------


def propose_remember(rule: str, category: str = "General") -> dict:
    return memory_writer.propose_pending_remember(AGENT_ID, rule, category)


def confirm_remember(rule: str, category: str) -> dict:
    return memory_writer.append_rule(AGENT_ID, rule, category)


def record_engagement(article_id: str, action: str) -> dict:
    """Record a user engagement event (thumbs_up, thumbs_down, more)
    for an article. Appends to engagement.jsonl. The dispatcher's
    callback shortcut routes like:N/dislike:N/more:N here directly."""
    valid_actions = {"thumbs_up", "thumbs_down", "more"}
    if action not in valid_actions:
        return {"status": "error", "error": f"invalid action: {action}. Use: {valid_actions}"}

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "article_id": str(article_id),
        "action": action,
    }

    # Try to enrich with title/topics from morning items cache
    items = _read_json(MORNING_ITEMS_PATH, default=[])
    if isinstance(items, list):
        try:
            idx = int(article_id) - 1
            if 0 <= idx < len(items):
                entry["title"] = items[idx].get("title") or items[idx].get("headline", "")
                entry["topics"] = items[idx].get("topics", [])
                entry["source"] = items[idx].get("source", "")
        except (ValueError, IndexError):
            pass

    os.makedirs(os.path.dirname(ENGAGEMENT_PATH), exist_ok=True)
    with open(ENGAGEMENT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {"status": "ok", "article_id": article_id, "action": action}


_ON_DEMAND = str(
    Path(__file__).resolve().parent / "scripts" / "on-demand.py"
)


def ask_topic(topic: str) -> dict:
    """Answer a current-events query by shelling out to on-demand.py, which
    searches today's RSS cache + Google News RSS + Brave Search in parallel
    and returns ranked results. The LLM synthesizes a briefing from the
    results."""
    topic = (topic or "").strip()
    if not topic:
        return {"status": "error", "error": "empty topic"}
    result = subprocess_helpers.run_json_script(_ON_DEMAND, topic, timeout=30)
    if subprocess_helpers.is_subprocess_error(result):
        return {"status": "error", "error": result.get("__error__", "script error")}
    return result


TOOLS: list[dict] = [
    {
        "type": "function",
        "name": "get_todays_digest",
        "description": (
            "Return today's ranked news + LinkedIn digest (up to 20 "
            "items) with titles, categories, topics, and URLs. Call "
            "this when the operator asks about today's news, what's in the "
            "morning digest, or what Lowly Worm has for him."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_topic_weights",
        "description": (
            "Return the learned topic-weight preference model — which "
            "topics the operator scores as most interesting, based on past "
            "thumbs-up/down engagement. Call this when he asks what "
            "you've learned he likes or wants to see the ranking."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "recent_engagements",
        "description": (
            "Return the most recent engagement events (thumbs up / down / "
            "more) the operator has logged against articles. Good for 'what "
            "have I been reading lately' queries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many recent events to return. Default 15, max 50."
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "record_engagement",
        "description": (
            "Record a thumbs-up, thumbs-down, or 'more' reaction on a "
            "digest article. Pass the article number (1-based) from "
            "today's digest and the action. Also called automatically "
            "when the operator taps inline buttons on the morning digest."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "article_id": {"type": "string", "description": "Article number from the digest"},
                "action": {
                    "type": "string",
                    "enum": ["thumbs_up", "thumbs_down", "more"],
                    "description": "Engagement type",
                },
            },
            "required": ["article_id", "action"],
        },
    },
    {
        "type": "function",
        "name": "propose_remember",
        "description": (
            "Stage a rule for the operator's confirmation, to be added to your "
            "persistent MEMORY.md. the operator will see inline buttons to remember "
            "or skip. Use when the operator says 'remember that...', 'from now on...', "
            "or teaches you a new rule. Category is a short heading like "
            "'Grocery Defaults' or 'Alert Classification'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The rule to remember"},
                "category": {"type": "string", "description": "Category heading"},
            },
            "required": ["rule"],
        },
    },
    {
        "type": "function",
        "name": "ask_topic",
        "description": (
            "Search current news for a topic and return ranked results. "
            "Shells out to on-demand.py which queries today's RSS cache, "
            "Google News RSS, and Brave Search in parallel. Use for `/ask "
            "[topic]` or when the operator asks 'what's happening with X'. Then "
            "synthesize a 3-5 sentence briefing with source attribution."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The topic or free-text question",
                },
            },
            "required": ["topic"],
        },
    },
    {
        "type": "function",
        "name": "get_recent_runs",
        "description": (
            "Return your own recent cron-run activity in the operator's "
            "timezone (PT). Use this BEFORE answering questions like "
            "'did you run today?', 'what did you do?', 'why didn't "
            "you X?'. Returns per-run status + summary counts. "
            "Default window is 24h."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since_hours": {"type": "integer", "default": 24},
            },
            "required": [],
        },
    },
    _CONFIRM_PENDING_SCHEMA,
]


def get_recent_runs(since_hours: int = 24) -> dict:
    return state_introspection.recent_runs(AGENT_ID, since_hours=since_hours)


def confirm_pending(action_id: str, reason: str) -> dict:
    return _confirm_pending_impl(
        action_id=action_id, reason=reason,
        agent_id=AGENT_ID, executors=EXECUTORS,
    )


EXECUTORS: dict = {
    "get_todays_digest": get_todays_digest,
    "get_topic_weights": get_topic_weights,
    "recent_engagements": recent_engagements,
    "record_engagement": record_engagement,
    "propose_remember": propose_remember,
    "confirm_remember": confirm_remember,
    "ask_topic": ask_topic,
    "get_recent_runs": get_recent_runs,
    "confirm_pending": confirm_pending,
}
