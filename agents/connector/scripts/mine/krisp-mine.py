#!/usr/bin/env python3
"""
krisp-mine.py — Mine Krisp transcripts for meeting participants and content.

Uses the same MCP protocol as Sergeant Murphy's transcript-scan.py:
MCP session init → search_meetings → parse SSE responses.

Usage:
  python3 krisp-mine.py

Output: cache/mined-krisp.json
"""

import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, load_config, save_mined

KRISP_MCP_URL = "https://mcp.krisp.ai/mcp"
KRISP_TOKEN_ENDPOINT = "https://api.krisp.ai/platform/v1/oauth2/token"

DEFAULT_TOKEN_DIR = os.path.join(str(CACHE_DIR), "krisp-tokens")


def find_token_dir():
    config = load_config()
    custom = config.get("krisp", {}).get("token_dir")
    if custom and os.path.exists(custom):
        return custom
    if os.path.exists(DEFAULT_TOKEN_DIR):
        return DEFAULT_TOKEN_DIR
    flux_path = os.path.expanduser("~/Dropbox/Startup/Flux/data/krisp_tokens")
    if os.path.exists(flux_path):
        return flux_path
    return None


def load_krisp_token(token_dir):
    path = os.path.join(token_dir, "tokens.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f).get("access_token")


def load_client_info(token_dir):
    path = os.path.join(token_dir, "client_info.json")
    if not os.path.exists(path):
        return None, None
    with open(path) as f:
        data = json.load(f)
    return data.get("client_id"), data.get("client_secret")


# ── SSE parsing (from Murphy's transcript-scan.py) ───────────

def _parse_sse_results(sse_text):
    """Parse Server-Sent Events format and return MCP content items."""
    for line in sse_text.splitlines():
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if not data_str:
                continue
            try:
                data = json.loads(data_str)
                result = data.get("result", {})
                if "content" in result:
                    return result["content"]
            except json.JSONDecodeError:
                continue
    return []


def _extract_meetings_from_sse(sse_text):
    """Parse search_meetings SSE response into meeting dicts.
    Follows Murphy's exact parsing logic."""
    content = _parse_sse_results(sse_text)

    # Check for structuredContent first (clean JSON)
    for line in sse_text.splitlines():
        if line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
                structured = data.get("result", {}).get("structuredContent", {})
                if "meetings" in structured:
                    return structured["meetings"]
            except (json.JSONDecodeError, KeyError):
                continue

    # Fall back to markdown text parsing
    meetings = []
    for item in content:
        if item.get("type") != "text":
            continue
        text = item.get("text", "").strip()
        if text.startswith("Found ") and "meeting" in text:
            continue

        if "meeting_id:" in text:
            meeting = {}
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("## "):
                    title_part = line[3:].strip()
                    paren_match = re.search(r"\((\d{4}-\d{2}-\d{2}T[^)]+)\)\s*$", title_part)
                    if paren_match:
                        meeting["date"] = paren_match.group(1)
                        meeting["name"] = title_part[:paren_match.start()].strip()
                    else:
                        meeting["name"] = title_part
                elif line.startswith("meeting_id:"):
                    meeting["meeting_id"] = line.split(":", 1)[1].strip()
                elif line.startswith("speakers:"):
                    meeting["speakers"] = [s.strip() for s in line.split(":", 1)[1].split(",") if s.strip()]
                elif line.startswith("attendees:"):
                    meeting["attendees"] = [a.strip() for a in line.split(":", 1)[1].split(",") if a.strip()]

            if meeting.get("meeting_id"):
                meetings.append(meeting)

    return meetings


# ── MCP session (from Murphy's transcript-scan.py) ───────────

async def fetch_all_meetings(token_dir):
    """Fetch all meetings using Murphy's MCP session pattern."""
    try:
        import httpx
    except ImportError:
        print("ERROR: pip install httpx", file=sys.stderr)
        return [], "httpx not installed"

    token = load_krisp_token(token_dir)
    if not token:
        return [], "No Krisp access token"

    config = load_config()
    lookback = config.get("lookback_years", 2)
    since = (datetime.now(timezone.utc) - timedelta(days=lookback * 365)).strftime("%Y-%m-%d")

    def build_headers(tok):
        return {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tok}",
            "User-Agent": "Mozilla/5.0",
        }

    headers = build_headers(token)
    meetings = []
    error = None

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
    ) as client:
        try:
            # Step 1: Initialize MCP session
            init_payload = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "huckle", "version": "1.0"},
                },
            }
            init_resp = await client.post(KRISP_MCP_URL, headers=headers, json=init_payload)
            init_resp.raise_for_status()

            session_id = init_resp.headers.get("mcp-session-id") or init_resp.headers.get("Mcp-Session-Id")
            session_headers = dict(headers)
            if session_id:
                session_headers["Mcp-Session-Id"] = session_id

            # Step 2: Send initialized notification
            await client.post(KRISP_MCP_URL, headers=session_headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
            })

            # Step 3: Search all meetings with pagination
            offset = 0
            next_id = 2
            while True:
                search_payload = {
                    "jsonrpc": "2.0", "id": next_id,
                    "method": "tools/call",
                    "params": {
                        "name": "search_meetings",
                        "arguments": {
                            "after": since, "limit": 50, "offset": offset,
                            "fields": ["name", "date", "speakers", "attendees", "key_points", "action_items"],
                        },
                    },
                }
                next_id += 1
                resp = await client.post(KRISP_MCP_URL, headers=session_headers, json=search_payload)
                resp.raise_for_status()

                batch = _extract_meetings_from_sse(resp.text)
                if not batch:
                    break
                meetings.extend(batch)
                print(f"  {len(meetings)} meetings fetched...", file=sys.stderr)
                if len(batch) < 50:
                    break
                offset += len(batch)

        except Exception as e:
            error = str(e)

    return meetings, error


