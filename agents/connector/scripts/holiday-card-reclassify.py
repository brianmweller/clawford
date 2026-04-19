#!/usr/bin/env python3
"""holiday-card-reclassify.py — Upsert `professional-inner` for everyone on
the operator's Holiday Card List.

Companion to `holiday-card-pins.csv`. Walks the people directory, matches
each pin row to a `.md` file by email (primary) or name-slug (fallback),
merges `professional-inner` into the file's `circles` line. Pins with no
matching file get a new stub .md with `circles: professional-inner`.

Background. The mining pipeline's `auto_circle` only promotes to
`professional-inner` when a contact has 5+ meetings in the last 30 days.
After the operator left Example Corp every ex-colleague fell through to
`professional-outer`, and `people-seed.py` skips existing files on re-run
so seed updates never reclassify. This script is the manual override
ingest; the pin-list short-circuit in `contact-aggregator.auto_circle` is
the durability half of the fix.

Usage
    python3 holiday-card-reclassify.py                    # dry run
    python3 holiday-card-reclassify.py --write            # apply changes
    python3 holiday-card-reclassify.py \\
        --pins /path/to/holiday-card-pins.csv \\
        --people-dir /path/to/people \\
        --write

Matching (priority order)
    1. Email exact (lowercased, stripped) — against `- **email:**` and
       `- **alt_emails:**` in each .md file.
    2. Name-slug — slugified pin name (lowercase, hyphens, punctuation
       stripped, roman suffixes stripped) against the .md filename stem
       and `- **slug:**`.

Merge semantics
    - Existing file already has `professional-inner`: no-op.
    - Existing file has other circles (e.g. `holiday-card`, `friends-close`):
      append `, professional-inner` so the 7-day cadence wins but the
      historical tag is preserved.
    - No matching file for a pin: create a stub using the
      `people-seed.py` template.
    - Ambiguous name-slug (two files, same normalized name, no email
      match): log and skip — requires manual disambiguation.

SCRIPT_CONTRACT-compliant: always exits 0, prints JSON report.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PINS_CSV = Path(__file__).resolve().parent / "holiday-card-pins.csv"
DEFAULT_PEOPLE_DIR = Path(os.path.expanduser("~/Dropbox/openclaw-backup/people"))

# Roman numeral suffixes ("III", "IV") and honorifics ("Jr.", "Sr.")
# get stripped from names before slugification so "Casimir Ksiazek III"
# maps to `casimir-ksiazek` for matching purposes. The mining pipeline
# usually drops these already.
_SUFFIX_RE = re.compile(
    r"\s+(?:(?:[IVX]{1,5})|(?:jr\.?)|(?:sr\.?)|(?:ii)|(?:iii)|(?:iv))\b",
    re.IGNORECASE,
)


def slugify(name: str) -> str:
    """Normalize a display name to the same slug shape the mining
    pipeline's `name_to_slug` produces, with roman/honorific suffixes
    stripped first."""
    if not name:
        return ""
    s = name.strip()
    s = _SUFFIX_RE.sub("", s)
    s = re.sub(r"[^a-z0-9\s]", "", s.lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    return s


_FRONTMATTER_EMAIL_RE = re.compile(r"^- \*\*email:\*\*\s*(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_ALT_EMAILS_RE = re.compile(
    r"^- \*\*alt_emails:\*\*\s*(.+?)\s*$", re.MULTILINE
)
_FRONTMATTER_NAME_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_SLUG_RE = re.compile(r"^- \*\*slug:\*\*\s*(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_CIRCLES_RE = re.compile(
    r"^- \*\*circles:\*\*\s*(.+?)\s*$", re.MULTILINE
)


def _parse_emails(value: str) -> set[str]:
    """Extract normalized email addresses from a frontmatter value.

    The files use em-dash `—` as a 'missing' sentinel; return empty set
    for that. Alt-emails lists are comma-separated.
    """
    if not value or value.strip() in ("—", "-", ""):
        return set()
    emails = set()
    for part in value.split(","):
        p = part.strip().lower()
        if "@" in p:
            emails.add(p)
    return emails


def parse_person_file(path: Path) -> dict:
    """Return {slug, name, emails, circles, content} for a .md file."""
    content = path.read_text(encoding="utf-8")
    emails = set()
    m = _FRONTMATTER_EMAIL_RE.search(content)
    if m:
        emails |= _parse_emails(m.group(1))
    m = _FRONTMATTER_ALT_EMAILS_RE.search(content)
    if m:
        emails |= _parse_emails(m.group(1))

    name_m = _FRONTMATTER_NAME_RE.search(content)
    slug_m = _FRONTMATTER_SLUG_RE.search(content)
    circles_m = _FRONTMATTER_CIRCLES_RE.search(content)

    slug = (slug_m.group(1).strip() if slug_m else path.stem).lower()
    name = name_m.group(1).strip() if name_m else slug

    return {
        "path": path,
        "slug": slug,
        "name": name,
        "name_slug": slugify(name),
        "emails": emails,
        "circles": (circles_m.group(1).strip() if circles_m else ""),
        "content": content,
    }


def _circles_list(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


def _merge_circle(existing: str, new: str = "professional-inner") -> str:
    """Append `new` to the comma-separated circles value if not present."""
    items = _circles_list(existing)
    if new in items:
        return existing
    items.append(new)
    return ", ".join(items)


def _write_merged_circles(path: Path, content: str, new_circles: str) -> None:
    """In-place rewrite of only the `circles` line. Other frontmatter
    fields are preserved byte-for-byte."""
    new_line = f"- **circles:** {new_circles}"
    new_content = _FRONTMATTER_CIRCLES_RE.sub(new_line, content, count=1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    os.replace(tmp, path)


_STUB_TEMPLATE = """# {name}

