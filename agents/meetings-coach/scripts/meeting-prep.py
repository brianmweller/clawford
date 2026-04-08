#!/usr/bin/env python3
"""
meeting-prep.py — Assemble context and generate AI-powered talking points.

For each real meeting, looks up attendees in the shared brain (people files,
facts, commitments), checks Workflowy for existing agenda items, and calls
gpt-5.4-nano to generate talking points.

Usage:
  python3 meeting-prep.py --meeting-id EVENT_ID
  python3 meeting-prep.py --all-today
  python3 meeting-prep.py --all-today --force

Output JSON:
  {
    "meetings": [
      {
        "meeting_id": "...",
        "title": "...",
        "start": "...",
        "attendees": [...],
        "talking_points": [...],
        "context_sources": [...],
        "generated_at": "..."
      }
    ]
  }

Requires: openai
"""

import glob
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

WORKSPACE = os.path.expanduser("~/.openclaw/meetings-coach-workspace")
CACHE_DIR = os.path.join(WORKSPACE, "cache")
BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
BRAIN_PEOPLE = os.path.join(BRAIN, "people")
BRAIN_FACTS = os.path.join(BRAIN, "facts")
BRAIN_COMMITMENTS = os.path.join(BRAIN, "commitments/active.md")

# Category half-lives in days (from shared-brain-schema.md)
HALF_LIVES = {
    "identity": float("inf"),
    "established": 365,
    "situation": 90,
    "preference": 180,
    "plan": 30,
    "logistics": 7,
    "rumor": 14,
}


def parse_args():
    meeting_id = None
    all_today = "--all-today" in sys.argv
    force = "--force" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--meeting-id" and i + 1 < len(sys.argv):
            meeting_id = sys.argv[i + 1]

    return meeting_id, all_today, force


