#!/usr/bin/env python3
"""
transcript-scan.py — Fetch Krisp transcripts and match to calendar meetings.

Connects to Krisp's MCP OAuth API to fetch recent transcripts, matches them
to recently-ended calendar events using fuzzy scoring, and extracts action
items via gpt-5.4-nano for Sam's review.

Usage:
  python3 transcript-scan.py                    # Scan for new transcripts
  python3 transcript-scan.py --match EVENT_ID   # Force-match a specific event

Output JSON:
  {
    "status": "ok",
    "processed": [...],
    "unmatched": [...],
    "errors": []
  }

Krisp MCP auth uses OAuth 2.1 — run transcript-scan.py --auth for initial setup.
Tokens stored in cache/krisp-token.json.

Requires: openai
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import urllib.request
import urllib.error

WORKSPACE = os.path.expanduser("~/.openclaw/meetings-coach-workspace")
CACHE_DIR = os.path.join(WORKSPACE, "cache")
PROCESSED_FILE = os.path.join(CACHE_DIR, "processed-transcripts.json")
KRISP_TOKEN_FILE = os.path.join(CACHE_DIR, "krisp-token.json")

KRISP_MCP_URL = "https://mcp.krisp.ai/mcp"


def parse_args():
    match_event = None
    do_auth = "--auth" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--match" and i + 1 < len(sys.argv):
            match_event = sys.argv[i + 1]

    return match_event, do_auth


def load_processed():
    """Load list of already-processed transcript IDs."""
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return json.load(f)
    return {"processed": []}


def save_processed(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(PROCESSED_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_krisp_token():
    """Load stored Krisp OAuth token."""
    if not os.path.exists(KRISP_TOKEN_FILE):
        return None
    with open(KRISP_TOKEN_FILE) as f:
        data = json.load(f)
    return data.get("access_token")


def fetch_krisp_transcripts(hours_back=4):
    """Fetch recent transcripts from Krisp MCP API.

    This calls the Krisp MCP search_meetings tool to list recent meetings
    and then fetches transcript text for each.

    If Krisp MCP is not configured, falls back to checking a local directory.
    """
    token = get_krisp_token()
    transcripts = []

    if token:
        # Try MCP API
        try:
            transcripts = _fetch_via_mcp(token, hours_back)
        except Exception as e:
            print(json.dumps({"warning": f"Krisp MCP fetch failed: {e}"}), file=sys.stderr)

    if not transcripts:
        # Fallback: check local transcript directory
        transcripts = _scan_local_directory(hours_back)

    return transcripts


def _fetch_via_mcp(token, hours_back):
    """Fetch transcripts via Krisp MCP API."""
    # Call search_meetings
    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # MCP tool call: search_meetings
    payload = json.dumps({
        "method": "tools/call",
        "params": {
            "name": "search_meetings",
            "arguments": {
                "query": "",
                "days_back": max(1, hours_back // 24 + 1),
            }
        }
    }).encode("utf-8")

    req = urllib.request.Request(KRISP_MCP_URL, data=payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read().decode("utf-8"))

    meetings = []
    if isinstance(data, dict):
        content = data.get("result", {}).get("content", [])
        for item in content:
            if item.get("type") == "text":
                try:
                    meetings = json.loads(item.get("text", "[]"))
                except json.JSONDecodeError:
                    pass

    transcripts = []
    for meeting in meetings:
        meeting_id = meeting.get("meeting_id", "")
        if not meeting_id:
            continue

        transcript = {
            "id": f"krisp_mcp_{meeting_id}",
            "title": meeting.get("name", ""),
            "date": meeting.get("date", ""),
            "participants": meeting.get("speakers", []) + meeting.get("attendees", []),
            "key_points": meeting.get("key_points", []),
            "action_items": meeting.get("action_items", []),
            "text": "",  # Will be fetched via get_document if needed
            "source": "krisp_mcp",
        }

        # If Krisp already extracted key_points and action_items, use them
        if transcript["key_points"] or transcript["action_items"]:
            transcripts.append(transcript)
            continue

        # Otherwise, fetch full document text
        try:
            doc_payload = json.dumps({
                "method": "tools/call",
                "params": {
                    "name": "get_document",
                    "arguments": {"meeting_id": meeting_id}
                }
            }).encode("utf-8")

            doc_req = urllib.request.Request(KRISP_MCP_URL, data=doc_payload, headers=headers)
            doc_resp = urllib.request.urlopen(doc_req, timeout=30)
            doc_data = json.loads(doc_resp.read().decode("utf-8"))

            doc_content = doc_data.get("result", {}).get("content", [])
            for item in doc_content:
                if item.get("type") == "text":
                    transcript["text"] = item.get("text", "")
                    break

            transcripts.append(transcript)
            time.sleep(0.5)  # Rate limiting
        except Exception:
            transcripts.append(transcript)  # Still include without full text

    return transcripts


def _scan_local_directory(hours_back):
    """Fallback: scan a local directory for transcript files."""
    # Check common locations
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

            # Check modification time
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


def match_transcript_to_events(transcript, events):
    """Match a transcript to calendar events using fuzzy scoring.

    Scoring: date proximity (0.4), title similarity (0.3), participant overlap (0.3).
    Returns (best_event, score) or (None, 0).
    """
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

        # Date proximity (0.4 weight)
        if transcript_date:
            event_start = event.get("start", "")
            try:
                event_date = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
                diff_seconds = abs((transcript_date - event_date).total_seconds())
                if diff_seconds <= 3600:
                    score += 0.4 * (1.0 - diff_seconds / 3600.0)
            except ValueError:
                pass

        # Title similarity (0.3 weight)
        event_title = (event.get("summary") or "").lower().strip()
        if transcript_title and event_title:
            ratio = SequenceMatcher(None, transcript_title, event_title).ratio()
            score += 0.3 * ratio

        # Participant overlap (0.3 weight)
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


def extract_action_items(transcript, event):
    """Extract action items from a transcript using gpt-5.4-nano."""
    # If Krisp already extracted items, use them
    if transcript.get("key_points") or transcript.get("action_items"):
        return {
            "action_items": [
                {"description": item, "assignee": "unassigned", "deadline": None}
                for item in (transcript.get("action_items") or [])
            ],
            "decisions": transcript.get("key_points", []),
            "follow_ups": [],
            "facts": [],
        }

    # Otherwise, use LLM to extract from transcript text
    text = transcript.get("text", "")
    if not text:
        return {"action_items": [], "decisions": [], "follow_ups": [], "facts": []}

    try:
        from openai import OpenAI
    except ImportError:
        return {"action_items": [], "decisions": [], "follow_ups": [], "facts": [],
                "error": "openai not installed"}

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"action_items": [], "decisions": [], "follow_ups": [], "facts": [],
                "error": "OPENAI_API_KEY not set"}

    client = OpenAI(api_key=api_key)

    # Truncate transcript to stay within token limits
    truncated = text[:8000]

    attendee_names = ", ".join(
        att.get("name", att.get("email", "")) for att in event.get("attendees", [])
    )

    prompt = f"""Extract from this meeting transcript:

