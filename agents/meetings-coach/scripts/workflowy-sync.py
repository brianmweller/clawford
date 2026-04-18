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

WORKSPACE = os.path.expanduser("~/.clawford/meetings-coach-workspace")
CACHE_DIR = os.path.join(WORKSPACE, "cache")
LINKS_FILE = os.path.join(CACHE_DIR, "workflowy-links.json")
CONTACTS_CACHE_FILE = os.path.join(CACHE_DIR, "contact-names.json")
CONFIG_PATH = os.path.join(WORKSPACE, "meeting-config.json")
GOOGLE_TOKEN_PATH = os.path.join(WORKSPACE, "token.json")

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
    """Get Workflowy API key from env or .env file."""
    key = os.environ.get("WORKFLOWY_API_KEY", "")
    if not key:
        # Fallback: read from .env files. Workspace .env is the primary
        # source inside Docker (where the host ~/clawford/.env isn't mounted).
        for env_file in [
            os.path.join(WORKSPACE, ".env"),
            os.path.expanduser("~/clawford/.env"),
            "/home/openclaw/clawford/.env",
            os.path.expanduser("~/.env"),
            "/tmp/.env",
        ]:
            if os.path.exists(env_file):
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("WORKFLOWY_API_KEY=") and not line.startswith("#"):
                            key = line.split("=", 1)[1].strip().strip("'\"")
                            if key:
                                return key
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


def _load_contact_cache():
    if os.path.exists(CONTACTS_CACHE_FILE):
        try:
            with open(CONTACTS_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_contact_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CONTACTS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _get_gmail_service():
    """Build a Gmail API service using the shared Google token."""
    if not os.path.exists(GOOGLE_TOKEN_PATH):
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        return None

    with open(GOOGLE_TOKEN_PATH) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_data["token"] = creds.token
            with open(GOOGLE_TOKEN_PATH, "w") as f:
                json.dump(token_data, f, indent=2)
        except Exception:
            return None

    if not creds.valid:
        return None

    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception:
        return None


# From: "Chris Example" <chris.example@example.com>  OR  From: chris.example@example.com
_FROM_HEADER_RE = re.compile(
    r'^\s*"?([^"<]+?)"?\s*<([^>]+)>\s*$'
)


def _gmail_lookup_name(service, email):
    """Search Gmail for messages involving email and return the real display name.

    Searches both incoming (from:) and outgoing (to:) messages. Parses the
    From/To header to extract the display name when present.
    """
    if not service or not email:
        return None

    email_lower = email.lower()

    try:
        # Search recent messages where this person was sender or recipient
        query = f"from:{email} OR to:{email}"
        results = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=5,
        ).execute()

        messages = results.get("messages", [])
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me",
                id=msg_ref["id"],
                format="metadata",
                metadataHeaders=["From", "To"],
            ).execute()

            headers = msg.get("payload", {}).get("headers", [])
            for h in headers:
                if h.get("name") not in ("From", "To"):
                    continue
                value = h.get("value", "")
                # Could contain multiple addresses (in To:) — split on commas
                for addr_str in value.split(","):
                    match = _FROM_HEADER_RE.match(addr_str.strip())
                    if not match:
                        continue
                    display_name = match.group(1).strip()
                    addr = match.group(2).strip().lower()
                    if addr == email_lower and display_name and display_name.lower() != email_lower:
                        # Filter out cases where display name == email
                        return display_name
    except Exception:
        pass

    return None