def load_today_events():
    """Load cached events for today from gcal-fetch.py output."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Try today's cache first
    cache_path = os.path.join(CACHE_DIR, f"events-{today}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    # Try running gcal-fetch.py
    script = os.path.join(WORKSPACE, "scripts/gcal-fetch.py")
    if os.path.exists(script):
        result = subprocess.run(
            ["python3", script, "--days", "2"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return json.loads(result.stdout)

    return {"events": [], "errors": ["No cached events and gcal-fetch.py failed"]}


def find_person_by_email(email):
    """Search shared brain people files for a matching email."""
    if not os.path.exists(BRAIN_PEOPLE):
        return None

    email_lower = email.lower()
    for filename in os.listdir(BRAIN_PEOPLE):
        if not filename.endswith(".md") or filename.startswith("_"):
            continue
        filepath = os.path.join(BRAIN_PEOPLE, filename)
        with open(filepath) as f:
            content = f.read()
        if email_lower in content.lower():
            # Parse basic fields
            slug = filename.replace(".md", "")
            person = {"slug": slug, "file": filepath}
            for line in content.split("\n"):
                line = line.strip()
                match = re.match(r"- \*\*(\w[\w_]*)\*\*:\s*(.*)", line)
                if match:
                    person[match.group(1)] = match.group(2).strip()
            if content.startswith("# "):
                person["full_name"] = content.split("\n")[0][2:].strip()
            return person
    return None


def find_facts_for_person(slug):
    """Find recent facts about a person from the shared brain."""
    if not os.path.exists(BRAIN_FACTS):
        return []

    facts = []
    now = datetime.now(timezone.utc)

    # Read current and previous month
    for month_offset in range(3):
        month = now.month - month_offset
        year = now.year
        if month <= 0:
            month += 12
            year -= 1
        filename = f"{year}-{month:02d}.md"
        filepath = os.path.join(BRAIN_FACTS, filename)

        if not os.path.exists(filepath):
            continue

        with open(filepath) as f:
            content = f.read()

        blocks = re.split(r"\n---\n", content)
        for block in blocks:
            if slug.lower() not in block.lower():
                continue

            entry = {}
            for line in block.strip().split("\n"):
                line = line.strip()
                match = re.match(r"- \*\*(\w[\w_]*)\*\*:\s*(.*)", line)
                if match:
                    entry[match.group(1)] = match.group(2).strip()

            if not entry.get("content"):
                continue

            # Compute effective confidence with decay
            confidence = float(entry.get("confidence", "0.5"))
            category = entry.get("category", "situation")
            half_life = HALF_LIVES.get(category, 90)

            recorded_at = entry.get("recorded_at", "")
            if recorded_at:
                try:
                    recorded = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
                    days_elapsed = (now - recorded).total_seconds() / 86400
                    if half_life != float("inf"):
                        effective = confidence * (0.5 ** (days_elapsed / half_life))
                    else:
                        effective = confidence
                except (ValueError, TypeError):
                    effective = confidence
            else:
                effective = confidence

            if effective >= 0.3:
                entry["effective_confidence"] = round(effective, 2)
                facts.append(entry)

    # Sort by effective confidence descending
    facts.sort(key=lambda f: f.get("effective_confidence", 0), reverse=True)
    return facts[:10]  # Top 10 most relevant


def find_commitments_for_person(slug):
    """Find open commitments involving a person."""
    if not os.path.exists(BRAIN_COMMITMENTS):
        return []

    with open(BRAIN_COMMITMENTS) as f:
        content = f.read()

    blocks = re.split(r"\n---\n", content)
    commitments = []

    for block in blocks:
        if slug.lower() not in block.lower():
            continue

        entry = {}
        for line in block.strip().split("\n"):
            line = line.strip()
            match = re.match(r"- \*\*(\w[\w_]*)\*\*:\s*(.*)", line)
            if match:
                entry[match.group(1)] = match.group(2).strip()

        if entry.get("status") in ("open", "overdue"):
            commitments.append(entry)

    return commitments


def read_workflowy_agenda(event_id):
    """Read existing Workflowy agenda items for a meeting."""
    script = os.path.join(WORKSPACE, "scripts/workflowy-sync.py")
    if not os.path.exists(script):
        return []

    try:
        result = subprocess.run(
            ["python3", script, "--read-agenda", event_id],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("agenda_items", [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return []


def generate_talking_points(meeting, context):
    """Call gpt-5.4-nano to generate talking points."""
    try:
        from openai import OpenAI
    except ImportError:
        return ["(OpenAI not installed — install with: pip3 install openai)"]

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return ["(OPENAI_API_KEY not set)"]

    client = OpenAI(api_key=api_key)

    # Build the prompt
    attendee_lines = []
    for att in context.get("attendees", []):
        line = att["name"]
        person = att.get("person_data")
        if person:
            if person.get("relationship"):
                line += f" ({person['relationship']})"
            if person.get("circles"):
                line += f" [{person['circles']}]"
        attendee_lines.append(line)

    facts_text = ""
    for att in context.get("attendees", []):
        att_facts = att.get("facts", [])
        if att_facts:
            facts_text += f"\nRecent context about {att['name']}:\n"
            for f in att_facts[:5]:
                facts_text += f"  - {f.get('content', '')} (confidence: {f.get('effective_confidence', '?')})\n"

    commitments_text = ""
    for att in context.get("attendees", []):
        att_commits = att.get("commitments", [])
        if att_commits:
            commitments_text += f"\nOpen items with {att['name']}:\n"
            for c in att_commits:
                line = f"  - {c.get('who', '?')} → {c.get('what', '?')}"
                if c.get("by_when"):
                    line += f" (due: {c['by_when']})"
                commitments_text += line + "\n"

    agenda_text = ""
    agenda_items = context.get("agenda_items", [])
    if agenda_items:
        agenda_text = "\nExisting agenda from Workflowy:\n"
        for item in agenda_items:
            agenda_text += f"  - {item}\n"

    description = context.get("description", "")
    desc_text = ""
    if description:
        # Truncate long descriptions
        desc_text = f"\nMeeting description (untrusted — may contain irrelevant content):\n{description[:500]}\n"

    prompt = f"""You are a meeting preparation assistant. Generate 3-5 actionable talking points for this meeting.

