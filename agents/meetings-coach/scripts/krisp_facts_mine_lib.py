"""Pure helpers for krisp-facts-mine.py.

Handles pending-debrief JSON loading, all-hands filtering, candidate
slug derivation, and transcript chunking — all without LLM or I/O
beyond the local cache directory.
"""
from __future__ import annotations

import json
from pathlib import Path


ALL_HANDS_ATTENDEE_THRESHOLD = 6


def load_debriefs(cache_dir: Path) -> list[dict]:
    """Return the parsed contents of every pending-debrief-*.json in
    cache_dir. Malformed files are skipped silently so one bad file
    doesn't wedge the whole miner."""
    if not cache_dir.exists():
        return []
    out: list[dict] = []
    for path in sorted(cache_dir.glob("pending-debrief-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            data["_source_path"] = str(path)
            out.append(data)
    return out


def is_all_hands(debrief: dict) -> bool:
    """True when the attendee count exceeds the all-hands threshold.
    Follows daily-refresh.py: 6 is the cutoff, >6 is all-hands."""
    attendees = debrief.get("attendees") or []
    return len(attendees) > ALL_HANDS_ATTENDEE_THRESHOLD


def build_candidate_slugs(
    debrief: dict,
    *,
    email_to_slug: dict[str, str],
    operator_emails: set[str],
) -> set[str]:
    """Map attendee emails to known-person slugs, excluding the operator."""
    operator = {a.lower() for a in operator_emails}
    slugs: set[str] = set()
    for a in debrief.get("attendees") or []:
        if not isinstance(a, dict):
            continue
        email = (a.get("email") or "").strip().lower()
        if not email or email in operator:
            continue
        slug = email_to_slug.get(email)
        if slug:
            slugs.add(slug)
    return slugs


def chunk_transcript(text: str | None, *, max_chars: int) -> list[str]:
    """Split a transcript into chunks of at most max_chars. Keeps
    hard-split semantics — no boundary tricks — because Krisp
    transcripts are already speaker-labeled and the LLM just needs a
    contiguous window."""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
