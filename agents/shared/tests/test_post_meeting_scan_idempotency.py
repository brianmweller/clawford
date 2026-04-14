"""Tests for the post-meeting-scan cron message delivery invariant.

Background — 2026-04-14 incident: Sergeant Murphy re-sent the same
meeting debrief + coaching message five times across 2.5 hours for a
single Google Meet with Alexis Lloyd. Same thing had silently happened
on 2026-04-08 to a "the operator / Ashley" debrief. Root cause: the cron
prompt told the LLM to iterate `cache/pending-debrief-*.json` and
present each one whose `event_id` was not yet in
`commitments/active.md`. So as long as the user hadn't `/confirm`-ed,
every 30-minute scan re-composed a fresh debrief from the pending
file and re-ran coaching, re-appending to `coaching-history.json`.

The correct model is: `transcript-scan.py` is the single source of
truth for "what to deliver". Its `processed` output lists only
freshly-staged transcripts (deduped at the script level via
`processed-transcripts.json`). Pending files are the `/confirm`
staging area and MUST NOT be treated as a delivery queue.

This test file enforces that invariant on the real
agents/meetings-coach/manifest.json so a future regression can't
silently re-introduce the bug.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "agents" / "meetings-coach" / "manifest.json"


@pytest.fixture(scope="module")
def post_meeting_scan_message() -> str:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for cron in data["crons"]:
        if cron["name"] == "post-meeting-scan":
            return cron["message"]
    pytest.fail("post-meeting-scan cron not found in meetings-coach manifest")


def test_delivery_source_is_transcript_scan_output(post_meeting_scan_message: str):
    """The prompt must name `transcript-scan.py` output as the sole
    delivery source. The LLM must not be told to decide what to send
    by walking pending-debrief-*.json files."""
    msg = post_meeting_scan_message

    # Must include the explicit scoping rule.
    assert "ONLY" in msg and "transcript-scan" in msg, (
        "post-meeting-scan prompt must scope delivery to transcript-scan.py "
        "output explicitly (the word ONLY plus transcript-scan)"
    )

    # Must explicitly forbid treating pending-debrief files as a queue.
    forbid_patterns = [
        r"NOT\s+iterate.*pending-debrief",
        r"NEVER\s+iterate.*pending-debrief",
        r"pending-debrief.*not.*delivery queue",
    ]
    matched = any(re.search(p, msg, re.IGNORECASE) for p in forbid_patterns)
    assert matched, (
        "post-meeting-scan prompt must explicitly forbid iterating "
        "cache/pending-debrief-*.json files to decide what to send. "
        "See 2026-04-14 Alexis Lloyd re-send incident."
    )


def test_coaching_history_dedup_before_append(post_meeting_scan_message: str):
    """The coaching step must check coaching-history.json for an
    existing entry before appending. Otherwise a re-run (for any
    reason) appends a duplicate record, as happened 5x on 2026-04-14
    for the Alexis event and 5x on 2026-04-08 for the Ashley event."""
    msg = post_meeting_scan_message
    dedup_patterns = [
        r"coaching-history\.json.*already",
        r"already.*coaching-history\.json",
        r"skip.*coaching-history",
        r"coaching-history.*skip",
    ]
    matched = any(re.search(p, msg, re.IGNORECASE | re.DOTALL) for p in dedup_patterns)
    assert matched, (
        "post-meeting-scan prompt must tell the LLM to check "
        "coaching-history.json for an existing entry (by event_id) "
        "before running metrics + appending. See 2026-04-14 duplicate "
        "coaching incident."
    )
