#!/usr/bin/env python3
"""
engagement-poller.py — Extract engagement signals from agent session transcripts.

Reads OpenClaw session files for the news-digest agent, finds /like N and
/dislike N messages (including callback button taps), looks up articles in
the item-map, and appends to engagement.jsonl.

Runs every 5 minutes via cron. Tracks which sessions/lines have been
processed to avoid duplicates.

Usage: python3 engagement-poller.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.path.expanduser("~/.clawford/news-digest-workspace"))
SESSIONS_DIR = Path(os.path.expanduser("~/.clawford/agents/news-digest/sessions"))
CACHE_DIR = WORKSPACE / "cache"
ENGAGEMENT_FILE = WORKSPACE / "preferences" / "engagement.jsonl"
STATE_FILE = CACHE_DIR / "poller-state.json"


def load_state():
    """Load processed session/line tracking state."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"processed": {}}


def save_state(state):
    """Save processing state."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_item_map():
    """Load today's item-number-to-article mapping."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    map_file = CACHE_DIR / f"item-map-{today}.json"
    if map_file.exists():
        with open(map_file) as f:
            return json.load(f)
    return {}


def extract_engagement(text):
    """Parse a message for /like N, /dislike N, or /more N.

    Returns (action, item_num) on first match, where action is one of
    `thumbs_up`, `thumbs_down`, or `expand`. Returns None if no engagement
    pattern is found.

    Each verb has three accepted forms so we cover every way the signal
    can arrive in a session transcript:

      1. `/like 3` — slash command typed manually
      2. `like:3`  — inline keyboard button callback_data (openclaw
                     forwards callback_data into the transcript as text)
      3. `like 3`  — freeform prose where the user wrote the verb
                     and number without a slash or colon

    Order is load-bearing: check `dislike` BEFORE `like` so the trailing
    "like" inside "dislike" doesn't misclassify a negative signal as
    positive. (Pre-2026-04-13 bug: the old implementation matched
    `like` as a bare substring and turned every `/dislike N` into a
    `thumbs_up`.)

    Item number must be at least one digit and follow either a space,
    a colon, or end-of-prefix whitespace — guards against picking up
    item numbers from unrelated text ("in 2025 I liked x"). The verb
    must be at a word boundary so `unlike`, `childlike`, `dislikes`
    don't trigger.
    """
    # (verb_regex, result_action). Each regex matches the verb at a word
    # boundary followed by a space, colon, or explicit underscore, then
    # captures the item number. We scan for ALL matches across all verbs
    # and return the one at the leftmost position, so if two signals
    # happen to appear in the same transcript line the one the user
    # wrote first wins.
    patterns: list[tuple[str, str]] = [
        (r"(?<![a-z])/?dislike[:_\s]\s*(\d+)", "thumbs_down"),
        (r"(?<![a-z])/?like[:_\s]\s*(\d+)",    "thumbs_up"),
        (r"(?<![a-z])/?more[:_\s]\s*(\d+)",    "expand"),
    ]
    best: tuple[int, str, str] | None = None  # (start_pos, action, item_num)
    for pattern, action in patterns:
        m = re.search(pattern, text, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), action, m.group(1))
    if best is None:
        return None
    return (best[1], best[2])


def main():
    if not SESSIONS_DIR.exists():
        print(json.dumps({"status": "ok", "processed": 0, "message": "no sessions directory"}))
        return

    state = load_state()
    processed = state.get("processed", {})
    item_map = load_item_map()
    logged = 0

    # Scan all session files
    for session_file in SESSIONS_DIR.glob("*.jsonl"):
        fname = session_file.name
        last_line = processed.get(fname, 0)

        with open(session_file) as f:
            lines = f.readlines()

        new_lines = lines[last_line:]
        for i, line in enumerate(new_lines, start=last_line):
            try:
                obj = json.loads(line)
                if obj.get("type") != "message":
                    continue

                msg = obj.get("message", {})
                role = msg.get("role", "")
                if role != "user":
                    continue

                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )

                if not isinstance(content, str):
                    continue

                result = extract_engagement(content)
                if not result:
                    continue

                action, item_num = result
                article = item_map.get(item_num)

                if article:
                    event = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "article_id": article.get("id", ""),
                        "action": action,
                        "title": article.get("title", ""),
                        "topics": article.get("topics", []),
                        "source": article.get("source", ""),
                    }
                    with open(ENGAGEMENT_FILE, "a") as ef:
                        ef.write(json.dumps(event) + "\n")
                    logged += 1
                    print(f"  Logged {action} for item {item_num}: {article.get('title', '')[:60]}", file=sys.stderr)
                else:
                    print(f"  Item {item_num} not found in today's item-map", file=sys.stderr)

            except (json.JSONDecodeError, KeyError):
                continue

        # Update processed line count for this session
        processed[fname] = len(lines)

    # Save state
    state["processed"] = processed
    save_state(state)

    print(json.dumps({"status": "ok", "logged": logged}))


if __name__ == "__main__":
    main()
