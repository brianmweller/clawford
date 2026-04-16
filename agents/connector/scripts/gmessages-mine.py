#!/usr/bin/env python3
"""gmessages-mine.py — Headless scrape of Google Messages Web.

Uses the persistent Chromium profile that gmessages-auth.py paired once,
visits messages.google.com/web/conversations, extracts the conversation
list (name + relative time), resolves the time strings to yyyy-mm-dd in
the operator's local timezone, and writes the results to

    ~/.clawford/connector-workspace/cache/mined-gmessages.json

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

from agents.shared.scan_fields import scan_fields  # noqa: E402


WORKSPACE = Path(os.path.expanduser("~/.clawford/connector-workspace"))
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
MAX_ROWS = 500
SCROLL_PAUSE_MS = 1_500
SCROLL_MAX_ITER = 12


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
_MDY_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
_RELATIVE_RECENT_RE = re.compile(r"^(\d+)\s*(min|h|hr|hrs|d)$", re.IGNORECASE)


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

    # "3/28/2026" or "1/26/25" (2-digit year → 2000s if <70, else 1900s)
    m = _MDY_RE.match(s)
    if m:
        try:
            month = int(m.group(1))
            day_num = int(m.group(2))
            year = int(m.group(3))
            if year < 100:
                year += 2000 if year < 70 else 1900
            return date(year, month, day_num).isoformat()
        except ValueError:
            return None

    # "23 min", "5 h", "2 hr", "11 hrs" → today; "3 d" → today minus N days
    m = _RELATIVE_RECENT_RE.match(s)
    if m:
        try:
            n = int(m.group(1))
        except ValueError:
            return None
        unit = m.group(2).lower()
        if unit == "d":
            return (today - timedelta(days=n)).isoformat()
        return today.isoformat()

    return None


def _row_to_contact(row: dict, today: date) -> dict | None:
    name = (row.get("name") or "").strip()
    time_label = (row.get("time") or "").strip()
    if not name:
        return None
    resolved = _resolve_relative_date(time_label, today)
    if not resolved:
        return None

    # P0.4: the display name is attacker-controlled (anyone who sends
    # an SMS or chat can set it). An attacker could set it to
    # "[SYSTEM] ..." and have the agent read it during chat. Scan and
    # (in enforce mode) replace with a placeholder; warn mode passes
    # through but records the hit.
    sanitized, warnings = scan_fields(
        fields={"name": name},
        source_type="gmessages",
        source_id=name[:64],
        workspace=WORKSPACE,
    )
    contact = {
        "name": sanitized["name"],
        "phone": "",
        "last_message_date": resolved,
    }
    if warnings:
        contact["scan_warnings"] = warnings
    # If the display name looks like a phone number (no letters), expose
    # it as phone too so daily-refresh can match by normalized digits.
    if not any(ch.isalpha() for ch in sanitized["name"]):
        contact["phone"] = sanitized["name"]
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

  // Recognize common Google Messages timestamp formats: "Yesterday",
  // "Today", weekday names, "12:35 PM" / "14:05", "Mar 28", "3/28/25",
  // "Apr 5". Avoid matching emoji-only spans or message snippets.
  const TIME_RE = /^(Today|Yesterday|Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|\d{1,2}:\d{2}\s*(AM|PM|am|pm)?|[A-Z][a-z]{2}\s\d{1,2}|\d{1,2}\/\d{1,2}\/\d{2,4}|\d+\s*(min|h|hr|hrs|d))$/;

  // Walk every element in the row and collect its DIRECT text (the
  // text node children only — not concatenated descendant text). This
  // gives us the discrete rendered strings: name, snippet, time tag.
  function directText(el) {
    let out = '';
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        out += child.textContent;
      }
    }
    return out.trim();
  }

  const out = [];
  for (const row of rows) {
    const texts = [];
    row.querySelectorAll('*').forEach(el => {
      const t = directText(el);
      if (t) texts.push(t);
    });

    // Time: rightmost text matching the time regex
    let time = '';
    for (let i = texts.length - 1; i >= 0; i--) {
      if (TIME_RE.test(texts[i])) {
        time = texts[i];
        break;
      }
    }

    // Name: explicit selector first, then first non-time, non-snippet
    let name = '';
    const nameEl = row.querySelector('h3, h4, [class*="name"]:not([class*="time"]):not([class*="timestamp"])');
    if (nameEl) {
      name = directText(nameEl) || (nameEl.textContent || '').trim().split('\n')[0];
    }
    if (!name) {
      for (const t of texts) {
        if (!t || t === time) continue;
        if (TIME_RE.test(t)) continue;
        if (/^[\p{Extended_Pictographic}\s]+$/u.test(t)) continue;
        if (/^You:\s/.test(t)) continue;
        if (/\u201c|\u201d/.test(t)) continue;
        if (t.length > 80) continue;
        name = t;
        break;
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
        from agents.shared.camoufox_proxy import launch_camoufox
    except Exception as e:
        return [], f"camoufox_proxy import failed: {e}"

    try:
        # Camoufox + persistent context, mirroring gmessages-auth.py
        # exactly. Google Messages Web inspects the navigator on every
        # load — chromium gets `signin/rejected` even with the
        # --enable-automation flag stripped, but Camoufox's patched
        # Firefox passes cleanly with the same profile cookies the
        # auth flow already wrote.
        with launch_camoufox(
            proxy_cfg=None,
            headless="virtual",
            os_name="windows",
            persistent_context=True,
            user_data_dir=PROFILE_DIR,
        ) as ctx:
            pages = ctx.pages
            page = pages[0] if pages else ctx.new_page()
            page.goto(MESSAGES_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            page.wait_for_timeout(POST_LOAD_SETTLE_MS)

            # Sanity check: if we bounce to /welcome the profile lost
            # its session and the user needs to re-run gmessages-auth.
            current = page.url or ""
            if "/web/welcome" in current or "authentication" in current:
                return [], "profile expired — re-run gmessages-auth.py"

            # Lazy-load pagination: Google Messages shows the top ~26
            # conversations on first render, then loads the next batch
            # asynchronously when you scroll the sidebar near the
            # bottom. We have to scroll, *wait*, then count — doing
            # both in the same evaluate() captures the row count
            # before the new batch lands in the DOM.
            last_count = -1
            stable_iters = 0
            for _ in range(SCROLL_MAX_ITER):
                page.evaluate(
                    """() => {
                        const nav = document.querySelector('nav.conversation-list');
                        if (nav) nav.scrollTop = nav.scrollHeight;
                    }"""
                )
                page.wait_for_timeout(SCROLL_PAUSE_MS)
                count = page.evaluate(
                    "() => document.querySelectorAll('mws-conversation-list-item').length"
                )
                if not isinstance(count, int):
                    break
                if count == last_count:
                    stable_iters += 1
                    if stable_iters >= 2:
                        break  # plateau confirmed across two cycles
                else:
                    stable_iters = 0
                last_count = count

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
