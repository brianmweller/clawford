#!/usr/bin/env python3
"""
commitment-scan.py — Read and filter ALL commitments from the shared brain.

Unlike Sergeant Murphy's commitment-tracker.py which filters for
source_agent=meetings-coach, this reads ALL commitments for a unified view.

Usage:
  python3 commitment-scan.py                        # All open items
  python3 commitment-scan.py --person SLUG           # Filter by person
  python3 commitment-scan.py --overdue-only          # Only overdue items
  python3 commitment-scan.py --source-agent AGENT    # Filter by source agent

Output JSON:
  {
    "commitments": [...],
    "summary": {"total": N, "open": N, "overdue": N, "approaching": N}
  }
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

BRAIN_COMMITMENTS = os.path.expanduser("~/Dropbox/openclaw-backup/commitments/active.md")


def parse_args():
    person = None
    overdue_only = "--overdue-only" in sys.argv
    source_agent = None

    for i, arg in enumerate(sys.argv):
        if arg == "--person" and i + 1 < len(sys.argv):
            person = sys.argv[i + 1]
        if arg == "--source-agent" and i + 1 < len(sys.argv):
            source_agent = sys.argv[i + 1]

    return person, overdue_only, source_agent


def parse_commitments(filepath):
    """Parse commitment entries from the shared brain markdown file."""
    if not os.path.exists(filepath):
        return []

    with open(filepath) as f:
        content = f.read()

    if not content.strip():
        return []

    # Split on --- dividers
    blocks = re.split(r"\n---\n", content)
    commitments = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        entry = {}
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("- **") and ":**" in line:
                match = re.match(r"- \*\*(\w[\w_]*)\*\*:\s*(.*)", line)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip()
                    if value == "—" or value == "":
                        value = None
                    entry[key] = value

        if entry.get("id"):
            commitments.append(entry)

    return commitments


def main():
    person, overdue_only, source_agent = parse_args()

    commitments = parse_commitments(BRAIN_COMMITMENTS)

    # Filter for open/overdue status
    commitments = [c for c in commitments if c.get("status") in ("open", "overdue")]

    # Filter by source agent if specified
    if source_agent:
        commitments = [c for c in commitments if c.get("source_agent") == source_agent]

    # Filter by person if specified
    if person:
        person_lower = person.lower()
        commitments = [
            c for c in commitments
            if person_lower in (c.get("who", "").lower())
            or person_lower in (c.get("to_whom", "").lower())
        ]

    now = datetime.now(timezone.utc).date()
    enriched = []

    for c in commitments:
        by_when = c.get("by_when")
        days_info = None
        is_overdue = False
        is_approaching = False

        if by_when:
            try:
                deadline = datetime.strptime(by_when, "%Y-%m-%d").date()
                delta = (deadline - now).days
                if delta < 0:
                    is_overdue = True
                    days_info = f"{abs(delta)} days overdue"
                    c["status"] = "overdue"
                elif delta <= 2:
                    is_approaching = True
                    days_info = f"{delta} days left"
                else:
                    days_info = f"{delta} days left"
            except ValueError:
                pass

        enriched.append({
            **c,
            "days_info": days_info,
            "is_overdue": is_overdue,
            "is_approaching": is_approaching,
        })

    # Apply overdue-only filter
    if overdue_only:
        enriched = [c for c in enriched if c["is_overdue"]]

    # Sort: overdue first, then approaching, then by deadline
    enriched.sort(key=lambda c: (
        not c["is_overdue"],
        not c["is_approaching"],
        c.get("by_when") or "9999",
    ))

    summary = {
        "total": len(enriched),
        "open": sum(1 for c in enriched if c.get("status") == "open"),
        "overdue": sum(1 for c in enriched if c["is_overdue"]),
        "approaching": sum(1 for c in enriched if c["is_approaching"]),
    }

    result = {
        "status": "ok",
        "commitments": enriched,
        "summary": summary,
    }

    print(json.dumps(result, indent=2))


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
