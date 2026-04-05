#!/usr/bin/env python3
"""
update-preferences.py — Process engagement signals and update the preference model.

Reads new entries from preferences/engagement.jsonl, runs a virtual judge
LLM on thumbs-down articles to understand WHY, applies weight updates
with finer-grained topic tags, and writes updated model.json.

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
JUDGE_LOG = WORKSPACE / "preferences" / "judge-log.jsonl"

# Weight update multipliers (base — judge can override)
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

# Brave Search API key for the judge (reuse existing env var)
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")


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
                if last_updated and event.get("ts", "") <= last_updated:
                    continue
                events.append(event)
            except json.JSONDecodeError:
                continue

    return events


def call_judge_llm(title, summary, topics, source, action):
    """Call a cheap LLM via Claude Code to analyze WHY the user reacted.

    Uses `claude -p` with --model haiku (cheap, fast) which is already
    authenticated via the container's OAuth token. No API key needed.

    Returns a dict with:
    - reason: one of IRRELEVANT_SUBTOPIC, LOW_QUALITY, STALE, WRONG_FRAMING, TOO_NICHE
    - subtopics: list of finer-grained topic tags to adjust
    - quality_signal: -1 (bad), 0 (neutral), 1 (good) for the source
    - explanation: brief human-readable reason
    """
    import subprocess

    action_word = "liked" if action == "thumbs_up" else "disliked"

    prompt = (
        f'The user {action_word} this news article in their morning digest.\n\n'
        f'Title: {title}\n'
        f'Summary: {summary[:300]}\n'
        f'Current topics: {", ".join(topics)}\n'
        f'Source: {source}\n\n'
        f'Analyze why the user probably {action_word} this. '
        f'Return ONLY valid JSON with these fields:\n'
        f'{{"reason": "IRRELEVANT_SUBTOPIC|LOW_QUALITY|STALE|WRONG_FRAMING|TOO_NICHE|GOOD_CONTENT|IMPORTANT_TOPIC", '
        f'"subtopics": ["specific_tag_1", "specific_tag_2"], '
        f'"quality_signal": -1, '
        f'"explanation": "one sentence why"}}'
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text", "--model", "haiku"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"  Judge LLM failed: {result.stderr[:100]}", file=sys.stderr)
            return None

        # Parse JSON from the response (may be wrapped in markdown code block)
        output = result.stdout.strip()
        if output.startswith("```"):
            output = output.split("```")[1]
            if output.startswith("json"):
                output = output[4:]
        output = output.strip()

        return json.loads(output)

    except subprocess.TimeoutExpired:
        print("  Judge LLM timed out", file=sys.stderr)
        return None
    except (json.JSONDecodeError, Exception) as e:
        print(f"  Judge LLM parse error: {e}", file=sys.stderr)
        return None


def apply_updates(model, events):
    """Apply engagement events to the preference model, using judge LLM for richer inference."""
    topic_weights = model.get("topic_weights", {})
    source_weights = model.get("source_weights", {})
    topic_vocabulary = model.get("topic_vocabulary", {})

    changes = {"topics_updated": set(), "sources_updated": set(), "judged": 0}
    judge_results = []

    for event in events:
        action = event.get("action", "")
        rules = WEIGHT_RULES.get(action)
        if not rules:
            continue

        topics = event.get("topics", [])
        source = event.get("source", "")
        title = event.get("title", "")
        summary = event.get("summary", "")

        # For thumbs_up/thumbs_down, call the virtual judge for richer signals
        judge = None
        if action in ("thumbs_up", "thumbs_down"):
            judge = call_judge_llm(title, summary, topics, source, action)
            if judge:
                changes["judged"] += 1

                # Log the judge result
                judge_results.append({
                    "ts": event.get("ts"),
                    "title": title,
                    "action": action,
                    "judge": judge,
                })

                # Use judge's subtopics for finer-grained adjustment
                subtopics = judge.get("subtopics", [])
                if subtopics:
                    for st in subtopics:
                        st_key = st.lower().replace(" ", "_").replace("-", "_")
                        old = topic_weights.get(st_key, 1.0)
                        new = old * rules["topic_mult"]
                        topic_weights[st_key] = round(clamp(new, TOPIC_WEIGHT_MIN, TOPIC_WEIGHT_MAX), 3)
                        changes["topics_updated"].add(st_key)

                        # Add to vocabulary if new
                        if st_key not in topic_vocabulary:
                            topic_vocabulary[st_key] = [st.lower()]

                # If judge says LOW_QUALITY or WRONG_FRAMING, hit the source harder
                reason = judge.get("reason", "")
                quality = judge.get("quality_signal", 0)
                if reason in ("LOW_QUALITY", "WRONG_FRAMING") and action == "thumbs_down":
                    # Extra source penalty for quality issues
                    old = source_weights.get(source, 1.0)
                    new = old * 0.90  # Extra 10% penalty on top of normal
                    source_weights[source] = round(clamp(new, SOURCE_WEIGHT_MIN, SOURCE_WEIGHT_MAX), 3)
                    changes["sources_updated"].add(source)

                # If judge says IRRELEVANT_SUBTOPIC, don't penalize the broad topic
                # — only the subtopics were adjusted above
                if reason == "IRRELEVANT_SUBTOPIC":
                    # Skip broad topic adjustment for this event
                    topics = []  # Clear so the loop below skips it

        # Apply standard broad topic + source weight updates
        for topic in topics:
            old_weight = topic_weights.get(topic, 1.0)
            new_weight = old_weight * rules["topic_mult"]
            new_weight = clamp(new_weight, TOPIC_WEIGHT_MIN, TOPIC_WEIGHT_MAX)
            topic_weights[topic] = round(new_weight, 3)
            if old_weight != new_weight:
                changes["topics_updated"].add(topic)

        if source and rules["source_mult"] != 1.0:
            old_weight = source_weights.get(source, 1.0)
            new_weight = old_weight * rules["source_mult"]
            new_weight = clamp(new_weight, SOURCE_WEIGHT_MIN, SOURCE_WEIGHT_MAX)
            source_weights[source] = round(new_weight, 3)
            if old_weight != new_weight:
                changes["sources_updated"].add(source)

    model["topic_weights"] = topic_weights
    model["source_weights"] = source_weights
    model["topic_vocabulary"] = topic_vocabulary

    # Save judge log
    if judge_results:
        with open(JUDGE_LOG, "a") as f:
            for jr in judge_results:
                f.write(json.dumps(jr) + "\n")

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

    # Decay stale topic weights toward 1.0
    # Topics with no engagement in 30+ days drift back to neutral,
    # preventing old signals from permanently skewing the digest.
    topic_weights = model.get("topic_weights", {})
    last_engaged = model.get("topic_last_engaged", {})
    now = datetime.now(timezone.utc)
    decayed = []

    for topic, weight in list(topic_weights.items()):
        last_ts = last_engaged.get(topic)
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                days_idle = (now - last_dt).days
            except (ValueError, TypeError):
                days_idle = 0
        else:
            days_idle = 0

        # After 30 days idle, decay 5% toward 1.0 per day
        if days_idle > 30:
            decay_days = days_idle - 30
            # Move 5% of the distance to 1.0 per idle day (exponential decay)
            for _ in range(min(decay_days, 60)):
                weight = weight + (1.0 - weight) * 0.05
            weight = round(clamp(weight, TOPIC_WEIGHT_MIN, TOPIC_WEIGHT_MAX), 3)
            if weight != topic_weights[topic]:
                topic_weights[topic] = weight
                decayed.append(topic)

    # Update last_engaged timestamps for topics in today's events
    for event in events:
        for topic in event.get("topics", []):
            last_engaged[topic] = now.isoformat()
        # Also update judge subtopics
        # (already handled in apply_updates via topic_weights keys)

    model["topic_weights"] = topic_weights
    model["topic_last_engaged"] = last_engaged
    changes["decayed"] = decayed

    # Update metadata
    model["updated_at"] = now.isoformat()
    model["version"] = model.get("version", 0) + 1

    # Write updated model
    with open(MODEL_FILE, "w") as f:
        json.dump(model, f, indent=2)

    summary = {
        "status": "ok",
        "events_processed": len(events),
        "topics_updated": sorted(changes["topics_updated"]),
        "sources_updated": sorted(changes["sources_updated"]),
        "judged_by_llm": changes["judged"],
        "decayed": changes.get("decayed", []),
        "model_version": model["version"],
    }

    print(json.dumps(summary))


if __name__ == "__main__":
    main()