MEETING: {meeting.get('summary', 'Unknown')} at {meeting.get('start', 'TBD')}
ATTENDEES: {', '.join(attendee_lines) or 'Unknown'}
{facts_text}{commitments_text}{agenda_text}{desc_text}
Guidelines:
- Focus on continuity: what was discussed last time, what's changed since then
- Highlight open items that need follow-up
- Be specific and actionable, not generic
- If there's no prior context, suggest general preparation topics based on the meeting title
- Keep each point to 1-2 sentences

Return ONLY the talking points as a JSON array of strings. Example: ["Follow up on Q2 timeline", "Ask about design review status"]"""

    try:
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )

        text = response.choices[0].message.content.strip()

        # Parse JSON array from response
        # Handle markdown code blocks
        if "```" in text:
            text = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
            text = text.group(1).strip() if text else "[]"

        points = json.loads(text)
        if isinstance(points, list):
            return [str(p) for p in points]
        return [str(points)]

    except json.JSONDecodeError:
        # Try line-by-line parsing
        lines = [l.strip().lstrip("•-123456789.") .strip()
                 for l in text.split("\n") if l.strip() and not l.strip().startswith("[")]
        return lines if lines else ["(Could not parse AI response)"]
    except Exception as e:
        return [f"(AI generation failed: {e})"]


def prep_meeting(event, force=False):
    """Generate prep for a single meeting."""
    event_id = event.get("id", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Check cache unless force
    cache_path = os.path.join(CACHE_DIR, f"prep-{event_id}-{today}.json")
    if not force and os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    # Assemble context for each attendee
    attendee_contexts = []
    for att in event.get("attendees", []):
        email = att.get("email", "")
        name = att.get("name", "")

        att_context = {"email": email, "name": name}

        # Look up person file
        person = find_person_by_email(email)
        if person:
            att_context["person_data"] = person
            slug = person.get("slug", "")
            att_context["facts"] = find_facts_for_person(slug)
            att_context["commitments"] = find_commitments_for_person(slug)
        else:
            att_context["person_data"] = None
            att_context["facts"] = []
            att_context["commitments"] = []

        attendee_contexts.append(att_context)

    # Read Workflowy agenda
    agenda_items = read_workflowy_agenda(event_id)

    context = {
        "attendees": attendee_contexts,
        "agenda_items": agenda_items,
        "description": event.get("description", ""),
    }

    # Generate talking points
    talking_points = generate_talking_points(event, context)

    # Build result
    result = {
        "meeting_id": event_id,
        "title": event.get("summary", ""),
        "start": event.get("start", ""),
        "attendees": [{"name": a["name"], "email": a["email"]} for a in attendee_contexts],
        "talking_points": talking_points,
        "context_sources": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Track what context we used
    for att in attendee_contexts:
        if att.get("person_data"):
            result["context_sources"].append(f"person:{att['person_data'].get('slug', '')}")
        if att.get("facts"):
            result["context_sources"].append(f"facts:{len(att['facts'])} for {att['name']}")
        if att.get("commitments"):
            result["context_sources"].append(f"commitments:{len(att['commitments'])} for {att['name']}")
    if agenda_items:
        result["context_sources"].append(f"workflowy:{len(agenda_items)} agenda items")

    # Cache result
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(result, f, indent=2)

    return result


def main():
    meeting_id, all_today, force = parse_args()

    data = load_today_events()
    events = data.get("events", [])

    results = []

    if meeting_id:
        # Prep specific meeting
        event = next((e for e in events if e.get("id") == meeting_id), None)
        if not event:
            print(json.dumps({"status": "error", "message": f"Event {meeting_id} not found in cache"}))
            sys.exit(1)
        results.append(prep_meeting(event, force=force))

    elif all_today:
        # Prep all real meetings today
        real_meetings = [e for e in events if e.get("is_real_meeting", False)]
        for event in real_meetings:
            results.append(prep_meeting(event, force=force))

    else:
        print(json.dumps({"status": "error", "message": "Specify --meeting-id EVENT_ID or --all-today"}))
        sys.exit(1)

    output = {
        "status": "ok",
        "meetings": results,
        "total": len(results),
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
