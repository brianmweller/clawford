#!/usr/bin/env python3
"""
people-seed-from-mine.py — Create people files + brain facts from mined data.

Reads the finalized review JSON and creates:
1. People files in ~/Dropbox/openclaw-backup/people/
2. Identity facts in ~/Dropbox/openclaw-backup/facts/YYYY-MM.md

Usage:
  python3 people-seed-from-mine.py --dry-run         # Preview
  python3 people-seed-from-mine.py                    # Create files
  python3 people-seed-from-mine.py --enrich-existing  # Also update existing files

Input: cache/review-ready.json
Output: people files + facts file
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, email_to_slug, name_to_slug

BRAIN_PEOPLE = os.path.expanduser("~/Dropbox/openclaw-backup/people")
BRAIN_FACTS = os.path.expanduser("~/Dropbox/openclaw-backup/facts")

PERSON_TEMPLATE = """# {name}

- **slug:** {slug}
- **circles:** {circles}
- **relationship:** {relationship}
- **relationship_type:** {relationship_type}
- **preferred_channel:** {preferred_channel}
- **tone:** {tone}
- **email:** {email}
- **phone:** {phone}
- **platforms:** {platforms}
- **last_interaction:** {last_interaction}
- **context_notes:** {context_notes}
- **notes:** Auto-created by mining pipeline on {seed_date}. Score: {score}.
"""

FACT_TEMPLATE = """
---

- **id:** {fact_id}
- **content:** {content}
- **subject:** {subject}
- **source_type:** observed
- **source_detail:** {source_detail}
- **source_agent:** connector
- **confidence:** 0.7
- **category:** {category}
- **recorded_at:** {recorded_at}
"""


def main():
    dry_run = "--dry-run" in sys.argv
    enrich_existing = "--enrich-existing" in sys.argv

    review_path = CACHE_DIR / "review-ready.json"
    if not review_path.exists():
        print("ERROR: Run contact-review.py --finalize first.", file=sys.stderr)
        sys.exit(1)

    with open(review_path) as f:
        data = json.load(f)

    contacts = data.get("contacts", [])
    seed_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recorded_at = datetime.now(timezone.utc).isoformat()
    fact_month = datetime.now(timezone.utc).strftime("%Y-%m")

    created = 0
    skipped = 0
    enriched = 0
    facts = []
    fact_seq = 1

    if not dry_run:
        os.makedirs(BRAIN_PEOPLE, exist_ok=True)
        os.makedirs(BRAIN_FACTS, exist_ok=True)

    for contact in contacts:
        slug = contact.get("slug", "")
        if not slug:
            name = contact.get("name", "")
            slug = name_to_slug(name) if name else email_to_slug(contact.get("email", ""))
        if not slug:
            continue

        filepath = os.path.join(BRAIN_PEOPLE, f"{slug}.md")
        llm = contact.get("llm", {})
        sig = contact.get("signature", {})

        if os.path.exists(filepath):
            if enrich_existing:
                # Update last_interaction if newer
                # (simplified: just log what would change)
                if dry_run:
                    print(f"  ENRICH: {slug} (would update last_interaction)", file=sys.stderr)
                enriched += 1
            else:
                skipped += 1
            continue

        # Build people file
        circle = contact.get("circle", contact.get("auto_circle", "professional-outer"))
        rel_type = llm.get("relationship_type", contact.get("relationship_type", ""))
        tone = llm.get("tone", "professional")
        context = llm.get("context_notes", "")
        phone = sig.get("phone", "—")
        platforms = ", ".join(contact.get("platforms", ["email"]))
        preferred = "email"
        if "whatsapp" in contact.get("platforms", []):
            preferred = "WhatsApp"
        elif "sms" in contact.get("platforms", []):
            preferred = "iMessage"

        content = PERSON_TEMPLATE.format(
            name=contact.get("name", slug),
            slug=slug,
            circles=circle,
            relationship=rel_type or "contact",
            relationship_type=rel_type or "",
            preferred_channel=preferred,
            tone=tone,
            email=contact.get("email", "—"),
            phone=phone,
            platforms=platforms,
            last_interaction=contact.get("last_interaction", seed_date),
            context_notes=context or "—",
            seed_date=seed_date,
            score=contact.get("score", 0),
        ).strip() + "\n"

        if dry_run:
            print(f"  CREATE: {filepath} ({contact.get('name', slug)}, {circle})", file=sys.stderr)
        else:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

        created += 1

        # Generate facts from signature and LLM
        fact_sources = []

        if sig.get("title") and sig.get("company"):
            fact_sources.append({
                "content": f"{contact.get('name', slug)} works at {sig['company']} as {sig['title']}",
                "source_detail": "Extracted from email signature",
                "category": "established",
            })

        if sig.get("phone"):
            fact_sources.append({
                "content": f"{contact.get('name', slug)}'s phone number is {sig['phone']}",
                "source_detail": "Extracted from email signature",
                "category": "identity",
            })

        for llm_fact in llm.get("key_facts", []):
            if llm_fact and "signature" not in llm_fact.lower():
                fact_sources.append({
                    "content": llm_fact,
                    "source_detail": "Inferred from communication patterns by LLM",
                    "category": "established",
                })

        for fs in fact_sources:
            fact_id = f"connector-{seed_date}-{fact_seq:03d}"
            fact_seq += 1
            facts.append(FACT_TEMPLATE.format(
                fact_id=fact_id,
                content=fs["content"],
                subject=slug,
                source_detail=fs["source_detail"],
                category=fs["category"],
                recorded_at=recorded_at,
            ))

    # Write facts file
    if facts and not dry_run:
        facts_path = os.path.join(BRAIN_FACTS, f"{fact_month}.md")
        mode = "a" if os.path.exists(facts_path) else "w"
        with open(facts_path, mode, encoding="utf-8") as f:
            if mode == "w":
                f.write(f"# Facts — {fact_month}\n")
            for fact in facts:
                f.write(fact)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Results:", file=sys.stderr)
    print(f"  People files created: {created}", file=sys.stderr)
    print(f"  Skipped (exist): {skipped}", file=sys.stderr)
    print(f"  Enriched (existing): {enriched}", file=sys.stderr)
    print(f"  Facts generated: {len(facts)}", file=sys.stderr)

    if not dry_run and facts:
        print(f"  Facts written to: {BRAIN_FACTS}/{fact_month}.md", file=sys.stderr)


if __name__ == "__main__":
    main()