1. ACTION ITEMS: Who committed to doing what, by when? Format each as {{"description": str, "assignee": str, "deadline": str or null}}
2. KEY DECISIONS: What was decided? List as strings.
3. FOLLOW-UPS: What needs to happen next? List as strings.
4. NOTABLE FACTS: Any important information about people or projects worth remembering. List as strings.

Return as JSON: {{"action_items": [...], "decisions": [...], "follow_ups": [...], "facts": [...]}}

MEETING: {event.get('summary', 'Unknown')}
ATTENDEES: {attendee_names}
TRANSCRIPT (may be truncated):
{truncated}"""

    try:
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1000,
        )

        result_text = response.choices[0].message.content.strip()

        # Handle markdown code blocks
        if "```" in result_text:
            match = re.search(r"```(?:json)?\s*(.*?)```", result_text, re.DOTALL)
            result_text = match.group(1).strip() if match else result_text

        return json.loads(result_text)

    except (json.JSONDecodeError, Exception) as e:
        return {"action_items": [], "decisions": [], "follow_ups": [], "facts": [],
                "error": str(e)}


def stage_debrief(event_id, event, transcript, extractions):
    """Stage extracted items for Sam's review (pending /confirm)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    staging_path = os.path.join(CACHE_DIR, f"pending-debrief-{event_id}.json")

    data = {
        "event_id": event_id,
        "meeting_title": event.get("summary", ""),
        "meeting_start": event.get("start", ""),
        "transcript_id": transcript.get("id", ""),
        "transcript_source": transcript.get("source", ""),
        "action_items": extractions.get("action_items", []),
        "decisions": extractions.get("decisions", []),
        "follow_ups": extractions.get("follow_ups", []),
        "facts": extractions.get("facts", []),
        "staged_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_confirmation",
    }

    with open(staging_path, "w") as f:
        json.dump(data, f, indent=2)

    return data


def main():
    match_event_id, do_auth = parse_args()

    if do_auth:
        print("Krisp MCP OAuth setup:")
        print("1. Visit https://mcp.krisp.ai to get your OAuth credentials")
        print("2. Set KRISP_CLIENT_ID and KRISP_CLIENT_SECRET in .env")
        print("3. Run the OAuth flow to get an access token")
        print(f"4. Save the token to {KRISP_TOKEN_FILE}")
        sys.exit(0)

    # Fetch transcripts
    transcripts = fetch_krisp_transcripts(hours_back=4)

    # Load processed list
    processed_data = load_processed()
    processed_ids = set(processed_data.get("processed", []))

    # Filter out already-processed
    new_transcripts = [t for t in transcripts if t.get("id") not in processed_ids]

    if not new_transcripts:
        print(json.dumps({
            "status": "ok",
            "processed": [],
            "unmatched": [],
            "message": "No new transcripts found",
        }, indent=2))
        return

    # Load recent events for matching
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
            extractions = extract_action_items(transcript, matched_event)
            staged = stage_debrief(event_id, matched_event, transcript, extractions)

            processed_results.append({
                "transcript_id": transcript.get("id"),
                "matched_event_id": event_id,
                "matched_event_title": matched_event.get("summary", ""),
                "match_score": round(score, 2),
                "action_items_count": len(extractions.get("action_items", [])),
                "decisions_count": len(extractions.get("decisions", [])),
                "status": "staged_for_review",
            })

            # Mark as processed
            processed_ids.add(transcript.get("id"))

        except Exception as e:
            errors.append({
                "transcript_id": transcript.get("id"),
                "error": str(e),
            })

    # Save processed list
    processed_data["processed"] = list(processed_ids)
    save_processed(processed_data)

    print(json.dumps({
        "status": "ok",
        "processed": processed_results,
        "unmatched": unmatched,
        "errors": errors,
    }, indent=2))


if __name__ == "__main__":
    main()