def mine_krisp():
    token_dir = find_token_dir()
    if not token_dir:
        print("WARNING: No Krisp tokens found. Skipping.", file=sys.stderr)
        save_mined("krisp", {"status": "skipped", "reason": "No tokens", "contacts": {}})
        return

    print("Mining Krisp transcripts (MCP session)...", file=sys.stderr)
    meetings, error = asyncio.run(fetch_all_meetings(token_dir))

    if error:
        print(f"  Error: {error}", file=sys.stderr)

    print(f"  Total meetings: {len(meetings)}", file=sys.stderr)

    # Aggregate per participant
    contacts = defaultdict(lambda: {
        "name": "", "meeting_count": 0,
        "first_meeting": None, "last_meeting": None,
        "meeting_titles": [], "meeting_dates": [],
    })

    for meeting in meetings:
        title = meeting.get("name", "")
        date = (meeting.get("date") or "")[:10]
        participants = list(dict.fromkeys(
            meeting.get("speakers", []) + meeting.get("attendees", [])
        ))

        for name in participants:
            name = name.strip()
            if not name or len(name) < 2:
                continue
            if name.lower() in ("Sam Smith", "Sam"):
                continue
            key = name.lower()
            c = contacts[key]
            c["name"] = name
            c["meeting_count"] += 1
            if date:
                c["meeting_dates"].append(date)
                if not c["first_meeting"] or date < c["first_meeting"]:
                    c["first_meeting"] = date
                if not c["last_meeting"] or date > c["last_meeting"]:
                    c["last_meeting"] = date
            if title and len(c["meeting_titles"]) < 20:
                c["meeting_titles"].append(title)

    output = {k: dict(v) for k, v in contacts.items()}

    result = {
        "status": "ok",
        "source": "krisp",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "transcripts_scanned": len(meetings),
        "contacts_found": len(output),
        "contacts": output,
        "meetings": meetings,  # Keep raw meetings for cross-referencing
    }

    save_mined("krisp", result)

    top = sorted(output.values(), key=lambda c: -c["meeting_count"])[:15]
    for c in top:
        print(f"  {c['name']:25s} {c['meeting_count']:3d} meetings", file=sys.stderr)

    print(f"\nDone. {len(output)} participants from {len(meetings)} meetings.", file=sys.stderr)


if __name__ == "__main__":
    mine_krisp()
