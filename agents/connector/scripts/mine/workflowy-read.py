#!/usr/bin/env python3
"""
workflowy-read.py — Read Murphy's contact name resolution cache.

The Workflowy sync script (meetings-coach) resolves email addresses to
display names via Gmail lookups and caches them. We simply read this
cache — no API call needed.

Usage:
  python3 workflowy-read.py

Output: cache/mined-workflowy.json
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import load_config, save_mined

DEFAULT_CACHE_PATHS = [
    os.path.expanduser("~/.openclaw/meetings-coach-workspace/cache/contact-names.json"),
]


def mine_workflowy():
    config = load_config()
    custom = config.get("workflowy", {}).get("contact_cache_path")

    cache_path = None
    if custom and os.path.exists(custom):
        cache_path = custom
    else:
        for path in DEFAULT_CACHE_PATHS:
            if os.path.exists(path):
                cache_path = path
                break

    if not cache_path:
        print("WARNING: No Workflowy contact cache found. Skipping.", file=sys.stderr)
        save_mined("workflowy", {
            "status": "skipped",
            "reason": "No contact-names.json found",
            "contacts": {},
        })
        return

    print(f"Reading: {cache_path}", file=sys.stderr)

    with open(cache_path) as f:
        cache = json.load(f)

    # Convert to standard mining output format
    contacts = {}
    for email, data in cache.items():
        if not email or "@" not in email:
            continue
        contacts[email.lower()] = {
            "email": email.lower(),
            "display_names": [data["name"]] if data.get("name") else [],
            "resolved_at": data.get("checked_at"),
        }

    result = {
        "status": "ok",
        "source": "workflowy",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "contacts_found": len(contacts),
        "contacts": contacts,
    }

    save_mined("workflowy", result)
    print(f"Done. {len(contacts)} name resolutions.", file=sys.stderr)


if __name__ == "__main__":
    mine_workflowy()
