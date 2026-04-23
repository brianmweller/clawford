#!/usr/bin/env python3
"""
transcript-scan.py — Fetch Krisp transcripts via MCP and match to calendar meetings.

Connects to Krisp's MCP server (https://mcp.krisp.ai/mcp) using OAuth 2.1
tokens (stored locally), fetches recent transcripts, matches them to
recently-ended calendar events using fuzzy scoring, and stages the data
for the agent to process.

Usage:
  python3 transcript-scan.py                    # Scan for new transcripts
  python3 transcript-scan.py --match EVENT_ID   # Force-match a specific event
  python3 transcript-scan.py --days-back 7      # Look back N days

The script does I/O only — fetches transcripts and matches to events.
The agent's own LLM extracts action items from the transcript content.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

WORKSPACE = os.path.expanduser("~/.clawford/meetings-coach-workspace")
CACHE_DIR = os.path.join(WORKSPACE, "cache")
PROCESSED_FILE = os.path.join(CACHE_DIR, "processed-transcripts.json")
KRISP_TOKEN_DIR = os.path.join(CACHE_DIR, "krisp-tokens")

KRISP_MCP_URL = "https://mcp.krisp.ai/mcp"


def parse_args():
    match_event = None
    days_back = 1

    for i, arg in enumerate(sys.argv):
        if arg == "--match" and i + 1 < len(sys.argv):
            match_event = sys.argv[i + 1]
        if arg == "--days-back" and i + 1 < len(sys.argv):
            days_back = int(sys.argv[i + 1])

    return match_event, days_back


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return json.load(f)
    return {"processed": []}


def save_processed(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(PROCESSED_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Krisp MCP token loading and refresh ─────────────────────────

KRISP_TOKEN_ENDPOINT = "https://api.krisp.ai/platform/v1/oauth2/token"


def load_krisp_token():
    """Load the Krisp access token from tokens.json."""
    token_path = os.path.join(KRISP_TOKEN_DIR, "tokens.json")
    if not os.path.exists(token_path):
        return None
    try:
        with open(token_path) as f:
            data = json.load(f)
        return data.get("access_token")
    except Exception:
        return None


def load_krisp_client_info():
    """Load the registered Krisp OAuth client_id and client_secret."""
    path = os.path.join(KRISP_TOKEN_DIR, "client_info.json")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("client_id"), data.get("client_secret")
    except Exception:
        return None, None


async def refresh_krisp_token():
    """Refresh the Krisp access token using the stored refresh_token.

    Saves the new tokens back to tokens.json. Returns the new access token,
    or None on failure.
    """
    import httpx

    token_path = os.path.join(KRISP_TOKEN_DIR, "tokens.json")
    if not os.path.exists(token_path):
        return None

    with open(token_path) as f:
        tokens = json.load(f)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return None

    client_id, client_secret = load_krisp_client_info()
    if not client_id or not client_secret:
        return None

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                KRISP_TOKEN_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            new_tokens = resp.json()

            # Merge and save (keep any extra fields)
            tokens.update(new_tokens)
            with open(token_path, "w") as f:
                json.dump(tokens, f, indent=2)

            return new_tokens.get("access_token")
        except Exception:
            return None


# ── MCP fetch via direct HTTP ───────────────────────────────────

async def fetch_krisp_transcripts_mcp(days_back=1):
    """Fetch recent transcripts from Krisp via direct HTTP calls to MCP.

    Bypasses the MCP SDK's OAuth flow (which breaks on token refresh) and
    uses the stored bearer token directly. The token is still valid even
    when the SDK's refresh logic fails.
    """
    try:
        import httpx
    except ImportError as e:
        return [], f"httpx not installed: {e}"

    token = load_krisp_token()
    if not token:
        return [], "No Krisp access token — copy tokens.json from Flux data/krisp_tokens/"

    transcripts = []
    error = None

    def build_headers(tok):
        return {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tok}",
        }

    headers = build_headers(token)

    async def mcp_post_with_retry(client, headers, payload, max_retries=3):
        """POST to MCP with 401 retry.

        Krisp has eventual consistency — tokens sometimes fail on edge
        servers that haven't synced yet. We retry with backoff before
        attempting a refresh. Only refresh ONCE at the end to avoid
        burning through refresh tokens (Krisp rotates them on each use).
        """
        import asyncio as _asyncio
        current_headers = headers

        for attempt in range(max_retries):
            resp = await client.post(KRISP_MCP_URL, headers=current_headers, json=payload)
            if resp.status_code != 401:
                return resp, current_headers

            # Backoff — Krisp eventual consistency usually resolves in a few seconds
            await _asyncio.sleep(1.0 + attempt * 1.0)

        # Last resort: try refreshing the token once after all retries failed
        new_token = await refresh_krisp_token()
        if new_token:
            current_headers = build_headers(new_token)
            if "Mcp-Session-Id" in headers:
                current_headers["Mcp-Session-Id"] = headers["Mcp-Session-Id"]
            resp = await client.post(KRISP_MCP_URL, headers=current_headers, json=payload)

        return resp, current_headers

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
    ) as client:
        try:
            # Step 1: Initialize MCP session
            init_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "murphy", "version": "1.0"},
                },
            }
            init_resp, headers = await mcp_post_with_retry(client, headers, init_payload)
            init_resp.raise_for_status()

            # Capture session ID from headers
            session_id = init_resp.headers.get("mcp-session-id") or init_resp.headers.get("Mcp-Session-Id")
            session_headers = dict(headers)
            if session_id:
                session_headers["Mcp-Session-Id"] = session_id

            # Step 2: Send initialized notification
            notif_payload = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            await client.post(KRISP_MCP_URL, headers=session_headers, json=notif_payload)

            # Step 3: Search for recent meetings
            since = (
                datetime.now(timezone.utc) - timedelta(days=days_back)
            ).strftime("%Y-%m-%d")

            meetings = []
            offset = 0
            next_id = 2
            while True:
                search_payload = {
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": "tools/call",
                    "params": {
                        "name": "search_meetings",
                        "arguments": {
                            "after": since,
                            "limit": 50,
                            "offset": offset,
                            "fields": [
                                "name", "date", "url", "speakers",
                                "attendees", "key_points",
                                "action_items",
                            ],
                        },
                    },
                }
                next_id += 1
                search_resp, session_headers = await mcp_post_with_retry(client, session_headers, search_payload)
                search_resp.raise_for_status()
                batch = _extract_meetings_from_sse(search_resp.text)
                if not batch:
                    break
                meetings.extend(batch)
                if len(batch) < 50:
                    break
                offset += len(batch)

            # Step 4: Fetch transcript text for each meeting
            for meeting in meetings:
                doc_id = meeting.get("meeting_id")
                if not doc_id:
                    continue

                transcript = {
                    "id": f"krisp_mcp_{doc_id}",
                    "title": meeting.get("name", ""),
                    "date": meeting.get("date", ""),
                    # Krisp-supplied deep-link URL (https://app.krisp.ai/t/<id>).
                    # Preferred by build_transcript_data over any constructed URL.
                    "url": str(meeting.get("url", "") or ""),
                    "participants": list(dict.fromkeys(
                        meeting.get("speakers", [])
                        + meeting.get("attendees", [])
                    )),
                    # ORDERED speakers list — required to resolve
                    # {{Speaker_N}} placeholders in action_items. Keep
                    # separate from ``participants`` (deduped combined).
                    "speakers": list(meeting.get("speakers", []) or []),
                    "key_points": (meeting.get("meeting_notes") or {}).get("key_points", []),
                    "action_items": (meeting.get("meeting_notes") or {}).get("action_items", []),
                    "text": "",
                    "source": "krisp_mcp",
                }

                if not transcript["key_points"]:
                    transcript["key_points"] = meeting.get("key_points", [])
                if not transcript["action_items"]:
                    transcript["action_items"] = meeting.get("action_items", [])

                # Fetch full transcript text
                try:
                    doc_payload = {
                        "jsonrpc": "2.0",
                        "id": next_id,
                        "method": "tools/call",
                        "params": {
                            "name": "get_multiple_documents",
                            "arguments": {"ids": [doc_id]},
                        },
                    }
                    next_id += 1
                    doc_resp, session_headers = await mcp_post_with_retry(client, session_headers, doc_payload)
                    doc_resp.raise_for_status()
                    raw_text = _extract_text_from_sse(doc_resp.text)
                    if raw_text and len(raw_text.strip()) >= 50:
                        transcript["text"] = raw_text
                except Exception:
                    pass

                transcripts.append(transcript)

        except Exception as e:
            error = str(e)

    return transcripts, error


def _parse_sse_results(sse_text):
    """Parse Server-Sent Events format and return the list of MCP content items."""
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
    """Parse search_meetings SSE response into a list of meeting dicts."""
    content = _parse_sse_results(sse_text)
    if not content:
        return []

    # Build a mock response object for the existing parser
    class MockContent:
        def __init__(self, text):
            self.text = text

    class MockResult:
        def __init__(self, content):
            self.content = [MockContent(c.get("text", "")) for c in content if c.get("type") == "text"]

    # Also check for structuredContent which has a clean JSON array
    for line in sse_text.splitlines():
        if line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
                structured = data.get("result", {}).get("structuredContent", {})
                if "meetings" in structured:
                    return structured["meetings"]
            except (json.JSONDecodeError, KeyError):
                continue

    # Fall back to text parsing
    return _extract_meetings_from_response(MockResult(content))


def _extract_text_from_sse(sse_text):
    """Extract raw transcript text from get_multiple_documents SSE response."""
    content = _parse_sse_results(sse_text)
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            if text.startswith("Retrieved document "):
                newline = text.find("\n")
                if newline != -1:
                    return text[newline + 1:].strip()
            return text
    return ""


def _extract_meetings_from_response(call_result):
    """Parse the search_meetings MCP response into a list of meeting dicts.

    Krisp MCP returns meetings in two possible formats:
    1. A header block ("Found N meeting(s)...") followed by individual
       content blocks, each with markdown like "## Title (date)\\nmeeting_id: ...\\nspeakers: ..."
    2. A JSON array embedded in text (older format)
    """
    meetings = []

    for content in call_result.content:
        if not hasattr(content, "text"):
            continue
        text = content.text.strip()

        # Skip the header block
        if text.startswith("Found ") and "meeting(s)" in text:
            continue

        # Try JSON array format first
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

        # Parse structured text format: "## Title (date)\nmeeting_id: ...\nspeakers: ..."
        if "meeting_id:" in text:
            meeting: dict = {}
            lines = text.split("\n")
            for line in lines:
                line = line.strip()
                if line.startswith("## "):
                    # New meeting block — flush the previous one before
                    # overwriting its fields. Fixes multi-meeting
                    # responses where only the last was ever appended.
                    if meeting.get("meeting_id"):
                        meetings.append(meeting)
                    meeting = {}
                    # Parse "## Title (date)"
                    title_part = line[3:].strip()
                    paren_match = re.search(r"\((\d{4}-\d{2}-\d{2}T[^)]+)\)\s*$", title_part)
                    if paren_match:
                        meeting["date"] = paren_match.group(1)
                        meeting["name"] = title_part[:paren_match.start()].strip()
                    else:
                        meeting["name"] = title_part
                elif line.startswith("meeting_id:"):
                    meeting["meeting_id"] = line.split(":", 1)[1].strip()
                elif line.startswith("url:"):
                    meeting["url"] = line.split(":", 1)[1].strip()
                elif line.startswith("speakers:"):
                    speakers_str = line.split(":", 1)[1].strip()
                    meeting["speakers"] = [s.strip() for s in speakers_str.split(",") if s.strip()]
                elif line.startswith("attendees:"):
                    attendees_str = line.split(":", 1)[1].strip()
                    meeting["attendees"] = [a.strip() for a in attendees_str.split(",") if a.strip()]
                elif line.startswith("key_points:"):
                    meeting["key_points"] = [line.split(":", 1)[1].strip()]
                elif line.startswith("action_items:"):
                    meeting["action_items"] = [line.split(":", 1)[1].strip()]

            if meeting.get("meeting_id"):
                meetings.append(meeting)

    return meetings


def _extract_text_from_response(call_result):
    """Extract raw text from a get_document MCP response."""
    for content in call_result.content:
        if hasattr(content, "text"):
            text = content.text
            if text.startswith("Retrieved document "):
                newline = text.find("\n")
                if newline != -1:
                    return text[newline + 1:].strip()
            return text
    return ""


# ── Local fallback ──────────────────────────────────────────────

def scan_local_directory(hours_back=24):
    """Fallback: scan a local directory for transcript files."""
    dirs_to_check = [
        os.path.expanduser("~/Dropbox/krisp-transcripts"),
        os.path.join(WORKSPACE, "transcripts"),
    ]

    transcripts = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    for d in dirs_to_check:
        if not os.path.isdir(d):
            continue

        for filename in os.listdir(d):
            filepath = os.path.join(d, filename)
            if not os.path.isfile(filepath):
                continue
            if not filename.endswith((".txt", ".md", ".json")):
                continue

            mtime = datetime.fromtimestamp(os.path.getmtime(filepath), tz=timezone.utc)
            if mtime < cutoff:
                continue

            with open(filepath) as f:
                content = f.read()

            if filename.endswith(".json"):
                try:
                    data = json.loads(content)
                    transcripts.append({
                        "id": f"file_{filename}",
                        "title": data.get("title", data.get("meeting_title", filename)),
                        "date": data.get("date", mtime.isoformat()),
                        "participants": data.get("participants", []),
                        "text": data.get("text", data.get("transcript", content)),
                        "key_points": data.get("key_points", []),
                        "action_items": data.get("action_items", []),
                        "source": "file",
                    })
                except json.JSONDecodeError:
                    pass
            else:
                transcripts.append({
                    "id": f"file_{filename}",
                    "title": filename.replace(".txt", "").replace(".md", ""),
                    "date": mtime.isoformat(),
                    "participants": [],
                    "text": content,
                    "key_points": [],
                    "action_items": [],
                    "source": "file",
                })

    return transcripts


# ── Matching ────────────────────────────────────────────────────

def match_transcript_to_events(transcript, events):
    """Match a transcript to calendar events using fuzzy scoring."""
    best_event = None
    best_score = 0.0

    transcript_date = None
    if transcript.get("date"):
        try:
            transcript_date = datetime.fromisoformat(
                transcript["date"].replace("Z", "+00:00")
            )
        except ValueError:
            pass

    transcript_title = (transcript.get("title") or "").lower().strip()
    transcript_participants = set()
    for p in transcript.get("participants", []):
        if isinstance(p, str):
            transcript_participants.add(p.lower())
            if " " in p:
                transcript_participants.add(p.split()[0].lower())

    for event in events:
        score = 0.0

        if transcript_date:
            event_start = event.get("start", "")
            try:
                event_date = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
                # Ensure both are offset-aware for comparison
                if transcript_date.tzinfo is None:
                    transcript_date = transcript_date.replace(tzinfo=timezone.utc)
                if event_date.tzinfo is None:
                    event_date = event_date.replace(tzinfo=timezone.utc)
                diff_seconds = abs((transcript_date - event_date).total_seconds())
                if diff_seconds <= 3600:
                    score += 0.4 * (1.0 - diff_seconds / 3600.0)
            except ValueError:
                pass

        event_title = (event.get("summary") or "").lower().strip()
        if transcript_title and event_title:
            ratio = SequenceMatcher(None, transcript_title, event_title).ratio()
            score += 0.3 * ratio

        event_participants = set()
        for att in event.get("attendees", []):
            name = (att.get("name") or "").lower()
            email = (att.get("email") or "").lower()
            if name:
                event_participants.add(name)
                if " " in name:
                    event_participants.add(name.split()[0])
            if email:
                event_participants.add(email.split("@")[0])

        if transcript_participants and event_participants:
            overlap = len(transcript_participants & event_participants)
            total = max(len(transcript_participants), len(event_participants))
            if total > 0:
                score += 0.3 * (overlap / total)

        if score > best_score:
            best_score = score
            best_event = event

    if best_event and best_score >= 0.4:
        return best_event, best_score
    return None, 0.0


_FIRST_TURN_RE = re.compile(
    r"\*\*[A-Z][a-zA-Z \-']+\s*\|\s*\d{1,2}:\d{2}\*\*"
)
_SECTION_END_RE = re.compile(
    r"#{2,3}\s+\w|\*\*[A-Z][a-zA-Z \-']+\s*\|\s*\d{1,2}:\d{2}\*\*"
)
# Maximum transcript body stored and fed to the coaching LLM. Covers a
# full 4hr meeting at typical dialogue density (~16K chars/hr). Token
# cost at 64K chars ≈ 16K tokens — trivial on current models.
_TRANSCRIPT_CAP_CHARS = 64000


def _transcript_body(raw: str) -> str:
    """Anchor stored transcript_text at the first speaker-turn marker.
    Krisp's document interleaves notes (Action Items / Key Points) with
    the transcript section; header text varies across meeting types
    ('### Transcript', '## Transcript', '## Transcript 1', none).
    The first ``**Name | MM:SS**`` marker is the reliable cut point."""
    m = _FIRST_TURN_RE.search(raw or "")
    if m:
        return raw[m.start():]
    return raw or ""


def _parse_doc_bullets(raw: str, section_name: str) -> list:
    """Pull the bulleted items out of a '### {section_name}' block in a
    Krisp document. Stops at the next Markdown header or the first
    speaker turn so dialogue lines starting with '-' don't pollute
    the list. Returns a list of trimmed strings (may be empty)."""
    if not raw or not section_name:
        return []
    header_re = re.compile(
        rf"#{{2,3}}\s*{re.escape(section_name)}\s*",
        re.IGNORECASE,
    )
    m = header_re.search(raw)
    if not m:
        return []
    start = m.end()
    end_m = _SECTION_END_RE.search(raw[start:])
    end = start + end_m.start() if end_m else len(raw)
    bullets: list = []
    for line in raw[start:end].split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            text = stripped[2:].strip()
            if text:
                bullets.append(text)
    return bullets


def build_transcript_data(transcript):
    """Build transcript data for the agent to process.

    Krisp's ``search_meetings`` response sometimes leaves
    ``meeting_notes.action_items`` and ``meeting_notes.key_points``
    empty even when the full document (from ``get_multiple_documents``)
    carries rich '### Action Items' and '### Key Points' Markdown
    sections — 2026-04-16 Steven Oliver had 0 key_points in metadata
    but 4 substantive bullets in the doc. Fall back to parsing the
    doc when metadata is empty; preserve the structured items from
    metadata when it is populated (they carry {title, assignee,
    completed}, richer than the stringified doc form)."""
    raw = transcript.get("text", "") or ""
    body = _transcript_body(raw)

    action_items = transcript.get("action_items") or []
    if not action_items:
        action_items = _parse_doc_bullets(raw, "Action Items")

    key_points = transcript.get("key_points") or []
    if not key_points:
        key_points = _parse_doc_bullets(raw, "Key Points")

    # Krisp's web URL for the debrief keyboard's "See more" button.
    # Prefer the ``url`` Krisp's MCP returns on each meeting
    # (confirmed shape: https://app.krisp.ai/t/<doc_id>). Fall back
    # to constructing the same /t/<doc_id> path when the API response
    # omits url. The earlier /meetings/<id> guess returned HTTP 200
    # but landed on the app shell, not the specific meeting.
    krisp_url = str(transcript.get("url", "") or "").strip()
    if not krisp_url:
        raw_id = str(transcript.get("id", "") or "")
        doc_id = (
            raw_id[len("krisp_mcp_"):]
            if raw_id.startswith("krisp_mcp_")
            else raw_id
        )
        krisp_url = f"https://app.krisp.ai/t/{doc_id}" if doc_id else ""

    return {
        "krisp_key_points": key_points,
        "krisp_action_items": action_items,
        "krisp_speakers": list(transcript.get("speakers", []) or []),
        "transcript_text": body[:_TRANSCRIPT_CAP_CHARS],
        "participants": transcript.get("participants", []),
        "krisp_meeting_url": krisp_url,
    }


def stage_debrief(event_id, event, transcript, transcript_data):
    """Stage transcript data for the agent to process and present to Sam."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    staging_path = os.path.join(CACHE_DIR, f"pending-debrief-{event_id}.json")

    data = {
        "event_id": event_id,
        "meeting_title": event.get("summary", ""),
        "meeting_start": event.get("start", ""),
        "attendees": [{"name": a.get("name", ""), "email": a.get("email", "")} for a in event.get("attendees", [])],
        "transcript_id": transcript.get("id", ""),
        "transcript_source": transcript.get("source", ""),
        "krisp_key_points": transcript_data.get("krisp_key_points", []),
        "krisp_action_items": transcript_data.get("krisp_action_items", []),
        "krisp_speakers": transcript_data.get("krisp_speakers", []),
        "krisp_meeting_url": transcript_data.get("krisp_meeting_url", ""),
        "transcript_text": transcript_data.get("transcript_text", ""),
        "participants": transcript_data.get("participants", []),
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_review",
    }

    with open(staging_path, "w") as f:
        json.dump(data, f, indent=2)

    return data


