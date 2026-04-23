"""agents/shared/calendar_brain.py — shared event cache (the "brain").

One writer (the listener daemon or the daily rebuild), many readers
(both agents' tools + scripts). Readers never write.

File layout::

    ~/.clawford/calendar-brain/
      calendar-brain.json           # events + metadata
      calendar-brain-tokens.json    # per-calendar syncToken state
      lock                          # O_EXCL lock (writer-only)

Brain file shape::

    {
      "generated_at": "<ISO UTC>",
      "fetched_via": "listener" | "daily-build",
      "window": {"time_min": "...", "time_max": "..."},
      "events": [ <normalized event>, ... ]  # as produced by calendar_fetch.normalize_event
    }

Token file shape::

    {
      "<calendar_id>": {
        "sync_token": "<str>",
        "last_success_at": "<ISO UTC>",
        "last_error": null | "<short message>",
      },
      ...
    }

All readers degrade-open: missing file → empty payload, malformed JSON
→ empty payload. The first-run state (no brain yet) looks the same as
a temporarily-missing brain, so callers don't need to special-case it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


# Canonical paths — callers can override via env vars for tests /
# alternate workspaces. Don't expand eagerly at import; compute in
# helpers so monkey-patching in tests is straightforward.
DEFAULT_BRAIN_DIR = os.path.expanduser("~/.clawford/calendar-brain")
DEFAULT_BRAIN_FILE = os.path.join(DEFAULT_BRAIN_DIR, "calendar-brain.json")
DEFAULT_TOKENS_FILE = os.path.join(
    DEFAULT_BRAIN_DIR, "calendar-brain-tokens.json"
)


def _empty_payload() -> dict:
    return {
        "events": [],
        "generated_at": None,
        "fetched_via": None,
        "window": None,
    }


def _load_json(path) -> dict | None:
    """Degrade-open JSON load. Returns None on any failure so callers
    can distinguish 'not there' from 'not JSON-parseable' if they want
    to, though in practice both collapse to the empty payload."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _atomic_dump_json(path, payload: dict) -> None:
    """Write JSON via tmp-file + os.replace so readers never see a
    partially-written file. Creates parent dir if missing."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Brain file
# ---------------------------------------------------------------------------


def atomic_write_brain(path, payload: dict) -> None:
    """Write the brain atomically. Payload must carry at least ``events``;
    other fields (``generated_at``, ``fetched_via``, ``window``) are
    optional but recommended — readers surface them for provenance."""
    if not isinstance(payload, dict):
        raise TypeError(f"brain payload must be dict, got {type(payload)}")
    payload = {
        "events": payload.get("events") or [],
        "generated_at": payload.get("generated_at"),
        "fetched_via": payload.get("fetched_via"),
        "window": payload.get("window"),
    }
    _atomic_dump_json(path, payload)


def read_brain(
    path,
    *,
    owner: str | None = None,
    date: str | None = None,
    days: int | None = None,
) -> dict:
    """Load the brain, optionally filtering events by owner and/or date
    range. Always returns the full payload shape; ``events`` is a list
    (possibly empty).

    Filter semantics:

      - ``owner="sergeant-murphy"``: events where ``owner == "sergeant-murphy"``
        AND ``is_real_meeting == True``. Enforces Murphy's display
        convention that skip_titles-matched items (Focus Time, Lunch,
        Block) are hidden even when they carry a video link.
      - ``owner="mistress-mouse"``: events where ``owner == "mistress-mouse"``.
        No skip_titles filter — those are Murphy's display preference,
        not Mouse's.
      - ``date`` + ``days``: keep events whose ``start`` (as ISO string)
        falls in the PT-day range ``[date, date + days)``. Works with
        both dateTime and all-day ``date`` starts because lexicographic
        comparison on ISO strings is correct for day-granularity."""
    raw = _load_json(path)
    if raw is None:
        return _empty_payload()

    events = raw.get("events") or []
    if not isinstance(events, list):
        events = []

    if owner == "sergeant-murphy":
        events = [
            e for e in events
            if isinstance(e, dict)
            and e.get("owner") == "sergeant-murphy"
            and e.get("is_real_meeting") is True
        ]
    elif owner == "mistress-mouse":
        events = [
            e for e in events
            if isinstance(e, dict) and e.get("owner") == "mistress-mouse"
        ]

    if date is not None:
        # Interpret date as a YYYY-MM-DD bound. days defaults to 1
        # when date is supplied but days is omitted.
        days = 1 if days is None else days
        from datetime import date as _date
        from datetime import timedelta as _timedelta
        start = _date.fromisoformat(date)
        end = start + _timedelta(days=days)
        start_s = start.isoformat()
        end_s = end.isoformat()
        events = [
            e for e in events
            if isinstance(e, dict)
            and start_s <= (e.get("start") or "")[:10] < end_s
        ]

    return {
        "events": events,
        "generated_at": raw.get("generated_at"),
        "fetched_via": raw.get("fetched_via"),
        "window": raw.get("window"),
    }


# ---------------------------------------------------------------------------
# Sync tokens
# ---------------------------------------------------------------------------


def read_sync_tokens(path) -> dict[str, dict]:
    """Load the per-calendar syncToken state. Missing/malformed → {}."""
    raw = _load_json(path)
    if raw is None:
        return {}
    return raw


def write_sync_tokens(path, tokens: dict[str, dict]) -> None:
    """Persist the per-calendar syncToken state atomically."""
    if not isinstance(tokens, dict):
        raise TypeError(
            f"sync_tokens payload must be dict, got {type(tokens)}"
        )
    _atomic_dump_json(path, tokens)


# ---------------------------------------------------------------------------
# Reader helpers for agent tools
# ---------------------------------------------------------------------------


DEFAULT_DISABLE_MARKER = os.path.expanduser(
    "~/.clawford/calendar-brain-read-disabled"
)


def read_brain_if_fresh(
    path,
    *,
    owner: str | None = None,
    date: str | None = None,
    days: int | None = None,
    max_age_seconds: int = 600,
    disable_marker: str | None = None,
    now=None,
) -> dict | None:
    """Load the brain and return the filtered payload IF it's fresh.

    Returns ``None`` when any of the following hold, so callers can
    cleanly fall back to their subprocess path:

      1. The disable marker file is present (the operator's instant-rollback
         lever — ``~/.clawford/calendar-brain-read-disabled``).
      2. The brain file is missing or empty.
      3. The brain's ``generated_at`` is older than ``max_age_seconds``
         (default 10 min — 10x the listener's 60s tick).

    ``now`` is injectable for tests. In production callers pass nothing
    and the helper uses ``datetime.now(timezone.utc)``."""
    marker = disable_marker if disable_marker is not None else DEFAULT_DISABLE_MARKER
    if marker and os.path.exists(marker):
        return None

    payload = read_brain(path, owner=owner, date=date, days=days)
    if not payload.get("events") and not payload.get("generated_at"):
        # Missing / malformed / empty brain. Force fallback.
        return None

    generated_at = payload.get("generated_at")
    if not generated_at:
        return None

    from datetime import datetime as _dt, timezone as _tz
    try:
        gen = _dt.fromisoformat(generated_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=_tz.utc)

    current = now or _dt.now(_tz.utc)
    age_s = (current - gen).total_seconds()
    if age_s > max_age_seconds:
        return None

    return payload
