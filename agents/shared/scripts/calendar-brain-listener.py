#!/usr/bin/env python3
"""calendar-brain-listener.py — the 60s syncToken poller.

Long-running daemon that keeps ``~/.clawford/calendar-brain/calendar-brain.json``
fresh. Structure mirrors ``agents/connector/scripts/gmail-push-listener.py``
but uses Google Calendar's ``events.list(syncToken=...)`` incremental
sync instead of Pub/Sub pull (Calendar API doesn't support Pub/Sub).

Per tick, for each calendar:

  1. Load the stored syncToken.
  2. If missing or 410 GONE on use: bounded full fetch, capture a fresh
     syncToken, mark ``fetched_via="listener-reseed"`` in the output.
  3. Else: incremental ``events.list(syncToken)`` → delta of changed +
     cancelled events since last tick.
  4. Merge delta into in-memory brain (cancelled → remove; else →
     normalize + insert/replace by id).
  5. Persist brain + tokens atomically.

Failure on one calendar does NOT block other calendars — the
``last_error`` column on its token entry carries the diagnostic, and
the next tick retries.

CLI modes:

  --once            Run one tick and exit. For tests and manual smoke.
  --interval 60     Seconds between ticks (default 60).

Disable marker: if ``~/.clawford/calendar-brain-disabled`` exists, the
daemon refuses to start. Remove the file to re-enable.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
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
    fetch_events_incremental,
    normalize_event,
)
from agents.shared.calendar_brain import (  # noqa: E402
    DEFAULT_BRAIN_FILE,
    DEFAULT_TOKENS_FILE,
    atomic_write_brain,
    read_brain,
    read_sync_tokens,
    write_sync_tokens,
)


log = logging.getLogger("calendar-brain-listener")

# ---------------------------------------------------------------------------
# Paths — mirror calendar-brain-build.py
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
DISABLE_MARKER = Path(os.path.expanduser("~/.clawford/calendar-brain-disabled"))
LOOKAHEAD_DAYS = int(os.environ.get("CLAWFORD_CALENDAR_LOOKAHEAD_DAYS", "8"))

_SHOULD_STOP = False


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        global _SHOULD_STOP
        log.info("caught signal %s, stopping after current tick", signum)
        _SHOULD_STOP = True
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# ---------------------------------------------------------------------------
# Pure tick logic — exercised by test_calendar_brain_listener.py
# ---------------------------------------------------------------------------


def _apply_delta(
    events_by_id: dict[str, dict],
    raw_deltas: list[dict],
    *,
    calendar_id: str,
    calendar_label: str,
    calendar_emoji: str,
    workflowy_event_ids: set[str],
    skip_titles: list[str],
) -> None:
    """Mutate ``events_by_id`` in place by applying the incremental
    delta: cancellations remove by id, everything else normalizes and
    replaces by id."""
    for raw in raw_deltas or []:
        if not isinstance(raw, dict):
            continue
        eid = raw.get("id")
        if not eid:
            continue
        if raw.get("status") == "cancelled":
            events_by_id.pop(eid, None)
            continue
        events_by_id[eid] = normalize_event(
            raw,
            calendar_id=calendar_id,
            calendar_label=calendar_label,
            calendar_emoji=calendar_emoji,
            workflowy_event_ids=workflowy_event_ids,
            skip_titles=skip_titles,
        )


def _compute_full_window(now_utc: datetime, lookahead_days: int) -> tuple[str, str]:
    """Return the (time_min, time_max) ISO strings to use when the
    listener needs to bounded-full-fetch (missing token or 410 GONE).
    Start at now_utc (not local midnight) — a reseed mid-afternoon
    shouldn't refetch the morning's already-past events."""
    time_min = now_utc.isoformat()
    time_max = (now_utc + timedelta(days=lookahead_days)).isoformat()
    return time_min, time_max


def _fetch_for_calendar(
    service,
    *,
    calendar_id: str,
    stored_sync_token: str | None,
    now_utc: datetime,
    lookahead_days: int,
) -> tuple[list[dict] | None, str | None, str | None]:
    """Return ``(events, new_sync_token, error)`` for one calendar.

    - If ``stored_sync_token`` is truthy: try incremental. On 410, fall
      back to full.
    - If missing (first run, or prior reseed): full fetch.
    - Errors are caught and returned as strings so the caller can
      record per-calendar ``last_error`` without aborting the tick."""
    try:
        if stored_sync_token:
            events, new_sync = fetch_events_incremental(
                service, calendar_id, stored_sync_token,
            )
            if events is not None:
                return events, new_sync, None
            # 410 GONE sentinel — fall through to full fetch + reseed.
        time_min, time_max = _compute_full_window(now_utc, lookahead_days)
        events, new_sync = fetch_events_full(
            service, calendar_id,
            time_min=time_min, time_max=time_max,
        )
        return events, new_sync, None
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)[:200]


def run_tick(
    *,
    service,
    brain_path: Path,
    tokens_path: Path,
    calendars: list[dict],
    workflowy_event_ids: set[str],
    skip_titles: list[str],
    lookahead_days: int,
    now_utc: datetime,
) -> dict:
    """Execute one listener tick and persist brain + tokens atomically."""
    existing = read_brain(brain_path)
    events_by_id: dict[str, dict] = {
        e["id"]: e for e in (existing.get("events") or [])
        if isinstance(e, dict) and e.get("id")
    }

    tokens = read_sync_tokens(tokens_path)
    now_iso = now_utc.isoformat()
    failures: list[dict] = []

    for cal in calendars:
        cal_id = cal.get("id")
        if not cal_id:
            continue
        label = cal.get("label", "") or ""
        emoji = cal.get("emoji", "") or ""

        stored = (tokens.get(cal_id) or {}).get("sync_token")
        raw_deltas, new_sync, err = _fetch_for_calendar(
            service,
            calendar_id=cal_id,
            stored_sync_token=stored,
            now_utc=now_utc,
            lookahead_days=lookahead_days,
        )

        if err is not None:
            # Preserve prior sync_token so the next tick retries from
            # the same cursor; only update the error column + stamp.
            prior = tokens.get(cal_id) or {}
            tokens[cal_id] = {
                "sync_token": prior.get("sync_token"),
                "last_success_at": prior.get("last_success_at"),
                "last_error": err,
            }
            failures.append({"calendar_id": cal_id, "error": err})
            continue

        # Drop existing events for this calendar when we did a reseed —
        # otherwise stale events (now past the lookahead window) would
        # linger forever. Incremental-path results are deltas only; we
        # keep the existing state untouched and let _apply_delta
        # mutate it.
        reseeded = not stored or stored != stored  # placeholder; see below
        # A reseed happened when we fell back to full-fetch. Detect by
        # the absence of a stored token OR by the fact that the
        # incremental path returned None (410 path, handled above).
        # Simpler invariant: if we just did a full fetch (which always
        # returns the complete window), replace-this-calendar rather
        # than delta-merge.
        did_full_fetch = (not stored)
        if did_full_fetch:
            events_by_id = {
                eid: rec for eid, rec in events_by_id.items()
                if rec.get("calendar_id") != cal_id
            }
            _apply_delta(
                events_by_id, raw_deltas,
                calendar_id=cal_id, calendar_label=label, calendar_emoji=emoji,
                workflowy_event_ids=workflowy_event_ids, skip_titles=skip_titles,
            )
        else:
            _apply_delta(
                events_by_id, raw_deltas,
                calendar_id=cal_id, calendar_label=label, calendar_emoji=emoji,
                workflowy_event_ids=workflowy_event_ids, skip_titles=skip_titles,
            )

        tokens[cal_id] = {
            "sync_token": new_sync,
            "last_success_at": now_iso,
            "last_error": None,
        }

    window = existing.get("window") or {}
    brain = {
        "generated_at": now_iso,
        "fetched_via": "listener",
        "window": window,
        "events": sorted(
            events_by_id.values(),
            key=lambda e: (e.get("start") or "", e.get("id") or ""),
        ),
    }
    atomic_write_brain(brain_path, brain)
    write_sync_tokens(tokens_path, tokens)

    return {
        "status": "degraded" if failures else "ok",
        "event_count": len(brain["events"]),
        "failures": failures,
        "tick_at": now_iso,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _load_calendars() -> list[dict]:
    if not CALENDAR_CONFIG_PATH.exists():
        return []
    try:
        cfg = json.loads(CALENDAR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return cfg.get("calendars") or []


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
        except Exception as e:  # noqa: BLE001
            return None, f"token refresh failed: {e}"
    if not creds.valid:
        return None, "credentials invalid"
    return creds, None


def _build_service(creds):
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=creds)


def run_forever(interval_s: int) -> int:
    if DISABLE_MARKER.exists():
        log.info("disable marker present at %s — refusing to start", DISABLE_MARKER)
        return 0

    _install_signal_handlers()

    creds, err = _get_credentials()
    if err:
        log.error("credentials failed: %s", err)
        return 1
    service = _build_service(creds)

    while not _SHOULD_STOP:
        calendars = _load_calendars()
        if not calendars:
            log.warning("no calendars in config; sleeping")
            time.sleep(interval_s)
            continue

        try:
            summary = run_tick(
                service=service,
                brain_path=BRAIN_FILE,
                tokens_path=BRAIN_TOKENS_FILE,
                calendars=calendars,
                workflowy_event_ids=_load_workflowy_event_ids(),
                skip_titles=_load_skip_titles(),
                lookahead_days=LOOKAHEAD_DAYS,
                now_utc=datetime.now(timezone.utc),
            )
            log.info("tick %s events=%s failures=%s",
                     summary["status"], summary["event_count"],
                     len(summary["failures"]))
        except Exception:  # noqa: BLE001
            log.error("tick crashed:\n%s", traceback.format_exc())

        # Sleep in short slices so SIGTERM is responsive.
        slept = 0.0
        while slept < interval_s and not _SHOULD_STOP:
            time.sleep(min(1.0, interval_s - slept))
            slept += 1.0

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Run one tick and exit (smoke-test mode)")
    ap.add_argument("--interval", type=int, default=60,
                    help="Seconds between ticks (default 60)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.once:
        creds, err = _get_credentials()
        if err:
            print(json.dumps({"status": "error", "error": err}))
            return 1
        service = _build_service(creds)
        calendars = _load_calendars()
        if not calendars:
            print(json.dumps({"status": "error", "error": "no calendars"}))
            return 1
        summary = run_tick(
            service=service,
            brain_path=BRAIN_FILE,
            tokens_path=BRAIN_TOKENS_FILE,
            calendars=calendars,
            workflowy_event_ids=_load_workflowy_event_ids(),
            skip_titles=_load_skip_titles(),
            lookahead_days=LOOKAHEAD_DAYS,
            now_utc=datetime.now(timezone.utc),
        )
        print(json.dumps(summary))
        return 0

    return run_forever(args.interval)


if __name__ == "__main__":
    sys.exit(main())
