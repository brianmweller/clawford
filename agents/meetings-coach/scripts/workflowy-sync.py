#!/usr/bin/env python3
"""
workflowy-sync.py — Create meeting nodes and push AI bullets to Workflowy.

Uses the Workflowy REST API (https://workflowy.com/api/v1) with bearer token
auth to manage meeting nodes in Sam's Workflowy structure:
  Year > Month > Date heading > Meeting > Pre/during > Agenda + Notes, Post-meeting > Takeaways

Usage:
  python3 workflowy-sync.py --create-nodes          # Create nodes for today's events
  python3 workflowy-sync.py --push-bullets EVENT_ID  # Push AI bullets to a meeting's Agenda
  python3 workflowy-sync.py --read-agenda EVENT_ID   # Read existing agenda items
  python3 workflowy-sync.py --sync                   # Full sync: export, parse, update cache

Requires: requests (or urllib — uses stdlib only for portability)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import urllib.request
import urllib.error

WORKSPACE = os.path.expanduser("~/.openclaw/meetings-coach-workspace")
CACHE_DIR = os.path.join(WORKSPACE, "cache")
LINKS_FILE = os.path.join(CACHE_DIR, "workflowy-links.json")
CONFIG_PATH = os.path.join(WORKSPACE, "meeting-config.json")

BASE_URL = "https://workflowy.com/api/v1"

# Matches Workflowy date headings: "Tue, Feb 11, 2026"
DATE_NODE_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"\d{1,2}, \d{4}$"
)

HASHTAG_RE = re.compile(r"#(\w+)")
HTML_TAG_RE = re.compile(r"<[^>]+>")

MONTH_NAMES = {
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
}


def get_api_key():
    key = os.environ.get("WORKFLOWY_API_KEY", "")
    if not key:
        print(json.dumps({"status": "error", "message": "WORKFLOWY_API_KEY not set"}))
        sys.exit(1)
    return key


def strip_html(text):
    return HTML_TAG_RE.sub("", text)


def api_request(method, path, data=None, api_key=None):
    """Make a Workflowy API request with retry logic."""
    if api_key is None:
        api_key = get_api_key()

    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(4):
        try:
            if data is not None:
                body = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
            else:
                req = urllib.request.Request(url, headers=headers, method=method)

            resp = urllib.request.urlopen(req, timeout=30)
            content = resp.read().decode("utf-8")
            if content:
                return json.loads(content)
            return {}

        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                delay = 5 * (2 ** attempt)
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        pass
                time.sleep(delay)
                continue
            raise
        except urllib.error.URLError:
            time.sleep(5 * (2 ** attempt))
            continue

    raise RuntimeError(f"Workflowy API request failed after 4 attempts: {method} {path}")


def get_export(api_key=None):
    """Fetch the flat node export from Workflowy."""
    data = api_request("GET", "/nodes-export", api_key=api_key)
    if isinstance(data, dict):
        return data.get("nodes", [])
    if isinstance(data, list):
        return data
    return []


def get_children(parent_id, api_key=None):
    """Fetch children of a specific node."""
    data = api_request("GET", f"/nodes?parent_id={parent_id}", api_key=api_key)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("nodes", data.get("items", []))
    return []


def create_node(parent_id, name, position=None, api_key=None):
    """Create a child node under a parent."""
    payload = {"parent_id": parent_id, "name": name}
    if position:
        payload["position"] = position
    return api_request("POST", "/nodes", data=payload, api_key=api_key)


def extract_node_id(result):
    """Extract node ID from a create-node API response."""
    if isinstance(result, dict):
        nid = result.get("item_id") or result.get("id")
        if nid:
            return str(nid)
    if isinstance(result, str) and result:
        return result
    raise RuntimeError(f"Could not extract node ID from: {result}")


def load_links():
    """Load the Workflowy node-to-event mapping cache."""
    if os.path.exists(LINKS_FILE):
        with open(LINKS_FILE) as f:
            return json.load(f)
    return {}


def save_links(links):
    """Save the Workflowy node-to-event mapping cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LINKS_FILE, "w") as f:
        json.dump(links, f, indent=2)