- **slug:** {slug}
- **circles:** professional-inner
- **relationship:** colleague
- **relationship_type:** colleague
- **preferred_channel:** email
- **email:** {email}
- **phone:** —
- **platforms:** email
- **last_interaction:** {date}
- **context_notes:**
- **notes:** Seeded by holiday-card-reclassify.py on {date}
"""
# last_interaction seeds to the stub creation date so the stub lands
# in `healthy` initially and ages into `overdue` at the 7-day
# professional-inner cadence. Without this, an empty field gave
# days_since=999 and every stub crowded out real-signal entries
# like Drew Branden (51d since last contact) at the top of the
# overdue list — exactly the wrong order for the operator's morning.


def _write_stub(people_dir: Path, slug: str, name: str, email: str) -> Path:
    path = people_dir / f"{slug}.md"
    content = _STUB_TEMPLATE.format(
        name=name,
        slug=slug,
        email=email or "—",
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    path.write_text(content, encoding="utf-8")
    return path


def _load_pins(csv_path: Path) -> tuple[list[dict], int]:
    """Return (deduped rows, duplicate_count). Dedupes by email when
    present, else by name-slug."""
    rows: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            name = (r.get("name") or r.get("Name") or "").strip()
            email = (r.get("email") or r.get("Email") or "").strip().lower()
            if not name and not email:
                continue
            key = email or slugify(name)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            rows.append({"name": name, "email": email})
    return rows, duplicates


def run(*, pins_csv: Path, people_dir: Path, write: bool) -> dict:
    """Reclassify. Returns a report dict."""
    people_dir = Path(people_dir)
    pins_csv = Path(pins_csv)
    if not pins_csv.exists():
        raise FileNotFoundError(f"pins CSV not found: {pins_csv}")
    people_dir.mkdir(parents=True, exist_ok=True)

    pins, duplicates_in_csv = _load_pins(pins_csv)

    # Index all existing files for O(1) email + name-slug lookup.
    files = sorted(people_dir.glob("*.md"))
    records = [parse_person_file(p) for p in files if p.stem != "_template"]
    by_email: dict[str, list[dict]] = defaultdict(list)
    by_name_slug: dict[str, list[dict]] = defaultdict(list)
    by_slug: dict[str, dict] = {}
    for rec in records:
        for e in rec["emails"]:
            by_email[e].append(rec)
        if rec["name_slug"]:
            by_name_slug[rec["name_slug"]].append(rec)
        by_slug[rec["slug"]] = rec

    report = {
        "pins_total": len(pins),
        "duplicates_in_csv": duplicates_in_csv,
        "matched_by_email": 0,
        "matched_by_name": 0,
        "created": 0,
        "ambiguous": 0,
        "already_pinned": 0,
        "details": [],
    }

    for pin in pins:
        name = pin["name"]
        email = pin["email"]
        pin_slug = slugify(name)

        matches: list[dict] = []

        # 1) Email exact.
        if email:
            matches = by_email.get(email, [])

        matched_by = "email" if matches else None

        # 2) Name-slug fallback if no email hit.
        if not matches and pin_slug:
            # Also match if the .md filename stem equals the pin slug.
            if pin_slug in by_slug:
                matches = [by_slug[pin_slug]]
                matched_by = "name"
            else:
                matches = by_name_slug.get(pin_slug, [])
                matched_by = "name" if matches else None

        if not matches:
            # Create stub.
            if write:
                _write_stub(people_dir, pin_slug, name, email)
            report["created"] += 1
            report["details"].append({
                "action": "created",
                "name": name,
                "email": email,
                "slug": pin_slug,
            })
            continue

        if len(matches) > 1:
            report["ambiguous"] += 1
            report["details"].append({
                "action": "ambiguous",
                "name": name,
                "email": email,
                "candidates": [m["slug"] for m in matches],
            })
            continue

        rec = matches[0]
        if "professional-inner" in _circles_list(rec["circles"]):
            report["already_pinned"] += 1
            report["details"].append({
                "action": "already_pinned",
                "slug": rec["slug"],
            })
            continue

        # Merge.
        new_circles = _merge_circle(rec["circles"])
        if write:
            _write_merged_circles(rec["path"], rec["content"], new_circles)
        if matched_by == "email":
            report["matched_by_email"] += 1
        else:
            report["matched_by_name"] += 1
        report["details"].append({
            "action": "merged",
            "slug": rec["slug"],
            "matched_by": matched_by,
            "old_circles": rec["circles"],
            "new_circles": new_circles,
        })

    report["status"] = "ok"
    report["write"] = write
    return report


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--pins", default=str(DEFAULT_PINS_CSV))
    p.add_argument("--people-dir", default=str(DEFAULT_PEOPLE_DIR))
    p.add_argument(
        "--write",
        action="store_true",
        help="Apply changes. Without this flag the script is a dry-run.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = run(
            pins_csv=Path(args.pins),
            people_dir=Path(args.people_dir),
            write=args.write,
        )
    except Exception as e:
        report = {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    # Omit verbose details from the top-level stdout unless stderr asks
    # for them — keeps the JSON line tight for contract compliance.
    summary = {k: v for k, v in report.items() if k != "details"}
    print(json.dumps(summary))
    if report.get("details"):
        print(json.dumps(report["details"], indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    import json as _contract_json
    import sys as _contract_sys
    _contract_status = "ok"
    _contract_error = None
    try:
        _contract_rc = main()
        if _contract_rc not in (0, None):
            _contract_status = "error"
            _contract_error = f"main returned {_contract_rc}"
    except SystemExit as _contract_e:
        if _contract_e.code not in (0, None):
            _contract_status = "error"
            _contract_error = f"main exited with code {_contract_e.code}"
    except BaseException as _contract_e:  # noqa: BLE001
        _contract_status = "error"
        _contract_error = str(_contract_e)[:200]
    _contract_envelope = {"status": _contract_status}
    if _contract_error:
        _contract_envelope["error"] = _contract_error
    print(_contract_json.dumps(_contract_envelope))
    _contract_sys.exit(0)
