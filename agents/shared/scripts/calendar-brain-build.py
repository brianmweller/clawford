#!/usr/bin/env python3
"""calendar-brain-build.py — daily full rebuild of the shared event brain.

Runs once per morning tick (10:25 UTC) ahead of the 10:30 UTC agent
morning-briefings AND as the belt-and-suspenders safety net under the
``calendar-brain-listener`` daemon. Fetches the next ``LOOKAHEAD_DAYS``
of events from every calendar in family-calendar's ``calendar-config.json``
(Mouse's token has the broadest access), normalizes each event via
``calendar_fetch.normalize_event``, and writes two files:

  1. ``~/.clawford/calendar-brain/calendar-brain.json`` — the new
     canonical brain, with full event records (attendees, description,
     conference_link, is_meeting, owner, is_real_meeting).
  2. ``~/Dropbox/openclaw-backup/status/calendar-index.json`` — the
     legacy thin-index shape, kept in lockstep through the migration
     so anything still reading the old path continues to work.

Also persists syncTokens to ``calendar-brain-tokens.json``; this resets
the listener's incremental cursor on every daily rebuild so we never
drift too far from a full refetch.

This script replaces ``agents/family-calendar/scripts/calendar-index-build.py``;
once all callers are migrated the old path can be deleted.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.calendar_fetch import (  # noqa: E402
    fetch_events_full,
    normalize_event,
)
from agents.shared.calendar_brain import (  # noqa: E402
    DEFAULT_BRAIN_FILE,
    DEFAULT_TOKENS_FILE,
    atomic_write_brain,
    write_sync_tokens,
)

# ---------------------------------------------------------------------------
# Paths + knobs
# ---------------------------------------------------------------------------

FAMILY_WORKSPACE = Path(os.path.expanduser("~/.clawford/family-calendar-workspace"))
MURPHY_WORKSPACE = Path(os.path.expanduser("~/.clawford/meetings-coach-workspace"))

CALENDAR_CONFIG_PATH = Path(os.environ.get(
    "CLAWFORD_CALENDAR_CONFIG_PATH",
    str(FAMILY_WORKSPACE / "calendar-config.json"),
))
MURPHY_CONFIG_PATH = Path(os.environ.get(
    "CLAWFORD_MURPHY_CONFIG_PATH",
    str(MURPHY_WORKSPACE / "meeting-config.json"),
))
TOKEN_PATH = Path(os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    str(FAMILY_WORKSPACE / "token.json"),
))
WORKFLOWY_LINKS_PATH = Path(os.environ.get(
    "WORKFLOWY_LINKS_PATH",
    str(MURPHY_WORKSPACE / "cache" / "workflowy-links.json"),
))
BRAIN_FILE = Path(os.environ.get(
    "CLAWFORD_CALENDAR_BRAIN_FILE", DEFAULT_BRAIN_FILE,
))
BRAIN_TOKENS_FILE = Path(os.environ.get(
    "CLAWFORD_CALENDAR_BRAIN_TOKENS_FILE", DEFAULT_TOKENS_FILE,
))
LEGACY_INDEX_PATH = Path(os.environ.get(
    "CLAWFORD_CALENDAR_INDEX_PATH",
    os.path.expanduser("~/Dropbox/openclaw-backup/status/calendar-index.json"),
))

LOOKAHEAD_DAYS = int(os.environ.get("CLAWFORD_CALENDAR_LOOKAHEAD_DAYS", "8"))


# ---------------------------------------------------------------------------
# Pure helpers — exercised by test_calendar_brain_build.py
# ---------------------------------------------------------------------------


def build_brain_payload(
    *,
    raw_by_cal: dict[str, dict],
    workflowy_event_ids: set[str],
    skip_titles: list[str],
    time_min: str,
    time_max: str,
    now_utc: datetime,
) -> tuple[dict, dict]:
    """Build the brain payload + per-calendar sync-token map.

    ``raw_by_cal`` is a dict keyed by calendar_id with entries of the
    shape::

        {
          "label": str,
          "emoji": str,
          "events": [<raw google event>, ...]      # on success
          "sync_token": str | None,                 # on success
          "error": "<string>",                      # on failure
        }

    Returns ``(brain_payload, tokens_payload)``. ``brain_payload``
    carries deduplicated normalized events; ``tokens_payload`` maps
    each calendar to its last-success metadata.

    Dedup rule: when the same event appears on two calendars (invite
    propagation), prefer the copy flagged as a meeting (the one with
    the video link intact). Legacy ``calendar-index-build`` had the
    same guarantee — keep it."""
    events_by_id: dict[str, dict] = {}
    tokens: dict[str, dict] = {}
    success_at = now_utc.isoformat()

    for cal_id, cal in raw_by_cal.items():
        label = cal.get("label", "") or ""
        emoji = cal.get("emoji", "") or ""

        if cal.get("error"):
            tokens[cal_id] = {
                "sync_token": None,
                "last_success_at": None,
                "last_error": str(cal["error"])[:200],
            }
            continue

        tokens[cal_id] = {
            "sync_token": cal.get("sync_token"),
            "last_success_at": success_at,
            "last_error": None,
        }

        for raw in cal.get("events", []) or []:
            if not isinstance(raw, dict):
                continue
            if raw.get("status") == "cancelled":
                continue
            eid = raw.get("id")
            if not eid:
                continue
            rec = normalize_event(
                raw,
                calendar_id=cal_id,
                calendar_label=label,
                calendar_emoji=emoji,
                workflowy_event_ids=workflowy_event_ids,
                skip_titles=skip_titles,
            )
            existing = events_by_id.get(eid)
            if existing is None:
                events_by_id[eid] = rec
            elif not existing.get("is_meeting") and rec.get("is_meeting"):
                # Prefer the copy with the stronger classification.
                events_by_id[eid] = rec

    brain = {
        "generated_at": now_utc.isoformat(),
        "fetched_via": "daily-build",
        "window": {"time_min": time_min, "time_max": time_max},
        "events": sorted(
            events_by_id.values(),
            key=lambda e: (e.get("start") or "", e.get("id") or ""),
        ),
    }
    return brain, tokens


def build_legacy_index(brain: dict) -> dict:
    """Derive the legacy ``calendar-index.json`` shape (thin classification
    records keyed by event_id) from the new brain payload. Kept during
    migration so anything still reading the old path doesn't regress."""
    events_dict: dict[str, dict] = {}
    for rec in brain.get("events") or []:
        eid = rec.get("id")
        if not eid:
            continue
        events_dict[eid] = {
            "id": eid,
            "summary": rec.get("summary", ""),
            "start": rec.get("start", ""),
            "end": rec.get("end", ""),
            "all_day": rec.get("all_day", False),
            "calendar_id": rec.get("calendar_id", ""),
            "has_video_link": rec.get("has_video_link", False),
            "in_workflowy": rec.get("in_workflowy", False),
            "is_meeting": rec.get("is_meeting", False),
            "owner": rec.get("owner", "mistress-mouse"),
        }
    window = brain.get("window") or {}
    return {
        "generated_at": brain.get("generated_at"),
        "window": {
            "start": (window.get("time_min") or "")[:10],
            "end": (window.get("time_max") or "")[:10],
        },
        "lookahead_days": LOOKAHEAD_DAYS,
        "events": events_dict,
        "event_count": len(events_dict),
        "scanned_count": len(events_dict),
        "workflowy_link_count": sum(
            1 for r in events_dict.values() if r.get("in_workflowy")
        ),
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Orchestration (OAuth + subprocess surface — not unit-tested)
# ---------------------------------------------------------------------------


def _load_workflowy_event_ids() -> set[str]:
    if not WORKFLOWY_LINKS_PATH.exists():
        return set()
    try:
        data = json.loads(WORKFLOWY_LINKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.keys()) if isinstance(data, dict) else set()


def _load_skip_titles() -> list[str]:
    if not MURPHY_CONFIG_PATH.exists():
        return []
    try:
        cfg = json.loads(MURPHY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    filters = (cfg or {}).get("meeting_filters") or {}
    titles = filters.get("skip_titles") or []
    return [t for t in titles if isinstance(t, str)]


def _get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        return None, f"token.json not found at {TOKEN_PATH}"
    token_data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_data["token"] = creds.token
            TOKEN_PATH.write_text(
                json.dumps(token_data, indent=2), encoding="utf-8"
            )
        except Exception as e:
            return None, f"token refresh failed: {e}"
    if not creds.valid:
        return None, "credentials invalid — run gcal-auth.py"
    return creds, None


def _build_service(creds):
    from googleapiclient.discovery import build as gbuild
    return gbuild("calendar", "v3", credentials=creds)


def _fetch_all_calendars(service, config: dict, time_min: str, time_max: str) -> dict:
    """Iterate over calendar-config.json calendars, fetching each one.
    Returns the ``raw_by_cal`` dict shape expected by
    ``build_brain_payload``."""
    result: dict[str, dict] = {}
    for cal in config.get("calendars") or []:
        cal_id = cal.get("id")
        if not cal_id:
            continue
        entry = {
            "label": cal.get("label", "") or "",
            "emoji": cal.get("emoji", "") or "",
        }
        try:
            events, sync_token = fetch_events_full(
                service, cal_id,
                time_min=time_min, time_max=time_max,
            )
            entry["events"] = events
            entry["sync_token"] = sync_token
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
        result[cal_id] = entry
    return result


def _atomic_dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def run() -> dict:
    if not CALENDAR_CONFIG_PATH.exists():
        return {"status": "error", "error": f"missing {CALENDAR_CONFIG_PATH}"}
    try:
        config = json.loads(CALENDAR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"status": "error", "error": f"failed to parse calendar config: {e}"}

    creds, err = _get_credentials()
    if err:
        return {"status": "error", "error": err}

    try:
        service = _build_service(creds)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"failed to build Calendar service: {e}"}

    tz_name = config.get("timezone", "America/Los_Angeles")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc

    now_tz = datetime.now(tz)
    base = datetime(now_tz.year, now_tz.month, now_tz.day, tzinfo=tz)
    time_min = base.isoformat()
    time_max = (base + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    now_utc = datetime.now(timezone.utc)

    raw_by_cal = _fetch_all_calendars(service, config, time_min, time_max)
    workflowy_ids = _load_workflowy_event_ids()
    skip_titles = _load_skip_titles()

    brain, tokens = build_brain_payload(
        raw_by_cal=raw_by_cal,
        workflowy_event_ids=workflowy_ids,
        skip_titles=skip_titles,
        time_min=time_min,
        time_max=time_max,
        now_utc=now_utc,
    )

    atomic_write_brain(BRAIN_FILE, brain)
    write_sync_tokens(BRAIN_TOKENS_FILE, tokens)

    # Double-write legacy path so anything still reading calendar-index.json
    # doesn't regress during migration.
    _atomic_dump_json(LEGACY_INDEX_PATH, build_legacy_index(brain))

    errors = [
        {"calendar_id": cid, "error": t["last_error"]}
        for cid, t in tokens.items() if t.get("last_error")
    ]
    degraded = bool(errors)

    result = {
        "status": "degraded" if degraded else "ok",
        "event_count": len(brain.get("events", [])),
        "meeting_count": sum(
            1 for e in brain.get("events", []) if e.get("is_meeting")
        ),
        "lookahead_days": LOOKAHEAD_DAYS,
        "brain_file": str(BRAIN_FILE),
        "legacy_index_path": str(LEGACY_INDEX_PATH),
        "errors": errors,
    }
    if degraded:
        result["alert"] = (
            f"calendar-brain-build: {len(errors)} calendar(s) failed"
        )
    return result


def main() -> int:
    try:
        result = run()
    except Exception as e:  # noqa: BLE001
        result = {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc().splitlines()[-3:],
            "alert": f"calendar-brain-build crashed: {e}",
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
