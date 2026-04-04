#!/usr/bin/env python3
"""
update-preferences.py — Process engagement signals and update the preference model.

Reads new entries from preferences/engagement.jsonl, applies multiplicative
weight updates, clamps to safe ranges, and writes updated model.json.

Runs as the nightly preference-update cron.

Usage: python3 update-preferences.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.path.expanduser("~/.openclaw/news-digest-workspace"))
ENGAGEMENT_FILE = WORKSPACE / "preferences" / "engagement.jsonl"
MODEL_FILE = WORKSPACE / "preferences" / "model.json"

# Weight update multipliers
WEIGHT_RULES = {
    "thumbs_up":   {"topic_mult": 1.10, "source_mult": 1.05},
    "thumbs_down": {"topic_mult": 0.85, "source_mult": 0.95},
    "expand":      {"topic_mult": 1.05, "source_mult": 1.00},
}

# Clamp ranges
TOPIC_WEIGHT_MIN = 0.1
TOPIC_WEIGHT_MAX = 3.0
SOURCE_WEIGHT_MIN = 0.5
SOURCE_WEIGHT_MAX = 2.0


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def load_model():
    try:
        with open(MODEL_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "version": 1,
            "updated_at": None,
            "topic_weights": {},
            "source_weights": {},
            "topic_vocabulary": {},
        }


def load_new_events(last_updated):
    """Load engagement events newer than last_updated."""
    events = []
    if not ENGAGEMENT_FILE.exists():
        return events

    with open(ENGAGEMENT_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                # Only process events newer than last update
                if last_updated and event.get("ts", "") <= last_updated:
                    continue
                events.append(event)
            except json.JSONDecodeError:
                continue

    return events


def apply_updates(model, events):
    """Apply engagement events to the preference model."""
    topic_weights = model.get("topic_weights", {})
    source_weights = model.get("source_weights", {})

    changes = {"topics_updated": set(), "sources_updated": set()}

    for event in events:
        action = event.get("action", "")
        rules = WEIGHT_RULES.get(action)
        if not rules:
            continue

        topics = event.get("topics", [])
        source = event.get("source", "")

        # Update topic weights
        for topic in topics:
            old_weight = topic_weights.get(topic, 1.0)
            new_weight = old_weight * rules["topic_mult"]
            new_weight = clamp(new_weight, TOPIC_WEIGHT_MIN, TOPIC_WEIGHT_MAX)
            topic_weights[topic] = round(new_weight, 3)
            if old_weight != new_weight:
                changes["topics_updated"].add(topic)

        # Update source weight
        if source and rules["source_mult"] != 1.0:
            old_weight = source_weights.get(source, 1.0)
            new_weight = old_weight * rules["source_mult"]
            new_weight = clamp(new_weight, SOURCE_WEIGHT_MIN, SOURCE_WEIGHT_MAX)
            source_weights[source] = round(new_weight, 3)
            if old_weight != new_weight:
                changes["sources_updated"].add(source)

    model["topic_weights"] = topic_weights
    model["source_weights"] = source_weights

    return model, changes


def main():
    model = load_model()
    last_updated = model.get("updated_at")

    events = load_new_events(last_updated)

    if not events:
        print(json.dumps({
            "status": "ok",
            "events_processed": 0,
            "message": "no new engagement events",
        }))
        return

    model, changes = apply_updates(model, events)

    # Update metadata
    model["updated_at"] = datetime.now(timezone.utc).isoformat()
    model["version"] = model.get("version", 0) + 1

    # Write updated model
    with open(MODEL_FILE, "w") as f:
        json.dump(model, f, indent=2)

    summary = {
        "status": "ok",
        "events_processed": len(events),
        "topics_updated": sorted(changes["topics_updated"]),
        "sources_updated": sorted(changes["sources_updated"]),
        "model_version": model["version"],
    }

    print(json.dumps(summary))


if __name__ == "__main__":
    main()
