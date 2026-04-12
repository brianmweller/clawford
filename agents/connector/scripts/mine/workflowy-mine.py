#!/usr/bin/env python3
"""
workflowy-mine.py — Mine Workflowy for meeting attendees, topics, and notes.

Fetches the full node export via Workflowy API. Parses the meeting tree
structure (Year > Month > Date > Meeting) to extract attendee names,
agenda topics, notes, and takeaways.

Usage:
  python3 workflowy-mine.py

Requires WORKFLOWY_API_KEY env var or --api-key flag.
Output: cache/mined-workflowy.json
"""

import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, save_mined

BASE_URL = "https://workflowy.com/api/v1"
HTML_TAG_RE = re.compile(r"<[^>]+>")
HASHTAG_RE = re.compile(r"#(\w+)")
DATE_NODE_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"\d{1,2}, \d{4}$"
)


def get_api_key():
    key = os.environ.get("WORKFLOWY_API_KEY", "")
    if not key:
        for i, arg in enumerate(sys.argv):
            if arg == "--api-key" and i + 1 < len(sys.argv):
                key = sys.argv[i + 1]
    if not key:
        print("ERROR: Set WORKFLOWY_API_KEY env var or use --api-key", file=sys.stderr)
        sys.exit(1)
    return key


def strip_html(text):
    return HTML_TAG_RE.sub("", text or "")


def fetch_export(api_key):
    req = urllib.request.Request(
        f"{BASE_URL}/nodes-export",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    nodes = data.get("nodes", data) if isinstance(data, dict) else data
    return nodes


def build_tree(nodes):
    """Build parent→children mapping and id→node lookup."""
    by_id = {}
    children_of = defaultdict(list)
    for n in nodes:
        nid = n.get("id", "")
        by_id[nid] = n
        parent = n.get("parent_id", "")
        if parent:
            children_of[parent].append(nid)
    return by_id, children_of


def get_subtree_text(node_id, by_id, children_of, max_depth=5, depth=0):
    """Get all text content from a node and its descendants."""
    if depth >= max_depth:
        return ""
    node = by_id.get(node_id, {})
    name = strip_html(node.get("name", ""))
    texts = [name] if name else []
    for child_id in children_of.get(node_id, []):
        texts.append(get_subtree_text(child_id, by_id, children_of, max_depth, depth + 1))
    return "\n".join(texts)


def extract_names_from_title(title):
    """Extract person names from meeting title. E.g., '1:1 with Alice Chen' → ['Alice Chen']"""
    title = strip_html(title)
    # Remove hashtags
    title = HASHTAG_RE.sub("", title).strip()
    # Remove common meeting prefixes
    for prefix in ["1:1 with", "1:1:", "Meeting with", "Sync with", "Call with",
                    "Coffee with", "Lunch with", "Chat with"]:
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()

    # Remove time patterns like "10:00 AM" or "2:30 PM"
    title = re.sub(r"\d{1,2}:\d{2}\s*(AM|PM|am|pm)?", "", title)
    # Remove date patterns
    title = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", "", title)

    # Split on separators
    parts = re.split(r"[/|,\-–—]", title)
    names = []
    for part in parts:
        part = part.strip()
        # A name is 2+ words, each starting with uppercase, no digits
        if part and len(part) > 2 and not any(c.isdigit() for c in part):
            words = part.split()
            if 1 <= len(words) <= 4 and all(w[0].isupper() for w in words if w):
                names.append(part)
    return names


def mine_workflowy():
    api_key = get_api_key()

    print("Fetching Workflowy export...", file=sys.stderr)
    nodes = fetch_export(api_key)
    print(f"  {len(nodes)} total nodes", file=sys.stderr)

    by_id, children_of = build_tree(nodes)

    # Find meeting nodes: children of date heading nodes
    # Structure: Year > Month > Date heading > Meeting node
    contacts = defaultdict(lambda: {
        "name": "",
        "meeting_count": 0,
        "first_meeting": None,
        "last_meeting": None,
        "meeting_titles": [],
        "topics": [],
    })

    meetings_found = 0
    date_context = ""

    for node in nodes:
        name = strip_html(node.get("name", "") or "")

        # Is this a date heading? (e.g., "Tue, Feb 11, 2026")
        if DATE_NODE_RE.match(name):
            date_context = name
            continue

        # Is this a meeting node? (child of a date heading, contains hashtags or person names)
        hashtags = HASHTAG_RE.findall(name)
        parent = by_id.get(node.get("parent_id", ""), {})
        parent_name = strip_html(parent.get("name", "") or "")

        # Meeting indicators: has hashtags, or parent is a date heading
        is_under_date = DATE_NODE_RE.match(parent_name)
        has_meeting_indicators = hashtags or any(
            kw in name.lower() for kw in ["1:1", "sync", "standup", "review", "meeting", "catch up", "check-in"]
        )

        if is_under_date or (has_meeting_indicators and len(name) > 5):
            meetings_found += 1

            # Extract person names from title and hashtags
            title_names = extract_names_from_title(name)

            # Hashtags are often first names of attendees
            for tag in hashtags:
                if len(tag) > 1 and tag[0].isupper():
                    key = tag.lower()
                    c = contacts[key]
                    c["name"] = tag
                    c["meeting_count"] += 1
                    if name and len(c["meeting_titles"]) < 20:
                        c["meeting_titles"].append(name[:100])
                    if date_context:
                        if not c["first_meeting"] or date_context < c["first_meeting"]:
                            c["first_meeting"] = date_context
                        if not c["last_meeting"] or date_context > c["last_meeting"]:
                            c["last_meeting"] = date_context

            for pname in title_names:
                key = pname.lower()
                c = contacts[key]
                c["name"] = pname
                c["meeting_count"] += 1
                if name and len(c["meeting_titles"]) < 20:
                    c["meeting_titles"].append(name[:100])
                if date_context:
                    if not c["first_meeting"] or date_context < c["first_meeting"]:
                        c["first_meeting"] = date_context
                    if not c["last_meeting"] or date_context > c["last_meeting"]:
                        c["last_meeting"] = date_context

            # Get subtree text for topic extraction
            subtree = get_subtree_text(node.get("id", ""), by_id, children_of, max_depth=3)
            if subtree and len(subtree) > 20:
                for pname in title_names + [t for t in hashtags if t[0].isupper()]:
                    key = pname.lower()
                    if key in contacts and len(contacts[key]["topics"]) < 10:
                        # First 200 chars of subtree as topic context
                        contacts[key]["topics"].append(subtree[:200])

    print(f"  {meetings_found} meeting nodes found", file=sys.stderr)
    print(f"  {len(contacts)} unique people from meetings", file=sys.stderr)

    output = {k: dict(v) for k, v in contacts.items()}

    result = {
        "status": "ok",
        "source": "workflowy",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "total_nodes": len(nodes),
        "meetings_found": meetings_found,
        "contacts_found": len(output),
        "contacts": output,
    }

    save_mined("workflowy", result)

    # Show top contacts
    top = sorted(output.values(), key=lambda c: -c["meeting_count"])[:15]
    for c in top:
        print(f"  {c['name']:25s} {c['meeting_count']:3d} meetings", file=sys.stderr)

    print(f"\nDone. {len(output)} contacts from {meetings_found} meetings.", file=sys.stderr)


if __name__ == "__main__":
    mine_workflowy()
