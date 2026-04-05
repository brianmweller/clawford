#!/usr/bin/env python3
"""
log-engagement.py — Log a user engagement event to engagement.jsonl.

Usage: python3 log-engagement.py <item_number> <action>
  action: thumbs_up or thumbs_down

Reads today's item-map to find article metadata, appends to engagement.jsonl.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.path.expanduser("~/.openclaw/news-digest-workspace"))
CACHE_DIR = WORKSPACE / "cache"
ENGAGEMENT_FILE = WORKSPACE / "preferences" / "engagement.jsonl"


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"status": "error", "message": "usage: log-engagement.py <item_number> <action>"}))
        sys.exit(1)

    item_num = sys.argv[1]
    action = sys.argv[2]  # thumbs_up or thumbs_down

    if action not in ("thumbs_up", "thumbs_down"):
        print(json.dumps({"status": "error", "message": f"invalid action: {action}. Use thumbs_up or thumbs_down"}))
        sys.exit(1)

    # Find today's item mapping
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    map_file = CACHE_DIR / f"item-map-{today}.json"

    if not map_file.exists():
        print(json.dumps({"status": "error", "message": f"no item mapping for {today}"}))
        sys.exit(1)

    with open(map_file) as f:
        item_map = json.load(f)

    article = item_map.get(item_num)
    if not article:
        print(json.dumps({"status": "error", "message": f"item {item_num} not found in today's digest"}))
        sys.exit(1)

    # Build engagement event
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "article_id": article.get("id", ""),
        "action": action,
        "title": article.get("title", ""),
        "topics": article.get("topics", []),
        "source": article.get("source", ""),
    }

    # Append to engagement log
    with open(ENGAGEMENT_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")

    print(json.dumps({"status": "ok", "action": action, "title": article.get("title", "")[:80]}))


if __name__ == "__main__":
    main()