# ── Node creation ───────────────────────────────────────────────

def find_child_by_name(children, target):
    """Find a child node by name (case-insensitive, HTML-stripped)."""
    target_clean = target.strip().lower()
    for child in children:
        raw = (child.get("name") or "").strip()
        clean = strip_html(raw).lower()
        if clean == target_clean:
            return child.get("id")
    return None


def find_or_create_date_path(meeting_date, nodes, api_key=None):
    """Navigate/create Year > Month > Date heading hierarchy. Returns date node ID."""
    children_map = {}
    for node in nodes:
        children_map.setdefault(node.get("parent_id"), []).append(node)

    year_str = str(meeting_date.year)

    # Find year node
    year_id = None
    year_parent_id = None
    for node in nodes:
        name = strip_html((node.get("name") or "").strip())
        if name == year_str:
            kids = children_map.get(node.get("id"), [])
            has_month = any(
                strip_html((k.get("name") or "").strip()).split()[0] in MONTH_NAMES
                for k in kids
                if strip_html((k.get("name") or "").strip())
            )
            if has_month or not year_id:
                year_id = node.get("id")
                year_parent_id = node.get("parent_id")
                if has_month:
                    break

    if not year_id:
        # Find sibling year's parent
        parent_id = None
        for node in nodes:
            name = strip_html((node.get("name") or "").strip())
            if re.match(r"^\d{4}$", name):
                kids = children_map.get(node.get("id"), [])
                has_month = any(
                    strip_html((k.get("name") or "").strip()).split()[0] in MONTH_NAMES
                    for k in kids if strip_html((k.get("name") or "").strip())
                )
                if has_month:
                    parent_id = node.get("parent_id")
                    break
        result = create_node(parent_id, year_str, api_key=api_key)
        year_id = extract_node_id(result)

    # Find or create month node
    month_str = f"{meeting_date.strftime('%B')} {meeting_date.year}"
    year_children = children_map.get(year_id, [])
    month_id = find_child_by_name(year_children, month_str)
    if not month_id:
        result = create_node(year_id, month_str, position="top", api_key=api_key)
        month_id = extract_node_id(result)

    # Find or create date heading
    date_str = (
        f"{meeting_date.strftime('%a')}, "
        f"{meeting_date.strftime('%b')} {meeting_date.day}, "
        f"{meeting_date.year}"
    )
    month_children = children_map.get(month_id, [])
    date_id = find_child_by_name(month_children, date_str)
    if not date_id:
        date_html = (
            f'<time startYear="{meeting_date.year}" '
            f'startMonth="{meeting_date.month}" '
            f'startDay="{meeting_date.day}">'
            f"{date_str}</time>"
        )
        result = create_node(month_id, date_html, api_key=api_key)
        date_id = extract_node_id(result)

    return date_id


def create_meeting_node(meeting_date, title, hashtags=None, api_key=None):
    """Create a meeting node with the standard template structure."""
    nodes = get_export(api_key=api_key)
    date_id = find_or_create_date_path(meeting_date, nodes, api_key=api_key)

    # Duplicate prevention
    existing = get_children(date_id, api_key=api_key)
    for child in existing:
        child_name = strip_html((child.get("name") or "").strip())
        child_title = HASHTAG_RE.sub("", child_name).strip()
        ratio = SequenceMatcher(None, title.lower().strip(), child_title.lower()).ratio()
        if ratio >= 0.6:
            return child.get("id")

    # Build node name with hashtags
    node_name = title
    if hashtags:
        tags_str = " ".join(f"#{tag}" for tag in hashtags)
        node_name = f"{title} {tags_str}"

    meeting_result = create_node(date_id, node_name, api_key=api_key)
    meeting_node_id = extract_node_id(meeting_result)

    # Create subsections
    pre_result = create_node(meeting_node_id, "Pre / during", api_key=api_key)
    pre_id = extract_node_id(pre_result)
    create_node(pre_id, "\U0001f4d4  Agenda", api_key=api_key)
    create_node(pre_id, "\U0001f4dd  Notes", api_key=api_key)

    post_result = create_node(meeting_node_id, "Post-meeting", api_key=api_key)
    post_id = extract_node_id(post_result)
    create_node(post_id, "\u2705  Takeaways", api_key=api_key)

    return meeting_node_id


