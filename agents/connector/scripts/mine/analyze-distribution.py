#!/usr/bin/env python3
"""
analyze-distribution.py — Analyze enriched contacts, suggest filter cutoff.

Loads cache/enriched-contacts.json and produces:
1. Score histogram (log-scale ASCII bars)
2. Breakdown by LLM relationship_type
3. Natural-gap detection in the score distribution
4. Composite cutoff rule recommendation
5. Preview of what would be kept vs dropped

Usage:
  python3 analyze-distribution.py
"""

import io
import json
import math
import os
import sys
from collections import Counter, defaultdict

# Force UTF-8 stdout to handle international characters
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR


def load_enriched():
    path = CACHE_DIR / "enriched-contacts.json"
    if not path.exists():
        print(f"ERROR: {path} not found. Run llm-enrich.py first.", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def bucket_score(score):
    """Group scores into log buckets for the histogram."""
    if score < 10:
        return f"{int(score):3d}"
    if score < 100:
        return f"{int(score // 10) * 10:3d}s"
    if score < 1000:
        return f"{int(score // 100) * 100:4d}s"
    return f"{int(score // 1000) * 1000:5d}s+"


def draw_histogram(contacts):
    """ASCII histogram of score distribution."""
    buckets = defaultdict(int)
    for c in contacts:
        buckets[bucket_score(c.get("score", 0))] += 1

    # Sort by bucket numeric value
    def sort_key(b):
        digits = "".join(c for c in b if c.isdigit())
        return int(digits) if digits else 0

    max_count = max(buckets.values()) if buckets else 1
    bar_width = 50

    print("\n=== Score Distribution ===")
    for bucket in sorted(buckets.keys(), key=sort_key):
        count = buckets[bucket]
        bar_len = int((count / max_count) * bar_width)
        bar = "#" * bar_len
        print(f"  {bucket:>8s} | {bar} {count}")


def breakdown_by_type(contacts):
    """Breakdown by LLM relationship_type."""
    by_type = defaultdict(lambda: {"count": 0, "scores": []})
    for c in contacts:
        rt = (c.get("llm") or {}).get("relationship_type", "unknown")
        by_type[rt]["count"] += 1
        by_type[rt]["scores"].append(c.get("score", 0))

    print("\n=== By Relationship Type ===")
    print(f"  {'Type':<15s} {'Count':>6s} {'Median':>8s} {'Max':>8s} {'Min':>6s}")
    for rt in sorted(by_type.keys(), key=lambda k: -by_type[k]["count"]):
        info = by_type[rt]
        scores = sorted(info["scores"])
        median = scores[len(scores) // 2] if scores else 0
        print(f"  {rt:<15s} {info['count']:>6d} {median:>8.0f} {max(scores):>8.0f} {min(scores):>6.0f}")
    return by_type


def find_natural_gaps(contacts):
    """Find significant gaps in the sorted score distribution (log scale)."""
    scores = sorted([c.get("score", 0) for c in contacts], reverse=True)
    if len(scores) < 10:
        return []

    gaps = []
    # Look at adjacent rank positions, compute log ratio
    for i in range(5, min(len(scores) - 5, 500)):
        if scores[i] <= 0:
            continue
        prev = scores[i - 1]
        curr = scores[i]
        if curr > 0 and prev > 0:
            ratio = prev / curr
            if ratio > 1.3:  # 30%+ drop
                gaps.append((i, prev, curr, ratio))

    gaps.sort(key=lambda g: -g[3])
    return gaps[:10]


# Per-type minimum scores. Data-quality filtering (resources, newsletters,
# one-way, handles) is done upstream in contact-aggregator.is_person_entity().
# This layer only decides signal strength per LLM-classified relationship type.
TYPE_THRESHOLDS = {
    "family": 0,        # always keep
    "friend": 5,        # keep all LLM-classified friends
    "colleague": 5,     # keep all colleagues (ex-Example Corp etc.)
    "client": 5,        # keep all clients
    "acquaintance": 5,  # keep all (some recent contacts score low)
    "vendor": 50,       # strict — service providers with real engagement
    "recruiter": 50,    # strict — meaningful recruiter conversations
    "unknown": 50,
}


def suggest_cutoff(contacts, by_type):
    """Cutoff rule = per-type score thresholds only. Data-quality gates are
    applied upstream in contact-aggregator.is_person_entity()."""
    return {
        "type_thresholds": TYPE_THRESHOLDS,
        "note": "Per-type score thresholds. Data-quality filtering is upstream in is_person_entity().",
    }


def apply_rule(contacts, rule):
    """Apply per-type score thresholds. Contacts were already filtered for
    data-quality by the aggregator."""
    kept = []
    dropped = []
    thresholds = rule["type_thresholds"]

    for c in contacts:
        rt = (c.get("llm") or {}).get("relationship_type", "unknown")
        score = c.get("score", 0)
        min_score = thresholds.get(rt, thresholds["unknown"])
        if score >= min_score:
            kept.append(c)
        else:
            dropped.append(c)

    return kept, dropped


def show_borderline(kept, dropped, n=10):
    """Show contacts just above and below the cutoff."""
    kept_sorted = sorted(kept, key=lambda c: c.get("score", 0))
    dropped_sorted = sorted(dropped, key=lambda c: -c.get("score", 0))

    print(f"\n=== Top {n} Kept (just above cutoff) ===")
    for c in kept_sorted[:n]:
        rt = (c.get("llm") or {}).get("relationship_type", "?")
        print(f"  {c.get('score', 0):6.0f}  [{rt:<12s}]  {c.get('name', ''):<30s}  {c.get('email', '')}")

    print(f"\n=== Top {n} Dropped (just below cutoff) ===")
    for c in dropped_sorted[:n]:
        rt = (c.get("llm") or {}).get("relationship_type", "?")
        print(f"  {c.get('score', 0):6.0f}  [{rt:<12s}]  {c.get('name', ''):<30s}  {c.get('email', '')}")


def show_high_score_drops(dropped, n=10):
    """Show the highest-score contacts that got dropped — these are the worrying ones."""
    high = sorted(dropped, key=lambda c: -c.get("score", 0))[:n]
    if high:
        print(f"\n=== Highest-score Dropped (check for false negatives) ===")
        for c in high:
            rt = (c.get("llm") or {}).get("relationship_type", "?")
            print(f"  {c.get('score', 0):6.0f}  [{rt:<12s}]  {c.get('name', ''):<30s}  {c.get('email', '')}")


def main():
    data = load_enriched()
    contacts = data.get("contacts", [])
    print(f"Loaded {len(contacts)} enriched contacts")

    draw_histogram(contacts)
    by_type = breakdown_by_type(contacts)

    gaps = find_natural_gaps(contacts)
    if gaps:
        print("\n=== Top 5 Natural Gaps (log ratio) ===")
        for rank, prev, curr, ratio in gaps[:5]:
            print(f"  rank {rank}: {prev:.0f} -> {curr:.0f}  (ratio {ratio:.2f})")

    rule = suggest_cutoff(contacts, by_type)
    print(f"\n=== Proposed Cutoff Rule ===")
    print(f"  Per-type minimum scores (data-quality filtering happens upstream):")
    for rt, thresh in rule["type_thresholds"].items():
        label = "always keep" if thresh == 0 else ("never keep" if thresh >= 999 else f">= {thresh}")
        print(f"    {rt:<15s} {label}")

    kept, dropped = apply_rule(contacts, rule)
    print(f"\n  Keeps: {len(kept)} | Drops: {len(dropped)}")

    # Break down by type after filtering
    kept_by_type = Counter((c.get("llm") or {}).get("relationship_type", "unknown") for c in kept)
    dropped_by_type = Counter((c.get("llm") or {}).get("relationship_type", "unknown") for c in dropped)
    print(f"\n  Kept by type:    {dict(kept_by_type.most_common())}")
    print(f"  Dropped by type: {dict(dropped_by_type.most_common())}")

    show_borderline(kept, dropped)
    show_high_score_drops(dropped)

    # Save the rule for contact-review.py to use
    rule_path = CACHE_DIR / "cutoff-rule.json"
    with open(rule_path, "w", encoding="utf-8") as f:
        json.dump(rule, f, indent=2)
    print(f"\nRule saved to {rule_path}")


if __name__ == "__main__":
    main()
