#!/usr/bin/env python3
"""daily-refresh.py — Incremental last_interaction updater.

Pulls fresh signal from three VPS-visible sources and rewrites
`- **last_interaction:**` lines in ~/Dropbox/openclaw-backup/people/*.md
whenever a newer date is observed:

  1. Google Calendar events in [-14d, +14d] — attendee emails.
     Past events update last_interaction; future events are written
     to upcoming-meetings.json so people-scan can demote anyone with
     a confirmed upcoming meeting out of the overdue/approaching bucket.
  2. Gmail metadata for `newer_than:14d` — From/To/Cc headers.
  3. Meetings-coach Krisp pending-debrief JSONs — but only transcripts
     with <= 6 attendees (avoids 40-person all-hands stamping everyone).

The script does I/O only, never calls an LLM, and conforms to
SCRIPT_CONTRACT.md: bare `python3 daily-refresh.py` invocation,
always exits 0, final stdout line is a single JSON object with `status`.
"""
from __future__ import annotations

import email.utils
import importlib.util
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
MINE_DIR = SCRIPT_DIR / "mine"

BRAIN_PEOPLE = Path(os.path.expanduser("~/Dropbox/openclaw-backup/people"))
WORKSPACE = Path(os.path.expanduser("~/.clawford/connector-workspace"))
UPCOMING_CACHE = WORKSPACE / "upcoming-meetings.json"
GMESSAGES_CACHE = WORKSPACE / "cache" / "mined-gmessages.json"
MC_CACHE = Path(os.path.expanduser("~/.clawford/meetings-coach-workspace/cache"))

LOOKBACK_DAYS = 30
LOOKAHEAD_DAYS = 14
KRISP_ATTENDEE_CAP = 6
GMAIL_PAGE_SIZE = 500
GCAL_PAGE_SIZE = 250


# ── Pure helpers ─────────────────────────────────────────────────────


_FIELD_RE = re.compile(r"^-\s*\*\*(\w[\w_]*)(?::\*\*|\*\*:)\s*(.*)$")


def _parse_field(text: str, key: str) -> str | None:
    for line in text.splitlines():
        m = _FIELD_RE.match(line.strip())
        if m and m.group(1) == key:
            val = m.group(2).strip()
            if val in ("", "—"):
                return None
            return val
    return None