def find_agenda_node(meeting_node_id, api_key=None):
    """Find the Agenda node under a meeting node."""
    children = get_children(meeting_node_id, api_key=api_key)
    for child in children:
        name = strip_html((child.get("name") or "").strip())
        if "Pre" in name or "during" in name:
            pre_children = get_children(child.get("id"), api_key=api_key)
            for sub in pre_children:
                sub_name = strip_html((sub.get("name") or "").strip())
                if "Agenda" in sub_name:
                    return sub.get("id")
    return None


# ── Commands ────────────────────────────────────────────────────

def cmd_create_nodes():
    """Create Workflowy meeting nodes for today's calendar events."""
    api_key = get_api_key()
    links = load_links()

    # Load today's events
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_path = os.path.join(CACHE_DIR, f"events-{today}.json")
    if not os.path.exists(cache_path):
        print(json.dumps({"status": "error", "message": "No cached events — run gcal-fetch.py first"}))
        sys.exit(1)

    with open(cache_path) as f:
        data = json.load(f)

    events = [e for e in data.get("events", []) if e.get("is_real_meeting", False)]
    created = []
    skipped = []

    for event in events:
        event_id = event.get("id", "")
        title = event.get("summary", "")

        # Skip if already linked
        if event_id in links:
            skipped.append({"event_id": event_id, "title": title, "reason": "already linked"})
            continue

        # Parse meeting date
        start = event.get("start", "")
        try:
            if "T" in start:
                meeting_date = datetime.fromisoformat(start.replace("Z", "+00:00"))
            else:
                meeting_date = datetime.strptime(start, "%Y-%m-%d")
        except ValueError:
            skipped.append({"event_id": event_id, "title": title, "reason": "bad date"})
            continue

        # Extract hashtags from attendee names
        hashtags = []
        for att in event.get("attendees", []):
            name = att.get("name", "")
            first_name = name.split()[0] if name else ""
            if first_name and len(first_name) > 1:
                hashtags.append(first_name)

        try:
            node_id = create_meeting_node(meeting_date, title, hashtags=hashtags, api_key=api_key)
            links[event_id] = {
                "node_id": node_id,
                "title": title,
                "date": today,
            }
            created.append({"event_id": event_id, "title": title, "node_id": node_id})
            time.sleep(0.5)  # Rate limiting
        except Exception as e:
            skipped.append({"event_id": event_id, "title": title, "reason": str(e)})

    save_links(links)

    print(json.dumps({
        "status": "ok",
        "created": created,
        "skipped": skipped,
    }, indent=2))


def cmd_push_bullets(event_id):
    """Push AI-generated bullets to a meeting's Agenda node in Workflowy."""
    api_key = get_api_key()
    links = load_links()

    link = links.get(event_id)
    if not link:
        print(json.dumps({"status": "error", "message": f"No Workflowy link for event {event_id}"}))
        sys.exit(1)

    node_id = link.get("node_id")

    # Load prep data
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prep_path = os.path.join(CACHE_DIR, f"prep-{event_id}-{today}.json")
    if not os.path.exists(prep_path):
        print(json.dumps({"status": "error", "message": f"No prep cache for {event_id}"}))
        sys.exit(1)

    with open(prep_path) as f:
        prep = json.load(f)

    bullets = prep.get("talking_points", [])
    if not bullets:
        print(json.dumps({"status": "ok", "pushed_count": 0, "reason": "no bullets"}))
        return

    # Load config for prefix
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    prefix = config.get("workflowy", {}).get("ai_bullet_prefix", "[AI]")

    # Find agenda node
    agenda_id = find_agenda_node(node_id, api_key=api_key)
    if not agenda_id:
        print(json.dumps({"status": "error", "message": "Agenda node not found under meeting"}))
        sys.exit(1)

    # Check existing children for duplicates
    existing = get_children(agenda_id, api_key=api_key)
    existing_names = {strip_html((c.get("name") or "").strip()) for c in existing}

    pushed = 0
    for bullet in bullets:
        text = f"{prefix} {bullet}" if not bullet.startswith(prefix) else bullet
        if text.strip() in existing_names:
            continue
        create_node(agenda_id, text, api_key=api_key)
        pushed += 1
        time.sleep(0.3)

    print(json.dumps({
        "status": "ok",
        "pushed_count": pushed,
        "event_id": event_id,
        "node_id": node_id,
    }, indent=2))


