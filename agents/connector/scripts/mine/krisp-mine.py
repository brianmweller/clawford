#!/usr/bin/env python3
"""
krisp-mine.py — Mine Krisp transcripts for meeting participants.

Fetches all transcripts via MCP and extracts participant names, meeting
topics, and speaker frequency. Names only (no emails) — the aggregator
does fuzzy matching against email-based contacts.

Usage:
  python3 krisp-mine.py

Output: cache/mined-krisp.json
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import load_config, save_mined

DEFAULT_TOKEN_DIR = os.path.expanduser(
    "~/.openclaw/meetings-coach-workspace/cache/krisp-tokens"
)

KRISP_MCP_URL = "https://mcp.krisp.ai/mcp"
KRISP_TOKEN_ENDPOINT = "https://api.krisp.ai/platform/v1/oauth2/token"


def find_token_dir():
    config = load_config()
    custom = config.get("krisp", {}).get("token_dir")
    if custom and os.path.exists(custom):
        return custom
    if os.path.exists(DEFAULT_TOKEN_DIR):
        return DEFAULT_TOKEN_DIR
    # Also check Flux location
    flux_path = os.path.expanduser("~/Dropbox/Startup/Flux/data/krisp_tokens")
    if os.path.exists(flux_path):
        return flux_path
    return None


def load_krisp_token(token_dir):
    path = os.path.join(token_dir, "tokens.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return data.get("access_token")


def load_client_info(token_dir):
    path = os.path.join(token_dir, "client_info.json")
    if not os.path.exists(path):
        return None, None
    with open(path) as f:
        data = json.load(f)
    return data.get("client_id"), data.get("client_secret")


def refresh_token(token_dir):
    """Refresh the Krisp access token."""
    tokens_path = os.path.join(token_dir, "tokens.json")
    with open(tokens_path) as f:
        tokens = json.load(f)

    client_id, client_secret = load_client_info(token_dir)
    if not client_id:
        return None

    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.get("refresh_token"),
        "client_id": client_id,
        "client_secret": client_secret,
    }

    payload = "&".join(f"{k}={v}" for k, v in data.items()).encode()
    req = urllib.request.Request(
        KRISP_TOKEN_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        new_tokens = json.loads(resp.read())
        tokens["access_token"] = new_tokens["access_token"]
        if "refresh_token" in new_tokens:
            tokens["refresh_token"] = new_tokens["refresh_token"]
        with open(tokens_path, "w") as f:
            json.dump(tokens, f, indent=2)
        return new_tokens["access_token"]
    except Exception as e:
        print(f"Krisp token refresh failed: {e}", file=sys.stderr)
        return None


def call_krisp_mcp(access_token, tool_name, arguments):
    """Call a Krisp MCP tool."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
        "id": 1,
    }).encode()

    req = urllib.request.Request(
        KRISP_MCP_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )

    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


def mine_krisp():
    config = load_config()
    lookback = config.get("lookback_years", 2)

    token_dir = find_token_dir()
    if not token_dir:
        print("WARNING: No Krisp tokens found. Skipping.", file=sys.stderr)
        save_mined("krisp", {
            "status": "skipped",
            "reason": "No Krisp tokens found",
            "contacts": {},
        })
        return

    access_token = load_krisp_token(token_dir)
    if not access_token:
        access_token = refresh_token(token_dir)
    if not access_token:
        print("ERROR: Could not get Krisp access token.", file=sys.stderr)
        sys.exit(1)

    print("Mining Krisp transcripts...", file=sys.stderr)

    contacts = defaultdict(lambda: {
        "name": "",
        "meeting_count": 0,
        "first_meeting": None,
        "last_meeting": None,
        "meeting_titles": [],
    })

    after_date = (datetime.now(timezone.utc) - timedelta(days=lookback * 365)).strftime("%Y-%m-%d")
    offset = 0
    limit = 50
    total = 0

    while True:
        try:
            result = call_krisp_mcp(access_token, "search_meetings", {
                "after": after_date,
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            # Try token refresh
            print(f"MCP call failed: {e}. Refreshing token...", file=sys.stderr)
            access_token = refresh_token(token_dir)
            if not access_token:
                break
            try:
                result = call_krisp_mcp(access_token, "search_meetings", {
                    "after": after_date,
                    "limit": limit,
                    "offset": offset,
                })
            except Exception as e2:
                print(f"MCP call failed after refresh: {e2}", file=sys.stderr)
                break

        # Parse MCP response
        content = result.get("result", {}).get("content", [])
        if not content:
            break

        meetings_found = 0
        for block in content:
            if block.get("type") != "text":
                continue
            try:
                meetings = json.loads(block["text"])
                if isinstance(meetings, dict):
                    meetings = [meetings]
            except json.JSONDecodeError:
                continue

            for meeting in meetings:
                if not isinstance(meeting, dict):
                    continue
                total += 1
                meetings_found += 1

                title = meeting.get("name", meeting.get("title", ""))
                date = meeting.get("date", "")[:10]
                participants = list(dict.fromkeys(
                    meeting.get("speakers", []) + meeting.get("attendees", [])
                ))

                for name in participants:
                    name = name.strip()
                    if not name or len(name) < 2:
                        continue
                    key = name.lower()
                    c = contacts[key]
                    c["name"] = name  # Keep original case
                    c["meeting_count"] += 1
                    if date:
                        if not c["first_meeting"] or date < c["first_meeting"]:
                            c["first_meeting"] = date
                        if not c["last_meeting"] or date > c["last_meeting"]:
                            c["last_meeting"] = date
                    if title and len(c["meeting_titles"]) < 20:
                        c["meeting_titles"].append(title)

        if meetings_found == 0:
            break

        offset += limit
        print(f"  {total} transcripts, {len(contacts)} participants", file=sys.stderr)
        time.sleep(1.0)

    output = {k: dict(v) for k, v in contacts.items()}

    result = {
        "status": "ok",
        "source": "krisp",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "transcripts_scanned": total,
        "contacts_found": len(output),
        "contacts": output,
    }

    save_mined("krisp", result)
    print(f"\nDone. Scanned {total} transcripts, found {len(output)} participants.", file=sys.stderr)


if __name__ == "__main__":
    mine_krisp()
