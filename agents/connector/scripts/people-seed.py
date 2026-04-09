#!/usr/bin/env python3
"""
people-seed.py — Create initial people files from a CSV.

One-time script to bootstrap the people directory with key relationships.
Run locally or on VPS before the first morning nudge cron.

Usage:
  python3 people-seed.py                          # Default: read people-seed.csv
  python3 people-seed.py --csv /path/to/list.csv  # Custom CSV path
  python3 people-seed.py --dry-run                # Preview without writing

CSV columns:
  name, slug, relationship, relationship_type, circles, preferred_channel,
  tone, last_interaction, context_notes

Output: Creates one .md file per person in ~/Dropbox/openclaw-backup/people/
"""

import csv
import os
import sys
from datetime import datetime, timezone

BRAIN_PEOPLE = os.path.expanduser("~/Dropbox/openclaw-backup/people")
DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "people-seed.csv")

TEMPLATE = """# {name}

- **slug:** {slug}
- **circles:** {circles}
- **relationship:** {relationship}
- **relationship_type:** {relationship_type}
- **preferred_channel:** {preferred_channel}
- **tone:** {tone}
- **email:** —
- **phone:** —
- **platforms:** {preferred_channel}
- **last_interaction:** {last_interaction}
- **context_notes:** {context_notes}
- **notes:** Seeded by people-seed.py on {seed_date}
"""


def parse_args():
    csv_path = DEFAULT_CSV
    dry_run = "--dry-run" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--csv" and i + 1 < len(sys.argv):
            csv_path = sys.argv[i + 1]

    return csv_path, dry_run


def main():
    csv_path, dry_run = parse_args()

    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}")
        sys.exit(1)

    if not dry_run:
        os.makedirs(BRAIN_PEOPLE, exist_ok=True)

    seed_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    created = 0
    skipped = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            slug = row.get("slug", "").strip()
            if not slug:
                continue

            filepath = os.path.join(BRAIN_PEOPLE, f"{slug}.md")

            if os.path.exists(filepath):
                print(f"  SKIP: {slug} (already exists)")
                skipped += 1
                continue

            content = TEMPLATE.format(
                name=row.get("name", slug).strip(),
                slug=slug,
                circles=row.get("circles", "").strip(),
                relationship=row.get("relationship", "").strip(),
                relationship_type=row.get("relationship_type", "").strip(),
                preferred_channel=row.get("preferred_channel", "").strip(),
                tone=row.get("tone", "casual").strip(),
                last_interaction=row.get("last_interaction", "").strip(),
                context_notes=row.get("context_notes", "").strip(),
                seed_date=seed_date,
            ).strip() + "\n"

            if dry_run:
                print(f"  [DRY RUN] Would create: {filepath}")
                print(f"            {row.get('name', slug)} — {row.get('circles', '')}")
            else:
                with open(filepath, "w", encoding="utf-8") as out:
                    out.write(content)
                print(f"  Created: {filepath}")

            created += 1

    print(f"\nDone. Created: {created}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
