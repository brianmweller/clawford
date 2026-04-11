#!/usr/bin/env python3
"""
contact-review.py — Generate a review markdown or finalize after edits.

Default: reads enriched-contacts.json and generates review-contacts.md
         for Sam to review in his editor.

--finalize: re-reads review-contacts.md (after Sam's edits), parses
            the remaining rows, and writes review-ready.json for seeding.

Usage:
  python3 contact-review.py               # Generate review markdown
  python3 contact-review.py --finalize    # Parse edited markdown → JSON

Input: cache/enriched-contacts.json (or aggregated-contacts.json)
Output: cache/review-contacts.md + cache/review-ready.json
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR

REVIEW_MD = CACHE_DIR / "review-contacts.md"
REVIEW_JSON = CACHE_DIR / "review-ready.json"


def load_contacts():
    """Load enriched or aggregated contacts."""
    enriched = CACHE_DIR / "enriched-contacts.json"
    aggregated = CACHE_DIR / "aggregated-contacts.json"

    path = enriched if enriched.exists() else aggregated
    if not path.exists():
        print("ERROR: Run aggregator (and optionally llm-enrich) first.", file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        return json.load(f)


def generate_review():
    """Generate the review markdown file."""
    data = load_contacts()
    contacts = data.get("contacts", [])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Count sources
    sources = set()
    for c in contacts:
        sources.update(c.get("platforms", []))

    lines = [
        f"# People Directory — Mining Review",
        f"Generated: {now} | Contacts: {len(contacts)} | Sources: {', '.join(sorted(sources))}",
        "",
        "## Instructions",
        "- **Delete** rows you don't want to create people files for",
        "- **Fix** names, circles, and relationship types as needed",
        "- **Leave** the slug column alone (auto-generated)",
        "- When done, run: `python3 contact-review.py --finalize`",
        "",
    ]

    # Tier contacts
    tiers = [
        ("Tier 1: High Importance (score >= 50)", lambda c: c.get("score", 0) >= 50),
        ("Tier 2: Medium Importance (score 15-49)", lambda c: 15 <= c.get("score", 0) < 50),
        ("Tier 3: Low Importance (score 5-14)", lambda c: c.get("score", 0) < 15),
    ]

    header = "| # | Name | Email | Circle | Type | Score | Gmail S/R | Meet | WA | SMS | Last Seen | Title / Company | Context | Slug |"
    separator = "|-|-|-|-|-|-|-|-|-|-|-|-|-|-|"

    for tier_name, tier_filter in tiers:
        tier_contacts = [c for c in contacts if tier_filter(c) and not c.get("exists_in_brain")]
        if not tier_contacts:
            continue

        lines.append(f"## {tier_name}")
        lines.append("")
        lines.append(header)
        lines.append(separator)

        for i, c in enumerate(tier_contacts, 1):
            llm = c.get("llm", {})
            sig = c.get("signature", {})
            title_co = ""
            if sig.get("title") and sig.get("company"):
                title_co = f"{sig['title']} / {sig['company']}"
            elif llm.get("key_facts"):
                # Use first fact as fallback
                title_co = llm["key_facts"][0][:40] if llm["key_facts"] else ""

            circle = llm.get("circle_suggestion", c.get("auto_circle", ""))
            rel_type = llm.get("relationship_type", "")
            context = llm.get("context_notes", "")[:60]

            lines.append(
                f"| {i} "
                f"| {c.get('name', '')} "
                f"| {c.get('email', '')} "
                f"| {circle} "
                f"| {rel_type} "
                f"| {c.get('score', 0)} "
                f"| {c.get('gmail_sent', 0)}/{c.get('gmail_received', 0)} "
                f"| {c.get('meeting_count', 0)} "
                f"| {c.get('whatsapp_messages', 0)} "
                f"| {c.get('sms_messages', 0)} "
                f"| {c.get('last_interaction', '')} "
                f"| {title_co} "
                f"| {context} "
                f"| {c.get('slug', '')} |"
            )

        lines.append("")

    # Already exists section
    existing = [c for c in contacts if c.get("exists_in_brain")]
    if existing:
        lines.append("## Already in People Directory")
        lines.append("")
        lines.append("| # | Name | Email | Slug | Current Data |")
        lines.append("|-|-|-|-|-|")
        for i, c in enumerate(existing, 1):
            lines.append(f"| {i} | {c.get('name', '')} | {c.get('email', '')} | {c.get('slug', '')} | score={c.get('score', 0)} |")
        lines.append("")

    # Unresolved
    for group, label in [
        ("unresolved_krisp", "Unresolved Krisp Names (no email match)"),
        ("unmatched_whatsapp", "Unmatched WhatsApp Contacts"),
        ("unmatched_messages", "Unmatched Google Messages Contacts"),
    ]:
        items = data.get(group, [])
        if items:
            lines.append(f"## {label}")
            lines.append("")
            lines.append("| # | Name | Count | Last Seen |")
            lines.append("|-|-|-|-|")
            for i, item in enumerate(items, 1):
                name = item.get("name", "")
                count = item.get("meeting_count", item.get("message_count", 0))
                last = item.get("last_meeting", item.get("last_message", ""))
                lines.append(f"| {i} | {name} | {count} | {last} |")
            lines.append("")

    # Write markdown
    with open(REVIEW_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Review file written: {REVIEW_MD}", file=sys.stderr)
    print(f"{len(contacts)} contacts. Edit the file, then run: python3 contact-review.py --finalize", file=sys.stderr)


def finalize_review():
    """Parse the edited review markdown back into JSON."""
    if not REVIEW_MD.exists():
        print("ERROR: No review-contacts.md found. Run without --finalize first.", file=sys.stderr)
        sys.exit(1)

    # Also load the enriched/aggregated data for full contact details
    data = load_contacts()
    contacts_by_slug = {c.get("slug", ""): c for c in data.get("contacts", [])}
    contacts_by_email = {c.get("email", ""): c for c in data.get("contacts", [])}

    with open(REVIEW_MD, encoding="utf-8") as f:
        content = f.read()

    # Parse table rows (lines starting with |)
    kept_contacts = []
    for line in content.split("\n"):
        line = line.strip()
        if not line.startswith("|") or line.startswith("|-"):
            continue
        # Skip header rows
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c]  # Remove empty from leading/trailing |

        if len(cells) < 10:
            continue
        if cells[0] == "#" or cells[0] == "Name":
            continue

        try:
            int(cells[0])
        except ValueError:
            continue

        # Parse: #, Name, Email, Circle, Type, Score, Gmail, Meet, WA, SMS, Last, Title, Context, Slug
        name = cells[1] if len(cells) > 1 else ""
        email = cells[2] if len(cells) > 2 else ""
        circle = cells[3] if len(cells) > 3 else ""
        rel_type = cells[4] if len(cells) > 4 else ""
        slug = cells[-1] if len(cells) > 13 else ""

        # Find full contact data
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

    with open(REVIEW_JSON, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Finalized: {len(kept_contacts)} contacts → {REVIEW_JSON}", file=sys.stderr)
    print(f"Next: python3 people-seed-from-mine.py --dry-run", file=sys.stderr)


if __name__ == "__main__":
    if "--finalize" in sys.argv:
        finalize_review()
    else:
        generate_review()
