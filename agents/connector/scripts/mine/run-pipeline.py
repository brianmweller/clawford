#!/usr/bin/env python3
"""
run-pipeline.py — Run the full contact mining pipeline (steps 1-4).

Runs miners, aggregator, LLM enrichment, and review generation.
Gmail runs first (slowest), then others in sequence. WhatsApp and
Google Messages must be run separately (VPS and Chrome respectively).

Usage:
  python3 run-pipeline.py              # Full pipeline
  python3 run-pipeline.py --skip-llm   # Skip LLM enrichment
  python3 run-pipeline.py --only-agg   # Only run aggregator + review
"""

import os
import subprocess
import sys

MINE_DIR = os.path.dirname(os.path.abspath(__file__))


def run(script, description, optional=False):
    """Run a mining script and report status."""
    path = os.path.join(MINE_DIR, script)
    if not os.path.exists(path):
        print(f"  SKIP: {script} not found")
        return False

    print(f"\n{'=' * 60}")
    print(f"  {description}")
    print(f"  Running: python3 {script}")
    print(f"{'=' * 60}\n")

    result = subprocess.run(
        [sys.executable, path],
        cwd=MINE_DIR,
    )

    if result.returncode != 0:
        if optional:
            print(f"\n  WARNING: {script} failed (optional, continuing)")
            return False
        else:
            print(f"\n  ERROR: {script} failed with code {result.returncode}")
            return False
    return True


def main():
    skip_llm = "--skip-llm" in sys.argv
    only_agg = "--only-agg" in sys.argv

    print("=" * 60)
    print("  Contact Mining Pipeline")
    print("  Huckle Cat — People Directory Seed")
    print("=" * 60)

    if not only_agg:
        # Step 1: Run miners
        print("\n\nPHASE 1: MINING\n")

        run("contacts-mine.py", "Google Contacts — Name + phone lookup table")
        run("gmail-mine.py", "Gmail — Full body mining (may take 15-30 min)")
        run("gcal-mine.py", "Google Calendar — Attendee extraction")
        run("krisp-mine.py", "Krisp — Transcript participants", optional=True)
        run("workflowy-read.py", "Workflowy — Contact name cache", optional=True)

        print("\n" + "=" * 60)
        print("  NOTE: WhatsApp and Google Messages must be run separately:")
        print("  - WhatsApp: ssh openclaw@vps 'python3 /tmp/whatsapp-mine.py' > cache/mined-whatsapp.json")
        print("  - Messages: Paste messages-extract.js in Chrome DevTools console")
        print("=" * 60)

    # Step 2: Aggregate
    print("\n\nPHASE 2: AGGREGATION\n")
    if not run("contact-aggregator.py", "Merge all sources, dedupe, rank"):
        sys.exit(1)

    # Step 3: LLM enrichment
    if not skip_llm:
        print("\n\nPHASE 3: LLM ENRICHMENT\n")
        run("llm-enrich.py", "gpt-5.4-nano — per-person fact extraction", optional=True)

    # Step 4: Review
    print("\n\nPHASE 4: REVIEW GENERATION\n")
    run("contact-review.py", "Generate review markdown")

    print("\n" + "=" * 60)
    print("  Pipeline complete!")
    print("")
    print("  Next steps:")
    print("  1. Review: cache/review-contacts.md")
    print("     Delete unwanted rows, fix circles/names")
    print("  2. Finalize: python3 contact-review.py --finalize")
    print("  3. Preview:  python3 people-seed-from-mine.py --dry-run")
    print("  4. Seed:     python3 people-seed-from-mine.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
