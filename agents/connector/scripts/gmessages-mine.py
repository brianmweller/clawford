#!/usr/bin/env python3
"""gmessages-mine.py — Headless scrape of Google Messages Web.

Uses the persistent Chromium profile that gmessages-auth.py paired once,
visits messages.google.com/web/conversations, extracts the conversation
list (name + relative time), resolves the time strings to yyyy-mm-dd in
the operator's local timezone, and writes the results to

    ~/.openclaw/connector-workspace/cache/mined-gmessages.json

daily-refresh.py reads that cache to stamp last_interaction on matching
people files (by phone when the name is a raw number, by name otherwise).

This script conforms to SCRIPT_CONTRACT.md: bare `python3 <path>`
invocation, final stdout line is one JSON object with `status`, always
exits 0. Stderr is free for debug logs.

One-time setup:
    python3 gmessages-auth.py   # interactive QR pair via Xvfb + remote debug
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break


WORKSPACE = Path(os.path.expanduser("~/.openclaw/connector-workspace"))
PROFILE_DIR = WORKSPACE / "gmessages-profile"
CACHE_FILE = WORKSPACE / "cache" / "mined-gmessages.json"
MESSAGES_URL = "https://messages.google.com/web/conversations"

# Local timezone (for converting "2:35 PM" → today's date). Falls back
# to UTC if zoneinfo is unavailable.
LOCAL_TZ_NAME = "America/Los_Angeles"

# Extraction is synchronous DOM polling inside evaluate() — no route
# handlers, so time.sleep would also work, but we use page.wait_for_timeout
# per feedback_playwright_threading.md.
PAGE_LOAD_TIMEOUT_MS = 30_000
POST_LOAD_SETTLE_MS = 4_000
MAX_ROWS = 100


# ── Date resolution helpers ──────────────────────────────────────────


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?$")
_MONTH_DAY_RE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2})$")
_MDY_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _resolve_relative_date(label: str, today: date) -> str | None:
    """Convert a Google Messages conversation-list timestamp to
    yyyy-mm-dd. Returns None if unparseable."""
    if not label:
        return None
    s = label.strip()
    if not s:
        return None

    lowered = s.lower()
    if lowered in ("today", "just now") or _TIME_RE.match(s):
        return today.isoformat()
    if lowered == "yesterday":
        return (today - timedelta(days=1)).isoformat()

    # Bare weekday — most recent past occurrence (never "today" — that's
    # a time string, not a weekday label)
    if lowered in _WEEKDAYS:
        target = _WEEKDAYS[lowered]
        # days_back in 1..7 so "same weekday as today" resolves to a week ago
        days_back = (today.weekday() - target) % 7
        if days_back == 0:
            days_back = 7
        return (today - timedelta(days=days_back)).isoformat()

    # "Mar 28" / "mar 28"
    m = _MONTH_DAY_RE.match(s)
    if m:
        month_name = m.group(1).lower()[:3]
        if month_name in _MONTHS:
            month = _MONTHS[month_name]
            try:
                day_num = int(m.group(2))
            except ValueError:
                return None
            year = today.year
            if month > today.month or (month == today.month and day_num > today.day):
                year -= 1
            try:
                return date(year, month, day_num).isoformat()
            except ValueError:
                return None

    # "3/28/2026"
    m = _MDY_RE.match(s)
    if m:
        try:
            month = int(m.group(1))
            day_num = int(m.group(2))
            year = int(m.group(3))
            return date(year, month, day_num).isoformat()
        except ValueError:
            return None

    return None


def _row_to_contact(row: dict, today: date) -> dict | None:
    name = (row.get("name") or "").strip()
    time_label = (row.get("time") or "").strip()
    if not name:
        return None
    resolved = _resolve_relative_date(time_label, today)
    if not resolved:
        return None
    contact = {
        "name": name,
        "phone": "",
        "last_message_date": resolved,
    }
    # If the display name looks like a phone number (no letters), expose
    # it as phone too so daily-refresh can match by normalized digits.
    if not any(ch.isalpha() for ch in name):
        contact["phone"] = name
    return contact


def _today_local() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(LOCAL_TZ_NAME)).date()
    except Exception:
        return datetime.now(timezone.utc).date()


# ── JavaScript DOM extractor (runs in page.evaluate) ────────────────


_ROW_EXTRACTOR_JS = r"""
() => {
  // Google Messages renders conversations as <mws-conversation-list-item>
  // or similar custom elements. Try a few selectors in order.
  const selectors = [
    'mws-conversation-list-item',
    '[data-conversation-id]',
    '[role="listitem"]',
    'mws-conversations-list-item',
    '.list-item',
  ];
  let rows = [];
  for (const sel of selectors) {
    const found = document.querySelectorAll(sel);
    if (found && found.length > 0) {
      rows = Array.from(found);
      break;
    }
  }

  const out = [];
  for (const row of rows) {
    // Strategies for name extraction, in preference order:
    //   1. an explicit .name / .display-name / [data-name] attribute
    //   2. the first <h3> / <h4> / .title
    //   3. the largest text span inside the row
    let name = "";
    const nameEl =
      row.querySelector('[class*="name"] [dir], [class*="name"] span, .name, .display-name, h3, h4, [data-name]');
    if (nameEl) {
      name = (nameEl.textContent || "").trim();
    } else {
      // Fallback: first non-empty text node
      const walk = row.querySelector('span, div');
      if (walk) name = (walk.textContent || "").trim();
    }

    // Time: typically a small <span> with a short string
    let time = "";
    const timeEl =
      row.querySelector('[class*="time"], [class*="timestamp"], time, [aria-label*=":"]');
    if (timeEl) {
      time = (timeEl.textContent || "").trim();
    }
    if (!time) {
      // Fallback: the shortest text span of length <= 20
      const spans = row.querySelectorAll('span');
      for (const sp of spans) {
        const t = (sp.textContent || "").trim();
        if (t && t.length > 0 && t.length <= 20) {
          if (!time || t.length < time.length) time = t;
        }
      }
    }

    if (name) out.push({ name, time });
  }
  return out;
}
"""


# ── Scrape entrypoint (optional — only runs on VPS with profile) ────


def _scrape_with_playwright() -> tuple[list[dict], str | None]:
    """Returns (raw_rows, error_or_none). Each raw row is {name, time}."""
    try:
        from agents.shared.playwright_profile import launch_persistent_profile
    except Exception as e:
        return [], f"playwright_profile import failed: {e}"

    try:
        with launch_persistent_profile(PROFILE_DIR, headless=True) as browser:
            pages = browser.pages
            page = pages[0] if pages else browser.new_page()
            page.goto(MESSAGES_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            page.wait_for_timeout(POST_LOAD_SETTLE_MS)

            # Sanity check: if we bounce to the auth page, the profile
            # is no longer paired and the user needs to re-run gmessages-auth.
            current = page.url or ""
            if "authentication" in current:
                return [], "profile expired — re-run gmessages-auth.py"

            rows: list[dict] = page.evaluate(_ROW_EXTRACTOR_JS)
            if not isinstance(rows, list):
                rows = []
            return rows[:MAX_ROWS], None
    except Exception as e:
        return [], str(e)


def run() -> dict:
    if not PROFILE_DIR.exists():
        return {
            "status": "degraded",
            "alert": (
                f"🐛 gmessages profile missing at {PROFILE_DIR} — "
                "run gmessages-auth.py to pair"
            ),
        }

    today = _today_local()
    raw, err = _scrape_with_playwright()
    if err:
        return {
            "status": "degraded",
            "alert": f"🐛 gmessages scrape: {err}",
            "rows_seen": len(raw),
        }

    contacts: list[dict] = []
    dropped = 0
    for row in raw:
        c = _row_to_contact(row, today)
        if c:
            contacts.append(c)
        else:
            dropped += 1

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(
            {
                "status": "ok",
                "mined_at": datetime.now(timezone.utc).isoformat(),
                "source": "gmessages-web",
                "contacts": contacts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "rows_seen": len(raw),
        "contacts_written": len(contacts),
        "dropped_unparseable": dropped,
        "cache_path": str(CACHE_FILE),
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"🐛 gmessages-mine crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
