"""Workflowy walker — Stage 1's second source alongside the filesystem
career-archive walker.

Pulls the operator's entire Workflowy tree via the /nodes-export endpoint,
identifies meeting nodes (those whose parent is a Workflowy date-heading
node like "Tue, Feb 11, 2026"), and emits records in the same shape as
iter_file_records so the downstream classifier pipeline is identical.

Chronological tagging: every record carries a parent_date (ISO date)
derived from the date-heading ancestor — this is the HIGH-CONFIDENCE
signal for when the meeting happened. Node-level last_modified tells us
when the notes were last edited, NOT when the meeting occurred. The
classifier receives parent_date as a hint and should prefer it.

Record shape (identical keys to iter_file_records, plus parent_date):
  {
    "path": "wf://<node_id>",
    "role": "meetings",
    "ext": ".wf",
    "size": int,
    "mtime": ISO string,
    "text_excerpt": meeting title + flattened descendants,
    "parent_date": ISO date from parent heading,
  }

The impure get_nodes_export() wraps the Workflowy API; tests exercise
the pure helpers (is_date_heading, extract_date_from_heading,
flatten_descendants, iter_meeting_records) using fake exports.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Iterator


# Matches "Tue, Feb 11, 2026" — same regex Murphy's workflowy-sync uses.
DATE_HEADING_RE = re.compile(
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{1,2}),\s+(\d{4})\b"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")

_MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


# Last-modified field candidates across Workflowy API versions
_MTIME_KEYS = ("last_modified_at", "lastModified", "last_modified", "lm")


def strip_html(text: str | None) -> str:
    """Remove HTML tags from a Workflowy node name. Workflowy wraps dates
    in <time> tags and formatted text in spans."""
    if not text:
        return ""
    return _HTML_TAG_RE.sub("", text).strip()


def is_date_heading(name: str | None) -> bool:
    """True if the (possibly HTML-wrapped) node name matches the Workflowy
    date-heading format."""
    if not name:
        return False
    clean = strip_html(name)
    return bool(DATE_HEADING_RE.search(clean))


def extract_date_from_heading(name: str | None) -> str:
    """Return an ISO date (YYYY-MM-DD) parsed from a Workflowy date
    heading, or empty string if the name doesn't match."""
    if not name:
        return ""
    m = DATE_HEADING_RE.search(strip_html(name))
    if not m:
        return ""
    month = _MONTH_MAP.get(m.group(2), 0)
    day = int(m.group(3))
    year = int(m.group(4))
    if not month:
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _get_mtime(node: dict) -> str:
    """Return the node's last-modified timestamp as an ISO string.
    Tries several API field names; returns empty if none present."""
    for key in _MTIME_KEYS:
        v = node.get(key)
        if v:
            return str(v)
    return ""


def flatten_descendants(node: dict, children_map: dict[str, list[dict]]) -> str:
    """Return the node's name plus every descendant's name, flattened
    into a newline-separated text blob. Strips HTML. Used to build the
    classifier excerpt for a meeting node."""
    parts: list[str] = []

    def _walk(n: dict, depth: int) -> None:
        name = strip_html(n.get("name", "")).strip()
        if name:
            if depth == 0:
                parts.append(name)
            else:
                parts.append(f"{'  ' * depth}- {name}")
        for child in children_map.get(n.get("id", ""), []):
            _walk(child, depth + 1)

    _walk(node, 0)
    # Also include node description/note if present (Workflowy "notes"
    # field holds free-form per-node commentary)
    note = strip_html(node.get("note", "") or node.get("description", ""))
    if note:
        parts.insert(1, note)
    return "\n".join(parts)


def _build_children_map(nodes: list[dict]) -> dict[str, list[dict]]:
    """Group nodes by parent_id for O(1) child lookup during traversal."""
    children_map: dict[str, list[dict]] = {}
    for node in nodes:
        parent_id = node.get("parent_id")
        children_map.setdefault(parent_id, []).append(node)
    return children_map


def _build_id_map(nodes: list[dict]) -> dict[str, dict]:
    return {n["id"]: n for n in nodes if "id" in n}


def iter_meeting_records(nodes: list[dict]) -> Iterator[dict]:
    """Walk the flat node export, identify meeting nodes, and yield one
    record per meeting in the Stage-1 record shape.

    A node is a "meeting" iff its parent is a date-heading node. Every
    child of a date-heading becomes its own meeting record, with all
    descendants flattened into the text_excerpt.
    """
    if not nodes:
        return
    children_map = _build_children_map(nodes)
    id_map = _build_id_map(nodes)

    for node in nodes:
        parent_id = node.get("parent_id")
        parent = id_map.get(parent_id) if parent_id else None
        parent_name = parent.get("name") if parent else None
        if not is_date_heading(parent_name):
            continue

        parent_date = extract_date_from_heading(parent_name)
        text_excerpt = flatten_descendants(node, children_map)
        mtime = _get_mtime(node)
        if not mtime and parent_date:
            # Fall back to parent_date so idempotency keys off of
            # something reproducible. Synthesizes an ISO datetime.
            mtime = f"{parent_date}T00:00:00+00:00"

        yield {
            "path": f"wf://{node['id']}",
            "role": "meetings",
            "ext": ".wf",
            "size": len(text_excerpt),
            "mtime": mtime,
            "text_excerpt": text_excerpt,
            "parent_date": parent_date,
        }


# ---------------------------------------------------------------------------
# Impure: fetch the export from the Workflowy API
# ---------------------------------------------------------------------------


BASE_URL = "https://workflowy.com/api/v1"
DEFAULT_TIMEOUT_S = 60


def get_api_key() -> str:
    """Fetch WORKFLOWY_API_KEY from env or .env files.

    Matches the fallback order Murphy's workflowy-sync.py uses so the
    key works in both places without duplication.
    """
    key = os.environ.get("WORKFLOWY_API_KEY", "")
    if key:
        return key

    for env_file in [
        os.path.expanduser("~/.clawford/meetings-coach-workspace/.env"),
        os.path.expanduser("~/clawford/.env"),
        "/home/openclaw/clawford/.env",
        os.path.expanduser("~/.env"),
    ]:
        if not os.path.exists(env_file):
            continue
        try:
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("WORKFLOWY_API_KEY=") and not line.startswith("#"):
                        val = line.split("=", 1)[1].strip().strip("'\"")
                        if val:
                            return val
        except OSError:
            continue
    return ""


def get_nodes_export(api_key: str | None = None, *, timeout: int = DEFAULT_TIMEOUT_S) -> list[dict]:
    """Fetch the flat node list from /nodes-export. Retries on 429/5xx.

    Returns empty list on failure rather than raising — the Stage 1
    runner treats Workflowy as one source among several and should keep
    going if it's unavailable.
    """
    key = api_key or get_api_key()
    if not key:
        print("WARNING: WORKFLOWY_API_KEY not found — skipping Workflowy walk", file=sys.stderr)
        return []

    url = f"{BASE_URL}/nodes-export"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return []
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data.get("nodes", []) or []
                if isinstance(data, list):
                    return data
                return []
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
            print(f"WARNING: Workflowy export HTTP {e.code}: {e.reason}", file=sys.stderr)
            return []
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"WARNING: Workflowy export network error: {e}", file=sys.stderr)
            return []

    print("WARNING: Workflowy export failed after retries", file=sys.stderr)
    return []