def _normalize_phone(phone: str | None) -> str:
    """Digits-only, strip leading US country code. Matches
    contact-aggregator.normalize_phone so the two stay in sync."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def build_phone_index(people_dir: Path) -> dict[str, tuple[Path, str | None]]:
    """Return normalized-phone → (file, last_interaction). People without
    a populated `phone:` field are simply absent from the index."""
    idx: dict[str, tuple[Path, str | None]] = {}
    if not people_dir.exists():
        return idx
    for fp in sorted(people_dir.glob("*.md")):
        if fp.name == "_template.md":
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        phone = _parse_field(text, "phone")
        key = _normalize_phone(phone)
        if not key:
            continue
        last = _parse_field(text, "last_interaction")
        idx[key] = (fp, last)
    return idx


def _read_gmessages_cache(cache_path: Path) -> list[dict]:
    """Read the gmessages cache. Returns empty list if the file doesn't
    exist (normal first-run state). Propagates OSError / JSONDecodeError
    so the caller's sources_failed tracking catches the failure — the
    old 'return [] on parse error' shape silently dropped corruption
    into empty-signal success, which is how connector gmessages
    mining could silently degrade for days."""
    if not cache_path.exists():
        return []
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    return [e for e in (data.get("contacts") or []) if isinstance(e, dict)]


def _load_gmessages_signals(cache_path: Path) -> dict[str, str]:
    """Read mined-gmessages.json → {normalized_phone: last_message_date}.
    Entries without a usable phone are skipped — name-match flows
    through _load_gmessages_by_name() instead."""
    signals: dict[str, str] = {}
    for entry in _read_gmessages_cache(cache_path):
        phone = _normalize_phone(entry.get("phone"))
        date = (entry.get("last_message_date") or "")[:10]
        if not phone or not date:
            continue
        cur = signals.get(phone)
        if not cur or date > cur:
            signals[phone] = date
    return signals


_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _load_gmessages_by_name(cache_path: Path) -> dict[str, str]:
    """Read mined-gmessages.json → {lower(name): last_message_date}.

    Google Messages displays contacts as 'Dan Zylberglejd (Netflix)' or
    'Mayra (Cleaning)' — the parenthetical is a UI hint that breaks
    name-match against the person file's plain H1. We expose BOTH the
    full name and the (...)-stripped variant so the index can match
    either spelling.
    """
    signals: dict[str, str] = {}
    for entry in _read_gmessages_cache(cache_path):
        raw_name = (entry.get("name") or "").strip()
        date = (entry.get("last_message_date") or "")[:10]
        if not raw_name or not date:
            continue
        if not any(ch.isalpha() for ch in raw_name):
            continue
        keys = {raw_name.lower()}
        stripped = _PARENS_RE.sub("", raw_name).strip()
        if stripped and stripped.lower() != raw_name.lower():
            keys.add(stripped.lower())
        for key in keys:
            cur = signals.get(key)
            if not cur or date > cur:
                signals[key] = date
    return signals


_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def build_name_index(people_dir: Path) -> dict[str, tuple[Path, str | None]]:
    """Return lowercased display name → (path, last_interaction) for
    every people/*.md with an H1 heading."""
    idx: dict[str, tuple[Path, str | None]] = {}
    if not people_dir.exists():
        return idx
    for fp in sorted(people_dir.glob("*.md")):
        if fp.name == "_template.md":
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _H1_RE.search(text)
        if not m:
            continue
        name = m.group(1).strip()
        if not name or name.startswith("{"):
            continue
        last = _parse_field(text, "last_interaction")
        idx[name.lower()] = (fp, last)
    return idx


def build_email_index(people_dir: Path) -> dict[str, tuple[Path, str | None]]:
    idx: dict[str, tuple[Path, str | None]] = {}
    if not people_dir.exists():
        return idx
    for fp in sorted(people_dir.glob("*.md")):
        if fp.name == "_template.md":
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        last = _parse_field(text, "last_interaction")
        addrs: list[str] = []
        primary = _parse_field(text, "email")
        if primary and "@" in primary:
            addrs.append(primary.lower())
        alts = _parse_field(text, "alt_emails")
        if alts:
            for part in alts.split(","):
                addr = part.strip().lower()
                if "@" in addr:
                    addrs.append(addr)
        for addr in addrs:
            idx[addr] = (fp, last)
    return idx


def merge_signals(*signal_dicts: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for d in signal_dicts:
        if not d:
            continue
        for email_val, date in d.items():
            if not email_val or not date:
                continue
            key = email_val.lower()
            if key not in merged or date > merged[key]:
                merged[key] = date
    return merged


_LAST_INTERACTION_LINE_RE = re.compile(
    r"^(-\s*\*\*last_interaction(?:\*\*:|:\*\*)\s*).*$",
    re.MULTILINE,
)


def update_last_interaction(fp: Path, new_date: str) -> bool:
    text = fp.read_text(encoding="utf-8")
    existing = _parse_field(text, "last_interaction")
    if existing and new_date <= existing:
        return False

    new_text, count = _LAST_INTERACTION_LINE_RE.subn(
        rf"\g<1>{new_date}", text, count=1
    )
    if count == 0:
        new_text = text.rstrip() + f"\n- **last_interaction:** {new_date}\n"
    fp.write_text(new_text, encoding="utf-8")
    return True


# ── Email header helpers ────────────────────────────────────────────


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _extract_emails(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    out: list[str] = []
    for raw in header_value.split(","):
        parts = email.utils.parseaddr(raw.strip())
        addr = (parts[1] or "").strip().lower()
        if "@" in addr:
            out.append(addr)
            continue
        # Fallback: regex
        m = _EMAIL_RE.search(raw)
        if m:
            out.append(m.group(0).lower())
    # Dedupe, preserve order
    seen: set[str] = set()
    result: list[str] = []
    for e in out:
        if e not in seen:
            seen.add(e)
            result.append(e)
    return result


def _parse_rfc2822_date(header_value: str) -> str | None:
    if not header_value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).date().isoformat()


def _event_date(event: dict) -> str | None:
    """Extract a yyyy-mm-dd from a GCal event's start field."""
    start = event.get("start", {}) or {}
    if isinstance(start, str):
        return start[:10]
    raw = start.get("dateTime") or start.get("date") or ""
    return raw[:10] if raw else None


def _attendee_emails(event: dict) -> list[str]:
    out: list[str] = []
    for att in event.get("attendees", []) or []:
        if not isinstance(att, dict):
            continue
        if att.get("self"):
            continue
        addr = (att.get("email") or "").strip().lower()
        if "@" in addr:
            out.append(addr)
    return out


# ── Krisp debrief reader ────────────────────────────────────────────


def _load_krisp_debriefs(cache_dir: Path, attendee_cap: int = KRISP_ATTENDEE_CAP) -> dict[str, str]:
    """Scan pending-debrief-*.json in a meetings-coach cache dir.

    For each debrief where len(attendees) <= attendee_cap, extract the
    meeting date and stamp every attendee email with it. Multiple
    debriefs collapse to the max date per email.
    """
    signals: dict[str, str] = {}
    if not cache_dir.exists():
        return signals
    for fp in sorted(cache_dir.glob("pending-debrief-*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        attendees = data.get("attendees") or []
        if not attendees or len(attendees) > attendee_cap:
            continue

        start = data.get("meeting_start")
        if isinstance(start, dict):
            iso = start.get("dateTime") or start.get("date") or ""
        else:
            iso = start or ""
        date = iso[:10] if iso else None
        if not date:
            continue

        for att in attendees:
            if not isinstance(att, dict):
                continue
            addr = (att.get("email") or "").strip().lower()
            if "@" not in addr:
                continue
            cur = signals.get(addr)
            if not cur or date > cur:
                signals[addr] = date
    return signals


# ── Network collectors ──────────────────────────────────────────────


def _collect_gcal_signals(
    service,
    lookback_days: int = LOOKBACK_DAYS,
    lookahead_days: int = LOOKAHEAD_DAYS,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (past, upcoming) where each is email -> yyyy-mm-dd."""
    today = datetime.now(timezone.utc).date()
    past: dict[str, str] = {}
    upcoming: dict[str, str] = {}

    page_token = None
    while True:
        resp = service.events().list(
            calendarId="primary",
            timeMin=(today - timedelta(days=lookback_days)).isoformat() + "T00:00:00Z",
            timeMax=(today + timedelta(days=lookahead_days)).isoformat() + "T23:59:59Z",
            singleEvents=True,
            orderBy="startTime",
            maxResults=GCAL_PAGE_SIZE,
            pageToken=page_token,
        ).execute()

        for event in resp.get("items", []) or []:
            date = _event_date(event)
            if not date:
                continue
            emails = _attendee_emails(event)
            if not emails:
                continue
            bucket = past if date <= today.isoformat() else upcoming
            for addr in emails:
                cur = bucket.get(addr)
                if bucket is past:
                    # past: most recent
                    if not cur or date > cur:
                        bucket[addr] = date
                else:
                    # upcoming: soonest
                    if not cur or date < cur:
                        bucket[addr] = date

        page_token = resp.get("nextPageToken") if isinstance(resp, dict) else None
        if not page_token:
            break

    return past, upcoming


def _collect_gmail_signals(
    service,
    days: int = LOOKBACK_DAYS,
    operator_emails: set[str] | None = None,
) -> dict[str, str]:
    operator_emails = {e.lower() for e in (operator_emails or set())}
    signals: dict[str, str] = {}

    list_resp = service.users().messages().list(
        userId="me",
        q=f"newer_than:{days}d",
        maxResults=GMAIL_PAGE_SIZE,
    ).execute()

    ids = [m.get("id") for m in list_resp.get("messages", []) if m.get("id")]
    for msg_id in ids:
        meta = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "To", "Cc", "Date"],
        ).execute()
        headers = {
            h.get("name", ""): h.get("value", "")
            for h in meta.get("payload", {}).get("headers", [])
        }
        date = _parse_rfc2822_date(headers.get("Date", ""))
        if not date:
            continue
        for field in ("From", "To", "Cc"):
            for addr in _extract_emails(headers.get(field)):
                if addr in operator_emails:
                    continue
                cur = signals.get(addr)
                if not cur or date > cur:
                    signals[addr] = date
    return signals


# ── Config + OAuth plumbing (only when running for real) ───────────


def _load_mining_config() -> dict:
    spec = importlib.util.spec_from_file_location(
        "mining_utils_dr", MINE_DIR / "mining_utils.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load_config()


def _operator_emails() -> set[str]:
    try:
        cfg = _load_mining_config()
    except Exception:
        return set()
    return {e.lower() for e in cfg.get("operator_emails", []) if e}


def _find_google_token() -> Path | None:
    # Connector's own token first (created via agents/connector/scripts/gcal-auth.py).
    # Cross-workspace paths are kept as legacy fallbacks but silently break under
    # bubblewrap isolation — the connector process can't see another workspace's files.
    candidates = [
        Path(os.path.expanduser("~/.clawford/connector-workspace/token.json")),
        Path(os.path.expanduser("~/.clawford/family-calendar-workspace/token.json")),
        Path(os.path.expanduser("~/.clawford/meetings-coach-workspace/token.json")),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _build_google_services():
    """Returns (gcal_service, gmail_service). Raises on failure."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = _find_google_token()
    if not token_path:
        raise RuntimeError(
            "No Google token.json found under ~/.clawford/family-calendar-workspace/ "
            "or ~/.clawford/meetings-coach-workspace/"
        )

    token_data = json.loads(token_path.read_text(encoding="utf-8"))
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_data["token"] = creds.token
        token_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    if not creds.valid:
        raise RuntimeError(f"Google credentials invalid: {token_path}")

    return (
        build("calendar", "v3", credentials=creds, cache_discovery=False),
        build("gmail", "v1", credentials=creds, cache_discovery=False),
    )


# ── run() ───────────────────────────────────────────────────────────


def run() -> dict:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    operator_emails = _operator_emails()

    sources_failed: list[dict] = []

    # Krisp is read-only off the disk — cheap, always try.
    try:
        krisp_signals = _load_krisp_debriefs(MC_CACHE, attendee_cap=KRISP_ATTENDEE_CAP)
    except Exception as e:  # pragma: no cover — defensive
        krisp_signals = {}
        sources_failed.append({"source": "krisp", "error": str(e)})

    # Google Messages Web — also a local cache file, cheap read.
    try:
        gmessages_signals = _load_gmessages_signals(GMESSAGES_CACHE)
    except Exception as e:  # pragma: no cover — defensive
        gmessages_signals = {}
        sources_failed.append({"source": "gmessages", "error": str(e)})

    # Gmail + GCal share a token. Build once, catch together.
    try:
        gcal_svc, gmail_svc = _build_google_services()
    except Exception as e:
        return {
            "status": "degraded",
            "alert": f"daily-refresh: Google token missing or invalid ({e})",
            "krisp_signals": len(krisp_signals),
            "sources_failed": [{"source": "google_auth", "error": str(e)}],
        }

    try:
        gcal_past, gcal_upcoming = _collect_gcal_signals(
            gcal_svc, lookback_days=LOOKBACK_DAYS, lookahead_days=LOOKAHEAD_DAYS
        )
    except Exception as e:
        gcal_past, gcal_upcoming = {}, {}
        sources_failed.append({"source": "gcal", "error": str(e)})

    try:
        gmail_signals = _collect_gmail_signals(
            gmail_svc, days=LOOKBACK_DAYS, operator_emails=operator_emails
        )
    except Exception as e:
        gmail_signals = {}
        sources_failed.append({"source": "gmail", "error": str(e)})

    # Merge all email-keyed signals
    merged = merge_signals(gmail_signals, gcal_past, krisp_signals)

    # Per-file write failures are collected rather than raised. Dropbox
    # can park a single person file in transient Errno-30 state; the
    # pre-2026-04-18 behavior let that abort the whole batch (see
    # 2026-04-18 kristen-pearis.md host log). Keep going and surface the
    # list in the result payload.
    write_failures: list[dict[str, str]] = []

    def _safe_update(fp: Path, date: str) -> bool | None:
        try:
            return update_last_interaction(fp, date)
        except OSError as exc:
            write_failures.append({"path": str(fp), "error": f"{exc.__class__.__name__}: {exc}"})
            return None

    # Apply email-keyed updates
    email_idx = build_email_index(BRAIN_PEOPLE)
    updated = 0
    unmatched = 0
    updated_paths: set[Path] = set()
    for addr, date in merged.items():
        entry = email_idx.get(addr)
        if not entry:
            unmatched += 1
            continue
        fp, _existing = entry
        if _safe_update(fp, date):
            updated += 1
            updated_paths.add(fp)

    # Apply phone-keyed gmessages updates — a person can be updated by
    # both pipelines in one run, so we count file updates once.
    phone_idx = build_phone_index(BRAIN_PEOPLE)
    gm_unmatched = 0
    matched_by_phone: set[Path] = set()
    for phone, date in gmessages_signals.items():
        entry = phone_idx.get(phone)
        if not entry:
            gm_unmatched += 1
            continue
        fp, _existing = entry
        matched_by_phone.add(fp)
        if _safe_update(fp, date):
            if fp not in updated_paths:
                updated += 1
                updated_paths.add(fp)

    # Name-match fallback: Google Messages displays the contact's saved
    # name for every chat that has one in the operator's Android contacts. Match
    # against the person-file H1 when phone mining didn't find them.
    try:
        gmessages_by_name = _load_gmessages_by_name(GMESSAGES_CACHE)
    except Exception as e:
        gmessages_by_name = {}
        sources_failed.append({"source": "gmessages_by_name", "error": str(e)})
    name_idx = build_name_index(BRAIN_PEOPLE)
    for name, date in gmessages_by_name.items():
        entry = name_idx.get(name)
        if not entry:
            gm_unmatched += 1
            continue
        fp, _existing = entry
        if fp in matched_by_phone:
            # Already stamped via phone — skip the extra work
            continue
        if _safe_update(fp, date):
            if fp not in updated_paths:
                updated += 1
                updated_paths.add(fp)
    unmatched += gm_unmatched

    # Upcoming cache: even unmatched emails get persisted so the
    # filter catches new contacts on their first meeting.
    UPCOMING_CACHE.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "window_days": LOOKAHEAD_DAYS,
                "emails": gcal_upcoming,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    degraded = bool(sources_failed) or bool(write_failures)
    alert_bits: list[str] = []
    if sources_failed:
        alert_bits.append(f"{len(sources_failed)} source(s) failed")
    if write_failures:
        alert_bits.append(
            f"{len(write_failures)} person file(s) unwritable (Dropbox EROFS?)"
        )

    result = {
        "status": "degraded" if degraded else "ok",
        "people_updated": updated,
        "signals_unmatched": unmatched,
        "gmail_signals": len(gmail_signals),
        "gcal_past_signals": len(gcal_past),
        "gcal_upcoming_signals": len(gcal_upcoming),
        "krisp_signals": len(krisp_signals),
        "gmessages_signals": len(gmessages_signals),
        "sources_failed": sources_failed,
        "write_failures": write_failures,
    }
    if alert_bits:
        result["alert"] = "daily-refresh: " + "; ".join(alert_bits)
    return result


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