def cmd_read_agenda(event_id):
    """Read existing agenda items from Workflowy for a meeting."""
    api_key = get_api_key()
    links = load_links()

    link = links.get(event_id)
    if not link:
        print(json.dumps({"status": "ok", "agenda_items": [], "reason": "no link"}))
        return

    node_id = link.get("node_id")
    agenda_id = find_agenda_node(node_id, api_key=api_key)
    if not agenda_id:
        print(json.dumps({"status": "ok", "agenda_items": [], "reason": "no agenda node"}))
        return

    children = get_children(agenda_id, api_key=api_key)

    # Load config for prefix filtering
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    prefix = config.get("workflowy", {}).get("ai_bullet_prefix", "[AI]")

    items = []
    for child in children:
        name = strip_html((child.get("name") or "").strip())
        if name and not name.startswith(prefix):
            items.append(name)

    print(json.dumps({
        "status": "ok",
        "agenda_items": items,
        "event_id": event_id,
    }, indent=2))


def cmd_sync():
    """Full sync: export, parse meetings from Workflowy, update cache."""
    api_key = get_api_key()
    nodes = get_export(api_key=api_key)

    # Build children map
    children_map = {}
    for node in nodes:
        children_map.setdefault(node.get("parent_id"), []).append(node)

    # Find date headings and parse meetings
    meetings = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)

    for node in nodes:
        raw_name = (node.get("name") or "").strip()
        clean_name = strip_html(raw_name)
        if not DATE_NODE_RE.match(clean_name):
            continue

        # Parse date
        try:
            node_date = datetime.strptime(clean_name, "%a, %b %d, %Y")
            node_date = node_date.replace(tzinfo=timezone.utc)
            if node_date < cutoff:
                continue
        except ValueError:
            continue

        # Each child is a meeting
        date_children = children_map.get(node.get("id"), [])
        for meeting_node in date_children:
            title = strip_html((meeting_node.get("name") or "").strip())
            if not title:
                continue
            meetings.append({
                "date": clean_name,
                "title": HASHTAG_RE.sub("", title).strip(),
                "hashtags": HASHTAG_RE.findall(title),
                "node_id": meeting_node.get("id"),
            })

    # Save sync cache
    os.makedirs(CACHE_DIR, exist_ok=True)
    sync_path = os.path.join(CACHE_DIR, "workflowy-sync.json")
    with open(sync_path, "w") as f:
        json.dump({
            "meetings": meetings,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)

    print(json.dumps({
        "status": "ok",
        "meetings_found": len(meetings),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def main():
    if "--create-nodes" in sys.argv:
        cmd_create_nodes()
    elif "--push-bullets" in sys.argv:
        idx = sys.argv.index("--push-bullets")
        if idx + 1 >= len(sys.argv):
            print(json.dumps({"status": "error", "message": "--push-bullets requires EVENT_ID"}))
            sys.exit(1)
        cmd_push_bullets(sys.argv[idx + 1])
    elif "--read-agenda" in sys.argv:
        idx = sys.argv.index("--read-agenda")
        if idx + 1 >= len(sys.argv):
            print(json.dumps({"status": "error", "message": "--read-agenda requires EVENT_ID"}))
            sys.exit(1)
        cmd_read_agenda(sys.argv[idx + 1])
    elif "--sync" in sys.argv:
        cmd_sync()
    else:
        print(json.dumps({"status": "error", "message": "Usage: --create-nodes | --push-bullets EVENT_ID | --read-agenda EVENT_ID | --sync"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