def _resolve_contact_name(email, config=None, cache=None, service=None):
    """Resolve an email to a real display name.

    Priority:
    1. Manual override in config.attendee_name_overrides
    2. Cached lookup
    3. Gmail search
    4. None (caller falls back to email local part)

    Returns a tuple (name_or_none, source_string).
    """
    if not email:
        return None, "no_email"

    email_lower = email.lower().strip()

    # 1. Manual override
    if config:
        overrides = config.get("attendee_name_overrides", {})
        if email_lower in overrides and not email_lower.startswith("_"):
            return overrides[email_lower], "override"

    # 2. Cache
    if cache and email_lower in cache:
        entry = cache[email_lower]
        if entry.get("name"):
            return entry["name"], "cache"
        # Cached as "not found" — don't re-query
        if entry.get("checked_at"):
            return None, "cache_miss"

    # 3. Gmail lookup
    if service is None:
        service = _get_gmail_service()

    name = _gmail_lookup_name(service, email_lower)

    # Save to cache (even on miss, so we don't re-query)
    if cache is not None:
        cache[email_lower] = {
            "name": name,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_contact_cache(cache)

    return (name, "gmail") if name else (None, "not_found")


def _attendee_tokens(attendees, config=None):
    """Extract matching tokens from GCal attendees.

    Uses resolved real names from Gmail/config overrides when available,
    falling back to email local parts and the raw display name.

    For Chris Example (chris.example@example.com, Gmail says "Chris Example"):
      returns {"Chris", "Example", "Chris Example", "chrisc", "cexample", "Chris.Example"}
    """
    tokens = set()
    service = _get_gmail_service() if attendees else None
    cache = _load_contact_cache()

    for att in attendees or []:
        raw_name = (att.get("name") or "").lower().strip()
        email = (att.get("email") or "").lower().strip()

        # Resolve real name (Gmail / override / cache)
        resolved, source = _resolve_contact_name(email, config=config, cache=cache, service=service)
        real_name = (resolved or raw_name or "").lower().strip()

        # Tokenize the real name (first name, last name, full)
        if real_name:
            tokens.add(real_name)
            for part in re.split(r"[\s,\-_.]+", real_name):
                if len(part) > 1:
                    tokens.add(part)

        # Also tokenize email local part as a fallback
        if email and "@" in email:
            local = email.split("@")[0]
            tokens.add(local)
            for sep in [".", "_", "-", "+"]:
                for part in local.split(sep):
                    if len(part) > 1:
                        tokens.add(part)
            # First-initial + last name variants (e.g. cexample, chrisc)
            parts = local.replace("_", ".").replace("-", ".").split(".")
            if len(parts) >= 2 and parts[0] and parts[-1]:
                tokens.add(parts[0][0] + parts[-1])
                tokens.add(parts[0] + parts[-1][0])

    return tokens


def _node_tokens(child_name):
    """Extract matching tokens from a Workflowy node: hashtags + title words."""
    clean = strip_html(child_name or "").strip().lower()
    tokens = set()

    # Hashtags (without the #)
    for tag in HASHTAG_RE.findall(clean):
        tokens.add(tag.lower())

    # Title words (hashtags stripped)
    title_only = HASHTAG_RE.sub("", clean).strip()
    for word in re.split(r"[\s,\-_.]+", title_only):
        word = word.strip()
        if len(word) > 1:
            tokens.add(word)

    return tokens


def _find_matching_child(existing_children, title, attendees, config=None):
    """Find an existing Workflowy child that matches a GCal event.

    Strategy (in order of confidence):
    1. Exact title match (case-insensitive, hashtags stripped)
    2. Attendee token overlap: any real name / email token from the invite
       (resolved via Gmail lookup) appears as a hashtag or title word in
       the Workflowy node
    3. Title substring match (either direction)
    4. Fuzzy title similarity >= 0.6

    Returns the matching child dict or None.
    """
    gcal_title = title.lower().strip()
    gcal_tokens = _attendee_tokens(attendees, config=config)

    for child in existing_children:
        child_name = strip_html((child.get("name") or "").strip())
        child_title = HASHTAG_RE.sub("", child_name).strip().lower()
        child_tokens = _node_tokens(child_name)

        # 1. Exact title match
        if gcal_title and child_title == gcal_title:
            return child

        # 2. Attendee token overlap (strongest signal — hashtags encode identity)
        if gcal_tokens & child_tokens:
            return child

        # 3. Substring match (either direction)
        if gcal_title and child_title:
            if gcal_title in child_title or child_title in gcal_title:
                return child

        # 4. Fuzzy fallback
        if gcal_title and child_title:
            ratio = SequenceMatcher(None, gcal_title, child_title).ratio()
            if ratio >= 0.6:
                return child

    return None


def create_meeting_node(meeting_date, title, hashtags=None, attendees=None, api_key=None, config=None):
    """Create a meeting node with the standard template structure."""
    nodes = get_export(api_key=api_key)
    date_id = find_or_create_date_path(meeting_date, nodes, api_key=api_key)

    # Duplicate prevention: try to find an existing matching meeting
    existing = get_children(date_id, api_key=api_key)
    matched = _find_matching_child(existing, title, attendees or [], config=config)
    if matched:
        return matched.get("id")

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
    config = _load_config()

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

        # Build hashtags from resolved attendee names (Gmail → real name → first name)
        attendees = event.get("attendees", [])
        cache = _load_contact_cache()
        service = _get_gmail_service()
        hashtags = []
        for att in attendees:
            email = (att.get("email") or "").lower().strip()
            resolved, _ = _resolve_contact_name(email, config=config, cache=cache, service=service)
            name = resolved or att.get("name", "")
            first_name = name.split()[0] if name else ""
            if first_name and len(first_name) > 1 and "." not in first_name:
                hashtags.append(first_name)

        try:
            node_id = create_meeting_node(
                meeting_date, title,
                hashtags=hashtags,
                attendees=attendees,
                api_key=api_key,
                config=config,
            )
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
    import json as _contract_json
    import sys as _contract_sys
    _contract_status = "ok"
    _contract_error = None
    try:
        _contract_rc = main()
        if _contract_rc not in (0, None):
            _contract_status = "error"
            _contract_error = f"main returned {_contract_rc}"
    except SystemExit as _contract_e:
        if _contract_e.code not in (0, None):
            _contract_status = "error"
            _contract_error = f"main exited with code {_contract_e.code}"
    except BaseException as _contract_e:  # noqa: BLE001
        _contract_status = "error"
        _contract_error = str(_contract_e)[:200]
    _contract_envelope = {"status": _contract_status}
    if _contract_error:
        _contract_envelope["error"] = _contract_error
    print(_contract_json.dumps(_contract_envelope))
    _contract_sys.exit(0)
