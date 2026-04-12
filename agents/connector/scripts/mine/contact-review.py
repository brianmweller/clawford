#!/usr/bin/env python3
"""
contact-review.py — Apply cutoff rule and generate review markdown.

Default: loads enriched-contacts.json + cache/cutoff-rule.json, applies
the rule, writes:
  - cache/review-contacts.md (kept contacts, for spot-check)
  - cache/review-dropped.md (dropped contacts, audit trail)

--finalize: re-reads review-contacts.md after edits and writes review-ready.json.

Usage:
  python3 contact-review.py               # Auto-apply cutoff, generate md
  python3 contact-review.py --finalize    # Parse edited md -> JSON
"""

import io
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

REVIEW_MD = CACHE_DIR / "review-contacts.md"
REVIEW_DROPPED_MD = CACHE_DIR / "review-dropped.md"
REVIEW_JSON = CACHE_DIR / "review-ready.json"
CUTOFF_RULE = CACHE_DIR / "cutoff-rule.json"


def load_contacts():
    enriched = CACHE_DIR / "contacts-enriched.json"
    merged = CACHE_DIR / "contacts-merged.json"
    path = enriched if enriched.exists() else merged
    if not path.exists():
        print("ERROR: Run contact-aggregator.py and heuristic-enrich.py first.", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_cutoff_rule():
    if not CUTOFF_RULE.exists():
        print("ERROR: cache/cutoff-rule.json not found. Run analyze-distribution.py first.", file=sys.stderr)
        sys.exit(1)
    with open(CUTOFF_RULE, encoding="utf-8") as f:
        return json.load(f)


def apply_rule(contacts, rule):
    """Apply cutoff thresholds. Data-quality filtering already done in aggregator
    via is_person_entity(); this only decides signal strength."""
    kept = []
    dropped = []
    thresholds = rule["type_thresholds"]

    for c in contacts:
        rt = (c.get("llm") or {}).get("relationship_type", "unknown")
        score = c.get("score", 0)
        min_s = thresholds.get(rt, thresholds.get("unknown", 50))
        if score >= min_s:
            kept.append(c)
        else:
            dropped.append((c, f"score<{min_s}({rt})"))

    return kept, dropped


def format_row(i, c):
    llm = c.get("llm", {}) or {}
    facts = llm.get("key_facts") or []
    # Prefer a "Works at..." or "Phone..." fact; otherwise first fact
    title_co = ""
    for f in facts:
        if f.lower().startswith(("works", "job title", "phone:", "linkedin:")):
            title_co = f[:60]
            break
    if not title_co and facts:
        title_co = (facts[0] or "")[:60]

    circle = llm.get("circle_suggestion") or c.get("auto_circle", "")
    rel_type = llm.get("relationship_type", "")
    context = (llm.get("context_notes") or "").replace("\n", " ").replace("|", "/")[:60]
    name = (c.get("name", "") or "").replace("|", "/")
    email = c.get("email", "") or ""

    return (
        f"| {i} "
        f"| {name} "
        f"| {email} "
        f"| {circle} "
        f"| {rel_type} "
        f"| {c.get('score', 0):.0f} "
        f"| {c.get('gmail_sent', 0)}/{c.get('gmail_received', 0)} "
        f"| {c.get('meeting_count', 0)} "
        f"| {c.get('whatsapp_messages', 0)} "
        f"| {c.get('sms_messages', 0)} "
        f"| {c.get('last_interaction', '') or ''} "
        f"| {title_co.replace('|', '/')} "
        f"| {context} "
        f"| {c.get('slug', '')} |"
    )


def generate_review():
    data = load_contacts()
    rule = load_cutoff_rule()
    contacts = data.get("contacts", [])

    kept, dropped = apply_rule(contacts, rule)
    # Sort kept by score descending
    kept.sort(key=lambda c: -c.get("score", 0))
    dropped.sort(key=lambda x: -x[0].get("score", 0))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sources = set()
    for c in contacts:
        sources.update(c.get("platforms", []))

    header = "| # | Name | Email | Circle | Type | Score | Gmail S/R | Meet | WA | SMS | Last Seen | Title / Company | Context | Slug |"
    separator = "|-|-|-|-|-|-|-|-|-|-|-|-|-|-|"

    # ── Kept (review markdown) ─────────────────────────────
    lines = [
        "# People Directory — Mining Review",
        f"Generated: {now} | Kept: {len(kept)} | Dropped: {len(dropped)} | Sources: {', '.join(sorted(sources))}",
        "",
        "## Instructions",
        "- **Delete** rows that slipped through (spot-check top 20 + random middle + bottom).",
        "- **Fix** names, circles, relationship types as needed.",
        "- **Leave** the slug column alone.",
        "- When done: `python3 contact-review.py --finalize`",
        "",
        "## Rule Applied",
        "```json",
        json.dumps({k: rule[k] for k in ("type_thresholds", "drop_domains", "drop_domains_if_low") if k in rule}, indent=2),
        "```",
        "",
        f"## All Kept Contacts ({len(kept)})",
        "",
        header,
        separator,
    ]
    for i, c in enumerate(kept, 1):
        lines.append(format_row(i, c))

    with open(REVIEW_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Review file written: {REVIEW_MD} ({len(kept)} contacts)", file=sys.stderr)

    # ── Dropped (audit trail) ──────────────────────────────
    drop_lines = [
        "# People Directory — Dropped Contacts (Audit Trail)",
        f"Generated: {now} | Dropped: {len(dropped)}",
        "",
        "Review this for false negatives. To rescue any, add them back to review-contacts.md before finalizing.",
        "",
        "| # | Name | Email | Reason | Score | Type | Last Seen |",
        "|-|-|-|-|-|-|-|",
    ]
    for i, (c, reason) in enumerate(dropped, 1):
        llm = c.get("llm", {}) or {}
        name = (c.get("name", "") or "").replace("|", "/")
        email = c.get("email", "") or ""
        rt = llm.get("relationship_type", "")
        drop_lines.append(
            f"| {i} | {name} | {email} | {reason} | {c.get('score', 0):.0f} | {rt} | {c.get('last_interaction', '') or ''} |"
        )

    with open(REVIEW_DROPPED_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(drop_lines) + "\n")
    print(f"Dropped audit: {REVIEW_DROPPED_MD} ({len(dropped)} contacts)", file=sys.stderr)

    print(f"\nNext: open {REVIEW_MD}, spot-check, then run --finalize", file=sys.stderr)


def finalize_review():
    if not REVIEW_MD.exists():
        print("ERROR: No review-contacts.md. Run without --finalize first.", file=sys.stderr)
        sys.exit(1)

    data = load_contacts()
    contacts_by_slug = {c.get("slug", ""): c for c in data.get("contacts", [])}
    contacts_by_email = {c.get("email", ""): c for c in data.get("contacts", [])}

    with open(REVIEW_MD, encoding="utf-8") as f:
        content = f.read()

    kept_contacts = []
    for line in content.split("\n"):
        line = line.strip()
        if not line.startswith("|") or line.startswith("|-"):
            continue
        # Split by | and strip each cell, but KEEP empty cells to preserve
        # column positions. Only trim the leading/trailing empties from
        # the outer pipe markers.
        raw_cells = line.split("|")
        if raw_cells and raw_cells[0].strip() == "":
            raw_cells = raw_cells[1:]
        if raw_cells and raw_cells[-1].strip() == "":
            raw_cells = raw_cells[:-1]
        cells = [c.strip() for c in raw_cells]
        if len(cells) < 10:
            continue
        if cells[0] in ("#", "Name"):
            continue
        try:
            int(cells[0])
        except ValueError:
            continue

        # Column layout: # Name Email Circle Type Score S/R Meet WA SMS LastSeen Title Context Slug
        name = cells[1] if len(cells) > 1 else ""
        email = cells[2] if len(cells) > 2 else ""
        circle = cells[3] if len(cells) > 3 else ""
        rel_type = cells[4] if len(cells) > 4 else ""
        slug = cells[13] if len(cells) > 13 else (cells[-1] if len(cells) > 10 else "")

        full = contacts_by_slug.get(slug) or contacts_by_email.get(email) or {}

        kept_contacts.append({
            **full,
            "name": name,
            "email": email,
            "circle": circle,
            "relationship_type": rel_type,
            "slug": slug,
        })

    result = {
        "status": "ok",
        "finalized_at": datetime.now(timezone.utc).isoformat(),
        "contacts": kept_contacts,
        "total": len(kept_contacts),
    }

    with open(REVIEW_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"Finalized: {len(kept_contacts)} contacts -> {REVIEW_JSON}", file=sys.stderr)
    print(f"Next: python3 people-seed-from-mine.py --dry-run", file=sys.stderr)


if __name__ == "__main__":
    if "--finalize" in sys.argv:
        finalize_review()
    else:
        generate_review()
