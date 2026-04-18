#!/usr/bin/env python3
"""
transcript-metrics.py — Extract deterministic metrics from speaker-attributed transcripts.

Parses Krisp transcript format (**Speaker Name | HH:MM**\\ntext) to compute
talk ratio, turn length, question count, filler words, etc. No LLM calls.

Usage:
  python3 transcript-metrics.py --event-id EVENT_ID
  python3 transcript-metrics.py --file path/to/pending-debrief.json

Output JSON:
  {
    "status": "ok",
    "event_id": "...",
    "meeting_title": "...",
    "metrics": { ... }
  }
"""

import json
import os
import re
import sys

WORKSPACE = os.path.expanduser("~/.clawford/meetings-coach-workspace")
CACHE_DIR = os.path.join(WORKSPACE, "cache")
CONFIG_PATH = os.path.join(WORKSPACE, "meeting-config.json")

# Regex for Krisp speaker turns: **Speaker Name | HH:MM**
SPEAKER_RE = re.compile(r"\*\*(.+?)\s*\|\s*(\d{1,2}:\d{2})\*\*")


def parse_args():
    event_id = None
    filepath = None

    for i, arg in enumerate(sys.argv):
        if arg == "--event-id" and i + 1 < len(sys.argv):
            event_id = sys.argv[i + 1]
        if arg == "--file" and i + 1 < len(sys.argv):
            filepath = sys.argv[i + 1]

    return event_id, filepath


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def parse_turns(transcript_text):
    """Parse transcript into a list of (speaker, timestamp, text) turns."""
    turns = []
    # Split on the speaker pattern
    parts = SPEAKER_RE.split(transcript_text)

    # parts = [preamble, speaker1, time1, text1, speaker2, time2, text2, ...]
    # Skip preamble (index 0), then take groups of 3
    i = 1
    while i + 2 < len(parts):
        speaker = parts[i].strip()
        timestamp = parts[i + 1].strip()
        text = parts[i + 2].strip()
        if text:
            turns.append({
                "speaker": speaker,
                "timestamp": timestamp,
                "text": text,
                "word_count": len(text.split()),
            })
        i += 3

    return turns


def is_brian(speaker_name, brian_names):
    """Check if a speaker name matches Sam."""
    speaker_lower = speaker_name.lower().strip()
    for name in brian_names:
        if name.lower() in speaker_lower or speaker_lower in name.lower():
            return True
    return False


def count_fillers(text, filler_words):
    """Count filler word occurrences in text."""
    text_lower = text.lower()
    count = 0
    found = []
    for filler in filler_words:
        # Use word boundary matching for single words, substring for phrases
        if " " in filler:
            occurrences = text_lower.count(filler)
        else:
            occurrences = len(re.findall(r"\b" + re.escape(filler) + r"\b", text_lower))
        if occurrences > 0:
            count += occurrences
            found.append(filler)
    return count, found


def compute_metrics(turns, brian_names, filler_words):
    """Compute all metrics from parsed turns."""
    brian_turns = [t for t in turns if is_brian(t["speaker"], brian_names)]
    other_turns = [t for t in turns if not is_brian(t["speaker"], brian_names)]

    brian_words = sum(t["word_count"] for t in brian_turns)
    other_words = sum(t["word_count"] for t in other_turns)
    total_words = brian_words + other_words

    # Talk ratio
    talk_ratio = round(brian_words / total_words, 2) if total_words > 0 else 0

    # Turn lengths
    avg_brian = round(brian_words / len(brian_turns), 1) if brian_turns else 0
    avg_other = round(other_words / len(other_turns), 1) if other_turns else 0

    # Longest Sam turn
    longest = max(brian_turns, key=lambda t: t["word_count"]) if brian_turns else None
    longest_info = None
    if longest:
        preview = longest["text"][:120]
        if len(longest["text"]) > 120:
            preview += "..."
        longest_info = {
            "words": longest["word_count"],
            "timestamp": longest["timestamp"],
            "preview": preview,
        }

    # Questions (count ? in Sam's turns)
    brian_text_all = " ".join(t["text"] for t in brian_turns)
    question_count = brian_text_all.count("?")

    # Fillers
    filler_count, filler_examples = count_fillers(brian_text_all, filler_words)

    # Unique speakers
    speakers = list(dict.fromkeys(t["speaker"] for t in turns))

    return {
        "brian_word_count": brian_words,
        "others_word_count": other_words,
        "talk_ratio": talk_ratio,
        "brian_turn_count": len(brian_turns),
        "others_turn_count": len(other_turns),
        "avg_brian_turn_words": avg_brian,
        "avg_others_turn_words": avg_other,
        "longest_brian_turn": longest_info,
        "brian_question_count": question_count,
        "brian_filler_count": filler_count,
        "brian_filler_examples": list(dict.fromkeys(filler_examples)),
        "speakers": speakers,
        "speaker_count": len(speakers),
        "transcript_word_count": total_words,
    }


def main():
    event_id, filepath = parse_args()

    # Find the debrief file
    if filepath:
        debrief_path = filepath
    elif event_id:
        debrief_path = os.path.join(CACHE_DIR, f"pending-debrief-{event_id}.json")
    else:
        print(json.dumps({"status": "error", "message": "Specify --event-id or --file"}))
        sys.exit(1)

    if not os.path.exists(debrief_path):
        print(json.dumps({"status": "error", "message": f"File not found: {debrief_path}"}))
        sys.exit(1)

    with open(debrief_path) as f:
        debrief = json.load(f)

    transcript_text = debrief.get("transcript_text", "")
    if not transcript_text:
        print(json.dumps({
            "status": "no_transcript",
            "event_id": debrief.get("event_id", ""),
            "message": "No transcript text available for metrics",
        }))
        return

    # Load config
    config = load_config()
    coaching = config.get("coaching", {})
    brian_names = coaching.get("brian_speaker_names", ["Sam Smith", "Sam"])
    filler_words = coaching.get("filler_words", [
        "like", "um", "uh", "you know", "basically",
        "I mean", "sort of", "kind of", "right", "actually",
    ])
    min_length = coaching.get("min_transcript_length", 500)

    if len(transcript_text) < min_length:
        print(json.dumps({
            "status": "too_short",
            "event_id": debrief.get("event_id", ""),
            "transcript_length": len(transcript_text),
            "min_length": min_length,
            "message": "Transcript too short for meaningful coaching",
        }))
        return

    # Parse and compute
    turns = parse_turns(transcript_text)

    if not turns:
        print(json.dumps({
            "status": "no_turns",
            "event_id": debrief.get("event_id", ""),
            "message": "Could not parse speaker turns from transcript",
        }))
        return

    # Check if Sam was found
    brian_found = any(is_brian(t["speaker"], brian_names) for t in turns)
    if not brian_found:
        print(json.dumps({
            "status": "no_brian",
            "event_id": debrief.get("event_id", ""),
            "speakers_found": list(dict.fromkeys(t["speaker"] for t in turns)),
            "message": "Could not identify Sam in transcript. Check coaching.brian_speaker_names in config.",
        }))
        return

    metrics = compute_metrics(turns, brian_names, filler_words)

    print(json.dumps({
        "status": "ok",
        "event_id": debrief.get("event_id", ""),
        "meeting_title": debrief.get("meeting_title", ""),
        "metrics": metrics,
    }, indent=2))


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
