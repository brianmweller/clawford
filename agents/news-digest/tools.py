"""agents/news-digest/tools.py — Lowly Worm's tool manifest.

Read-only tools that expose the news digest state the operator accumulates
each morning. All tools are cache-only: they read files written by
the scheduled crons (morning-edition.py, deliver-digest.py,
engagement-poller.py, update-preferences.py) and return structured
data. No network, no LLM, no subprocess.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

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
]


EXECUTORS: dict = {
    "get_todays_digest": get_todays_digest,
    "get_topic_weights": get_topic_weights,
    "recent_engagements": recent_engagements,
}
