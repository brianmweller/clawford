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

WORKSPACE = os.path.expanduser("~/.openclaw/meetings-coach-workspace")
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


# ── Krisp MCP OAuth token storage ───────────────────────────────

class FileTokenStorage:
    """Simple file-based OAuth token storage matching the MCP SDK protocol."""

    def __init__(self, token_dir):
        self._dir = token_dir
        os.makedirs(token_dir, exist_ok=True)

    @property
    def _tokens_path(self):
        return os.path.join(self._dir, "tokens.json")

    @property
    def _client_info_path(self):
        return os.path.join(self._dir, "client_info.json")

    async def get_tokens(self):
        if not os.path.exists(self._tokens_path):
            return None
        try:
            from mcp.shared.auth import OAuthToken
            with open(self._tokens_path) as f:
                data = json.load(f)
            return OAuthToken.model_validate(data)
        except Exception:
            return None

    async def set_tokens(self, tokens):
        with open(self._tokens_path, "w") as f:
            f.write(tokens.model_dump_json(indent=2))

    async def get_client_info(self):
        if not os.path.exists(self._client_info_path):
            return None
        try:
            from mcp.shared.auth import OAuthClientInformationFull
            with open(self._client_info_path) as f:
                data = json.load(f)
            return OAuthClientInformationFull.model_validate(data)
        except Exception:
            return None

    async def set_client_info(self, client_info):
        with open(self._client_info_path, "w") as f:
            f.write(client_info.model_dump_json(indent=2))

    def has_tokens(self):
        return os.path.exists(self._tokens_path)


# ── MCP fetch ───────────────────────────────────────────────────

async def fetch_krisp_transcripts_mcp(days_back=1):
    """Fetch recent transcripts from Krisp via the MCP protocol."""
    try:
        import httpx
        from mcp import ClientSession
        from mcp.client.auth.oauth2 import OAuthClientProvider
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata
    except ImportError as e:
        return [], f"MCP SDK not installed: {e}"

    storage = FileTokenStorage(KRISP_TOKEN_DIR)
    if not storage.has_tokens():
        return [], "No Krisp OAuth tokens — copy from Flux data/krisp_tokens/"

    client_metadata = OAuthClientMetadata(
        redirect_uris=["http://localhost:19823/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="Sergeant Murphy (Krisp Connector)",
        scope="read",
    )

    auth_provider = OAuthClientProvider(
        server_url=KRISP_MCP_URL,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=None,
        callback_handler=None,
        timeout=300.0,
    )

    http_client = httpx.AsyncClient(
        auth=auth_provider,
        timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
    )

    transcripts = []
    error = None

    try:
        async with streamable_http_client(
            KRISP_MCP_URL, http_client=http_client
        ) as (read_stream, write_stream, _get_sid):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                # Search for recent meetings
                since = (
                    datetime.now(timezone.utc) - timedelta(days=days_back)
                ).strftime("%Y-%m-%d")

                meetings = []
                offset = 0
                while True:
                    resp = await session.call_tool(
                        "search_meetings",
                        arguments={
                            "after": since,
                            "limit": 50,
                            "offset": offset,
                            "fields": [
                                "name", "date", "speakers",
                                "attendees", "key_points",
                                "action_items",
                            ],
                        },
                    )
                    batch = _extract_meetings_from_response(resp)
                    if not batch:
                        break
                    meetings.extend(batch)
                    if len(batch) < 50:
                        break
                    offset += len(batch)

                # Fetch transcript text for each meeting
                for meeting in meetings:
                    doc_id = meeting.get("meeting_id")
                    if not doc_id:
                        continue

                    transcript = {
                        "id": f"krisp_mcp_{doc_id}",
                        "title": meeting.get("name", ""),
                        "date": meeting.get("date", ""),
                        "participants": list(dict.fromkeys(
                            meeting.get("speakers", [])
                            + meeting.get("attendees", [])
                        )),
                        "key_points": (meeting.get("meeting_notes") or {}).get("key_points", []),
                        "action_items": (meeting.get("meeting_notes") or {}).get("action_items", []),
                        "text": "",
                        "source": "krisp_mcp",
                    }

                    # Also use top-level key_points/action_items if present
                    if not transcript["key_points"]:
                        transcript["key_points"] = meeting.get("key_points", [])
                    if not transcript["action_items"]:
                        transcript["action_items"] = meeting.get("action_items", [])

                    # Fetch full transcript text
                    try:
                        doc_resp = await session.call_tool(
                            "get_multiple_documents",
                            arguments={"ids": [doc_id]},
                        )
                        raw_text = _extract_text_from_response(doc_resp)
                        if raw_text and len(raw_text.strip()) >= 50:
                            transcript["text"] = raw_text
                    except Exception:
                        pass  # Include without full text

                    transcripts.append(transcript)

    except Exception as e:
        error = str(e)
    finally:
        await http_client.aclose()

    return transcripts, error


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
            meeting = {}
            lines = text.split("\n")
            for line in lines:
                line = line.strip()
                if line.startswith("## "):
                    # Parse "## Title (date)"
                    title_part = line[3:].strip()
                    # Extract date from parentheses at end
                    paren_match = re.search(r"\((\d{4}-\d{2}-\d{2}T[^)]+)\)\s*$", title_part)
                    if paren_match:
                        meeting["date"] = paren_match.group(1)
                        meeting["name"] = title_part[:paren_match.start()].strip()
                    else:
                        meeting["name"] = title_part
                elif line.startswith("meeting_id:"):
                    meeting["meeting_id"] = line.split(":", 1)[1].strip()
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


def build_transcript_data(transcript):
    """Build transcript data for the agent to process."""
    return {
        "krisp_key_points": transcript.get("key_points", []),
        "krisp_action_items": transcript.get("action_items", []),
        "transcript_text": (transcript.get("text", "") or "")[:8000],
        "participants": transcript.get("participants", []),
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
    main()