def main():
    match_event_id, days_back = parse_args()

    # Fetch transcripts via MCP
    transcripts, mcp_error = asyncio.run(fetch_krisp_transcripts_mcp(days_back=days_back))

    if mcp_error:
        print(json.dumps({"warning": f"Krisp MCP: {mcp_error}"}), file=sys.stderr)

    # Fallback to local directory
    if not transcripts:
        local = scan_local_directory(hours_back=days_back * 24)
        transcripts.extend(local)

    # Filter already-processed
    processed_data = load_processed()
    processed_ids = set(processed_data.get("processed", []))
    new_transcripts = [t for t in transcripts if t.get("id") not in processed_ids]

    if not new_transcripts:
        print(json.dumps({
            "status": "ok",
            "processed": [],
            "unmatched": [],
            "mcp_error": mcp_error,
            "message": "No new transcripts found",
        }, indent=2))
        return

    # Load recent events
    events = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    for date in [today, yesterday]:
        cache_path = os.path.join(CACHE_DIR, f"events-{date}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                data = json.load(f)
            events.extend(data.get("events", []))

    if match_event_id:
        events = [e for e in events if e.get("id") == match_event_id]

    processed_results = []
    unmatched = []
    errors = []

    for transcript in new_transcripts:
        matched_event, score = match_transcript_to_events(transcript, events)

        if not matched_event:
            unmatched.append({
                "transcript_id": transcript.get("id"),
                "title": transcript.get("title"),
                "date": transcript.get("date"),
            })
            continue

        event_id = matched_event.get("id", "")

        try:
            transcript_data = build_transcript_data(transcript)
            stage_debrief(event_id, matched_event, transcript, transcript_data)

            processed_results.append({
                "transcript_id": transcript.get("id"),
                "matched_event_id": event_id,
                "matched_event_title": matched_event.get("summary", ""),
                "match_score": round(score, 2),
                "has_krisp_extractions": bool(
                    transcript_data.get("krisp_key_points")
                    or transcript_data.get("krisp_action_items")
                ),
                "has_raw_text": bool(transcript_data.get("transcript_text")),
                "status": "staged_for_review",
            })

            processed_ids.add(transcript.get("id"))

        except Exception as e:
            errors.append({
                "transcript_id": transcript.get("id"),
                "error": str(e),
            })

    processed_data["processed"] = list(processed_ids)
    save_processed(processed_data)

    print(json.dumps({
        "status": "ok",
        "processed": processed_results,
        "unmatched": unmatched,
        "errors": errors,
        "mcp_error": mcp_error,
    }, indent=2))


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
