#!/usr/bin/env python3
"""
meeting-prep.py — Assemble meeting context from the shared brain.

For each real meeting, looks up attendees in the shared brain (people files,
facts, commitments) and checks Workflowy for existing agenda items. Outputs
context JSON for the agent to use in briefings and alerts.

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
        "context": { "facts": [...], "commitments": [...], "agenda_items": [...] },
        "context_sources": [...],
        "generated_at": "..."
      }
    ]
  }

The script does I/O only — assembles context from shared brain and Workflowy.
The agent's own LLM generates talking points from this context.
"""

import glob
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- shared library sys.path shim (pattern from pre-meeting-alert.py) ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.scan_fields import scan_fields  # noqa: E402

WORKSPACE = os.path.expanduser("~/.clawford/meetings-coach-workspace")
CACHE_DIR = os.path.join(WORKSPACE, "cache")
BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
BRAIN_PEOPLE = os.path.join(BRAIN, "people")
BRAIN_FACTS = os.path.join(BRAIN, "facts")
BRAIN_COMMITMENTS = os.path.join(BRAIN, "commitments/active.md")

# Category half-lives in days (from ops/brain/README.md)
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


def prep_meeting(event, force=False):
    """Generate prep for a single meeting."""
    event_id = event.get("id", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Check cache unless force
    cache_path = os.path.join(CACHE_DIR, f"prep-{event_id}-{today}.json")
    if not force and os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    # P0.4: Scan externally-sourced calendar fields before they enter
    # any downstream prompt or agent context. Calendar invites are the
    # canonical "Invitation Is All You Need" attack vector — a malicious
    # invite can carry an injection in its description or title that
    # the agent would otherwise read verbatim during a chat session.
    # Default mode is `warn` (observation-only), flip to `enforce` via
    # CLAWFORD_INBOUND_SCANNER_MODE once the warn stream stabilizes.
    raw_attendees = event.get("attendees", []) or []
    scan_input: dict[str, str | None] = {
        "title": event.get("summary", ""),
        "description": (event.get("description") or "")[:500],
    }
    for i, att in enumerate(raw_attendees):
        if isinstance(att, dict):
            scan_input[f"attendee_{i}_name"] = att.get("name", "")
            scan_input[f"attendee_{i}_email"] = att.get("email", "")

    sanitized_fields, scan_warnings = scan_fields(
        fields=scan_input,
        source_type="calendar",
        source_id=event_id,
        workspace=Path(WORKSPACE),
    )

    # Assemble context for each attendee — use sanitized name/email so
    # downstream agent context never sees a blocked value in enforce
    # mode. In warn mode the values are pass-through.
    attendee_contexts = []
    for i, att in enumerate(raw_attendees):
        if not isinstance(att, dict):
            continue
        email = sanitized_fields.get(f"attendee_{i}_email", att.get("email", ""))
        name = sanitized_fields.get(f"attendee_{i}_name", att.get("name", ""))

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

    # Build result — context only, agent does the reasoning. Calendar-
    # sourced fields (title, description) flow through scan_fields()
    # output so downstream LLM consumers see sanitized text in enforce
    # mode and the warnings show up under `scan_warnings` in both modes.
    result = {
        "meeting_id": event_id,
        "title": sanitized_fields.get("title", event.get("summary", "")),
        "start": event.get("start", ""),
        "end": event.get("end", ""),
        "attendees": [],
        "context": {
            "facts": [],
            "commitments": [],
            "agenda_items": agenda_items,
            "description": sanitized_fields.get("description", ""),
        },
        "context_sources": [],
        "scan_warnings": scan_warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Build attendee details with their context
    for att in attendee_contexts:
        att_entry = {"name": att["name"], "email": att["email"]}

        person = att.get("person_data")
        if person:
            att_entry["relationship"] = person.get("relationship", "")
            att_entry["circles"] = person.get("circles", "")
            att_entry["slug"] = person.get("slug", "")
            result["context_sources"].append(f"person:{person.get('slug', '')}")

        result["attendees"].append(att_entry)

        for fact in att.get("facts", []):
            result["context"]["facts"].append({
                "about": att["name"],
                "content": fact.get("content", ""),
                "confidence": fact.get("effective_confidence", 0),
                "category": fact.get("category", ""),
            })

        for commit in att.get("commitments", []):
            result["context"]["commitments"].append({
                "who": commit.get("who", ""),
                "to_whom": commit.get("to_whom", ""),
                "what": commit.get("what", ""),
                "by_when": commit.get("by_when"),
                "status": commit.get("status", ""),
            })

    if result["context"]["facts"]:
        result["context_sources"].append(f"facts:{len(result['context']['facts'])}")
    if result["context"]["commitments"]:
        result["context_sources"].append(f"commitments:{len(result['context']['commitments'])}")
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
