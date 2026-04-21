#!/usr/bin/env python3
"""morning-relationship-nudge.py — Connector (Huckle Cat) morning nudge.

Phase 4 liberation: replaces the OpenClaw LLM cron
`connector:morning-relationship-nudge`. Runs people-scan.py, formats
the nudge (see format_nudge() below), and writes cache/morning-brief-ready.txt
for the 5 AM PT fleet delivery path. On Mondays the weekly-review cron is
folded in (Option C) via an additional MONDAY recap section.

Pure Python templating — people-scan output is structured, so no LLM
in the composition tier (per the operator's logic-gate rule).

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import html
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.subprocess_helpers import (  # noqa: E402
    is_subprocess_error,
    run_json_script,
)


WORKSPACE = Path(os.path.expanduser("~/.clawford/connector-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
BRIEF_FILE = CACHE_DIR / "morning-brief-ready.txt"
# morning-fleet-deliver.py reads items from this file and renders
# each as a separate Telegram message with per-person nudge buttons.
ITEMS_FILE = CACHE_DIR / "morning-items.json"
LAST_RUN_FILE = CACHE_DIR / "last-morning-nudge.json"

PACIFIC = ZoneInfo("America/Los_Angeles")
SUBPROCESS_TIMEOUT_S = 120


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


# Channel labels that map to a phone number (tel: anchor) rather than
# an email address. Case-insensitive match against preferred_channel.
_PHONE_CHANNEL_TOKENS = frozenset({
    "phone", "text", "messages", "sms", "whatsapp", "signal",
})


def _render_contact_link(entry: dict) -> str:
    """Render the contact portion of a person line as clickable
    Telegram HTML (mailto: or tel:) when the entry carries an email /
    phone, falling back to the plain preferred_channel label when
    neither value is present.

    Preference logic:
      1. Normalize preferred_channel → prefer email | prefer phone.
      2. If the preferred side is populated, render it.
      3. If the preferred side is empty but the other is populated,
         render the other (don't drop the link).
      4. If both sides are empty, fall back to the HTML-escaped
         channel label (may be empty).

    Callers send the rendered string as part of a parse_mode=HTML
    Telegram message — any literal text that ends up in the anchor's
    text node or in the fallback label is html.escape'd to keep the
    markup valid."""
    channel = (entry.get("preferred_channel") or "").strip()
    email = (entry.get("email") or "").strip()
    phone = (entry.get("phone") or "").strip()

    prefers_email = channel.lower() == "email"
    prefers_phone = channel.lower() in _PHONE_CHANNEL_TOKENS

    if prefers_email and email:
        escaped = html.escape(email)
        return f'<a href="mailto:{escaped}">{escaped}</a>'
    if prefers_phone and phone:
        escaped = html.escape(phone)
        return f'<a href="tel:{escaped}">{escaped}</a>'
    # Preferred side missing — fall back to whichever is available.
    if email:
        escaped = html.escape(email)
        return f'<a href="mailto:{escaped}">{escaped}</a>'
    if phone:
        escaped = html.escape(phone)
        return f'<a href="tel:{escaped}">{escaped}</a>'
    # No reachable contact — render the channel label as plain text.
    return html.escape(channel)


def _fmt_overdue_line(entry: dict) -> str:
    name = entry.get("name") or entry.get("slug") or "?"
    days_since = entry.get("days_since")
    contact = _render_contact_link(entry)

    days_part = f"{days_since} days" if days_since is not None else "never"
    contact_part = f" · {contact}" if contact else ""
    return f"  {name} — {days_part}{contact_part}"


# Display-group labels for the grouped nudge output.
_GROUP_LABELS = {
    "family": "\U0001f46a FAMILY",
    "friends": "\U0001f91d FRIENDS",
    "colleagues": "\U0001f454 COLLEAGUES",
}


def build_nudge_items(scan: dict, now_pacific: datetime) -> list[dict]:
    """Build the structured items list for morning-fleet-deliver.

    Each item becomes its own Telegram message. Shapes:
      {"type": "overview",     "text": "..."}            — no buttons
      {"type": "group_header", "text": "👪 FAMILY (5)"} — no buttons
      {"type": "person",       "slug": "...",
                                "text": "Kai Rivera — 64 days · Messages"}
        — renders with [\u2705 done] [\U0001f515 snooze 30d] [\U0001f648 ignore]

    Returns a flat list so fleet-delivery can iterate once.
    """
    weekday = now_pacific.strftime("%A")
    month = now_pacific.strftime("%B")
    day = now_pacific.day
    overdue_by_group = scan.get("overdue_by_group") or {}
    overdue_total = scan.get("overdue_total", 0)
    summary = scan.get("summary") or {}
    tracked_total = summary.get("total", 0)

    grouped_counts = {g: len(items) for g, items in overdue_by_group.items()}
    non_empty_groups = [g for g, c in grouped_counts.items() if c > 0]
    total_shown = sum(grouped_counts.values())

    if total_shown == 0:
        return [{
            "type": "overview",
            "text": (
                f"\U0001f431\U0001f91d Relationship Check — {weekday}, {month} {day}\n"
                f"Everyone's accounted for. No overdue check-ins today.\n\n"
                f"{tracked_total} tracked"
            ),
        }]

    items: list[dict] = [{
        "type": "overview",
        "text": (
            f"\U0001f431\U0001f91d Relationship Check — {weekday}, {month} {day}\n"
            f"{overdue_total} overdue across {len(non_empty_groups)} "
            f"circle{'s' if len(non_empty_groups) != 1 else ''} · "
            f"{tracked_total} tracked"
        ),
    }]

    for group_key in ("family", "friends", "colleagues"):
        entries = overdue_by_group.get(group_key) or []
        if not entries:
            continue
        label = _GROUP_LABELS.get(group_key, group_key.upper())
        items.append({
            "type": "group_header",
            "group": group_key,
            "text": f"{label} ({len(entries)})",
        })
        for entry in entries:
            name = entry.get("name") or entry.get("slug") or "?"
            days_since = entry.get("days_since")
            contact = _render_contact_link(entry)
            days_part = f"{days_since} days" if days_since is not None else "never"
            contact_part = f" · {contact}" if contact else ""
            items.append({
                "type": "person",
                "slug": entry.get("slug", ""),
                "group": group_key,
                "text": f"{html.escape(name)} — {days_part}{contact_part}",
            })

    return items


def format_nudge(scan: dict, now_pacific: datetime) -> str:
    """Render the full morning nudge.

    `scan` is the people-scan.py JSON output. `now_pacific` is the
    current moment in Pacific time; weekday == 0 triggers the Monday
    recap fold of the retired weekly-review cron.
    """
    weekday = now_pacific.strftime("%A")
    month = now_pacific.strftime("%B")
    day = now_pacific.day
    header = f"\U0001f431\U0001f91d Relationship Check — {weekday}, {month} {day}"

    overdue_by_group = scan.get("overdue_by_group") or {}
    overdue_total = scan.get("overdue_total", 0)
    summary = scan.get("summary") or {}
    tracked_total = summary.get("total", 0)

    # Flatten group counts to determine overall shape of the report.
    grouped_counts = {g: len(items) for g, items in overdue_by_group.items()}
    total_shown = sum(grouped_counts.values())

    lines: list[str] = [header, ""]

    if total_shown == 0:
        lines.append("Everyone's accounted for. No overdue check-ins today.")
        lines.append("")
    else:
        # Render each group as its own section with an emoji header and
        # a per-group count. Empty groups are skipped.
        for group_key in ("family", "friends", "colleagues"):
            entries = overdue_by_group.get(group_key) or []
            if not entries:
                continue
            label = _GROUP_LABELS.get(group_key, group_key.upper())
            lines.append(f"{label} ({len(entries)})")
            for entry in entries:
                lines.append(_fmt_overdue_line(entry))
            lines.append("")

    if now_pacific.weekday() == 0:
        lines.append("\U0001f4c5 MONDAY RECAP")
        lines.append(f"  {tracked_total} people tracked across all circles")
        if overdue_total:
            lines.append(f"  {overdue_total} overdue going into the week")
        lines.append("")

    # Footer: total overdue across all circles + tracked total. The
    # approaching bucket is no longer displayed per the operator's feedback
    # (2026-04-16) — approaching contacts didn't add useful signal.
    footer = (
        f"\U0001f431\U0001f91d {overdue_total} overdue · "
        f"{tracked_total} tracked"
    )
    lines.append(footer)

    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)
    now_pacific = now_utc.astimezone(PACIFIC)

    scan = _run_script("people-scan.py")

    # people-scan.py is the only data source for the nudge. Propagate
    # subprocess failures instead of masking them as a "no overdue
    # check-ins" brief (the 2026-04-15 silent-outage class).
    if is_subprocess_error(scan):
        error_msg = scan["__error__"]
        body = (
            f"\U0001f431\U0001f91d Couldn't check the address book this "
            f"morning — people-scan failed: {error_msg[:120]}.\n"
        )
        _write_atomic(BRIEF_FILE, body)
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "error",
                    "error": error_msg,
                    "summary": f"people-scan failed: {error_msg[:120]}",
                },
                indent=2,
            ),
        )
        return {
            "status": "error",
            "error": error_msg,
            "alert": f"\U0001f431\U0001f91d morning-relationship-nudge failed: {error_msg[:200]}",
        }

    if not isinstance(scan, dict):
        body = (
            f"\U0001f431\U0001f91d Couldn't check the address book this "
            f"morning — people-scan returned no usable output.\n"
        )
        _write_atomic(BRIEF_FILE, body)
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "degraded",
                    "alert": "people-scan.py returned no usable output",
                },
                indent=2,
            ),
        )
        return {
            "status": "degraded",
            "alert": "\U0001f431\U0001f91d people-scan.py returned no usable output",
        }

    body = format_nudge(scan, now_pacific)
    _write_atomic(BRIEF_FILE, body)

    # Also emit the structured items file for morning-fleet-deliver.py.
    # This is what lets the operator see grouped sections + per-person buttons
    # instead of the fallback plain-text chunked delivery.
    try:
        nudge_items = build_nudge_items(scan, now_pacific)
        _write_atomic(ITEMS_FILE, json.dumps(nudge_items, ensure_ascii=False, indent=2))

        # Record which slugs are about to be delivered so tomorrow's
        # people-scan can auto-snooze any that the operator didn't action.
        shown_slugs = [
            it.get("slug") for it in nudge_items
            if it.get("type") == "person" and it.get("slug")
        ]
        today_pacific = now_pacific.date().isoformat()
        last_shown_path = WORKSPACE / f"last-shown-{today_pacific}.json"
        _write_atomic(
            last_shown_path,
            json.dumps({"slugs": shown_slugs, "delivered_at": now_utc.isoformat()}),
        )
    except Exception as exc:
        # Items-file is optional; plain-text BRIEF_FILE still works as
        # a fallback. Log but don't fail the whole cron.
        print(f"build_nudge_items failed: {exc}", file=sys.stderr)

    summary = scan.get("summary") or {}
    is_monday = now_pacific.weekday() == 0

    _write_atomic(
        LAST_RUN_FILE,
        json.dumps(
            {
                "timestamp": now_utc.isoformat(),
                "status": "ok",
                "overdue_total": scan.get("overdue_total", 0),
                "approaching_count": len(scan.get("approaching") or []),
                "tracked_total": summary.get("total", 0),
                "monday_recap_included": is_monday,
            },
            indent=2,
        ),
    )

    return {
        "status": "ok",
        "brief_path": str(BRIEF_FILE),
        "overdue_total": scan.get("overdue_total", 0),
        "approaching_count": len(scan.get("approaching") or []),
        "tracked_total": summary.get("total", 0),
        "monday_recap_included": is_monday,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f431\U0001f91d morning-relationship-nudge failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
