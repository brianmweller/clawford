"""role_timeline_lib — parse self/role-timeline.md and map ISO dates to
the role the operator was in on that date.

Purpose: Workflowy meeting records from Stage 1 are all tagged
role="meetings" because the walker doesn't know which job the operator held
on any given date. The per-role archive synthesizer uses this library
to join each meeting's chronological_date against the operator's actual role
timeline so the right meetings feed the right archives/<role>.md.

Pure helpers. Tests exercise them via tmp_path fixtures.

Timeline file format (one role per line under ## Roles):
  - **<role>** | <start-date or "before"> | <end-date or "current"> | <label>

Semantics:
  - start is inclusive (a record on 2019-05-01 with twitch.start=2019-05-01 maps to twitch)
  - end is exclusive (a record on 2020-09-01 with twitch.end=2020-09-01 does NOT map to twitch)
  - "before" in the start column means no lower bound (all dates before end match)
  - "current" in the end column means no upper bound (all dates from start onwards match)
"""
from __future__ import annotations

import re
from pathlib import Path


# Matches role lines like:
#   - **amazon**     | 2020-09-01 | 2022-04-01 | YETI / Prime Video / WMS
#   - **pre-twitch** | before | 2019-05-01 | earlier roles
#   - **post-airbnb** | 2026-02-01 | current  | Clawford / founder mode
_ROLE_LINE_RE = re.compile(
    r"^\s*-\s*\*\*([a-z0-9\-]+)\*\*\s*\|\s*"
    r"(before|\d{4}-\d{2}-\d{2})\s*\|\s*"
    r"(current|\d{4}-\d{2}-\d{2})\s*\|\s*"
    r"(.+?)\s*$"
)


def parse_role_timeline(path: Path) -> list[dict]:
    """Parse a role-timeline.md file into an ordered list of role ranges.

    Raises FileNotFoundError if the file doesn't exist. Ignores prose;
    only lines matching the role-line format are returned.
    """
    text = path.read_text(encoding="utf-8")
    ranges: list[dict] = []
    for line in text.splitlines():
        m = _ROLE_LINE_RE.match(line)
        if not m:
            continue
        role, start_raw, end_raw, label = m.groups()
        ranges.append({
            "role": role,
            "start": "" if start_raw == "before" else start_raw,
            "end": end_raw,   # may be "current" or an ISO date
            "label": label.strip(),
        })
    return ranges


def _normalize_iso(date: str | None) -> str:
    """Normalize a potentially-partial ISO date (YYYY, YYYY-MM, YYYY-MM-DD)
    to YYYY-MM-DD. Returns empty string if input is not parseable.

    Partial dates expand to the 1st of month / January 1st so "2022" and
    "2022-01-01" are equivalent at the role-boundary level.
    """
    if not date:
        return ""
    s = str(date).strip()
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$", s)
    if not m:
        return ""
    y, mo, d = m.groups()
    mo = mo or "01"
    d = d or "01"
    # Sanity-check month/day so "2026-13-45" returns empty
    try:
        yi, moi, di = int(y), int(mo), int(d)
        if not (1 <= moi <= 12):
            return ""
        if not (1 <= di <= 31):
            return ""
    except ValueError:
        return ""
    return f"{yi:04d}-{moi:02d}-{di:02d}"


def role_for_date(date: str | None, ranges: list[dict]) -> str:
    """Return the role the operator was in on the given ISO date, or 'unknown'
    if no range matches or the date is unparseable.

    Semantics:
      - start-inclusive, end-exclusive (the day someone joins a new role
        belongs to the new role)
      - start="" means no lower bound (pre-timeline records)
      - end="current" means no upper bound (current/ongoing role)
    """
    normalized = _normalize_iso(date)
    if not normalized:
        return "unknown"
    if not ranges:
        return "unknown"

    for r in ranges:
        start = r.get("start", "") or ""
        end = r.get("end", "")
        # start check: normalized >= start (empty start means always-before-ok)
        if start and normalized < start:
            continue
        # end check: normalized < end (current means always-after-ok)
        if end and end != "current" and normalized >= end:
            continue
        return r["role"]

    return "unknown"
