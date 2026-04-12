#!/usr/bin/env python3
"""
clean-sources.py — Per-source data-quality pass.

Reads each mined-*.json file and writes mined-clean-*.json with garbage
entities removed. This is the ONLY place where we filter for data quality;
the aggregator trusts its inputs.

Uses mining_utils.is_garbage_entity() as the single source of truth.

Usage:
  python3 clean-sources.py
"""

import io
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, is_garbage_entity, load_config, normalize_name, sanitize_signature

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def clean_gmail(data, config):
    """Clean Gmail output: drop garbage emails, sanitize cached signatures."""
    contacts = data.get("contacts", {})
    kept = {}
    drops = Counter()
    for email, c in contacts.items():
        names = c.get("display_names", [])
        name = names[0] if names else ""
        garbage, reason = is_garbage_entity(email, name, config)
        if garbage:
            drops[reason] += 1
            continue
        # Sanitize the cached signature: drop junk fields (quote markers, footers)
        if c.get("signature"):
            c["signature"] = sanitize_signature(c["signature"])
        kept[email] = c
    return {**data, "contacts": kept, "clean_drops": dict(drops)}


def clean_gcal(data, config):
    """Clean Calendar output: drop rooms, resources, facility codes."""
    contacts = data.get("contacts", {})
    kept = {}
    drops = Counter()
    for email, c in contacts.items():
        names = c.get("display_names", [])
        name = names[0] if names else ""
        garbage, reason = is_garbage_entity(email, name, config)
        if garbage:
            drops[reason] += 1
            continue
        kept[email] = c
    return {**data, "contacts": kept, "clean_drops": dict(drops)}


def clean_gcontacts(data, config):
    """Clean Google Contacts: drop 'other' entries with handle-style names.
    Keep 'saved' entries as-is (trusted curation)."""
    email_lookup = data.get("email_to_name", {})
    phone_lookup = data.get("phone_to_name", {})

    kept_emails = {}
    drops = Counter()
    for email, info in email_lookup.items():
        source = info.get("source", "saved")
        name = info.get("name", "")

        # Always trust "saved" contacts
        if source == "saved":
            # But still drop obviously garbage addresses/names
            g, reason = is_garbage_entity(email, name, config)
            if g and reason in (
                "resource_domain", "bot_token", "facility_code",
                "placeholder_name", "bracket_room_name", "bot_prefix",
            ):
                drops[reason] += 1
                continue
            kept_emails[email] = info
            continue

        # "Other" source: auto-saved by Google from email interactions.
        # These often have handle-style names. Drop if the name is handle-style
        # (we'd rather have no name than a misleading one).
        if not name:
            drops["other_no_name"] += 1
            continue
        # Keep "other" entries with real-looking names; drop handle-style
        if " " not in name or name == name.lower() or any(c.isdigit() for c in name):
            drops["other_handle_name"] += 1
            continue
        g, reason = is_garbage_entity(email, name, config)
        if g:
            drops[reason] += 1
            continue
        kept_emails[email] = info

    # Phone lookup is unfiltered (phones don't have garbage patterns)
    return {
        **data,
        "email_to_name": kept_emails,
        "phone_to_name": phone_lookup,
        "clean_drops": dict(drops),
    }


def clean_krisp(data, config):
    """Clean Krisp: drop participants with no real name or with placeholder names."""
    contacts = data.get("contacts", {})
    kept = {}
    drops = Counter()
    for key, c in contacts.items():
        name = c.get("name", "")
        if not name or len(name) < 2:
            drops["no_name"] += 1
            continue
        # Placeholder check (reuse is_garbage_entity with a fake email)
        if name.lower() in {"unknown", "unlisted", "guest", "speaker"}:
            drops["placeholder_name"] += 1
            continue
        kept[key] = c
    return {**data, "contacts": kept, "clean_drops": dict(drops)}


def clean_workflowy(data, config):
    """Clean Workflowy: drop common category tags that aren't people names."""
    contacts = data.get("contacts", {})
    kept = {}
    drops = Counter()

    # Workflowy hashtag categories that are NOT people names
    category_tags = {
        "post", "partners", "team", "teams", "pricing", "jobsearch",
        "recruiting", "managers", "directs", "interviews", "mentees",
        "supply", "leadershipmeetings", "tvod", "channels", "product",
        "pricing", "marketing", "engineering", "design", "data",
        "feedback", "oneonone", "oneonones", "1on1", "standup",
        "retro", "review", "planning", "all-hands", "allhands",
        "offsite", "kickoff", "demo", "prep",
    }

    for key, c in contacts.items():
        name = c.get("name", "")
        if not name:
            drops["no_name"] += 1
            continue
        if name.lower() in category_tags:
            drops["category_tag"] += 1
            continue
        # Single-character or very short names
        if len(name) < 2:
            drops["too_short"] += 1
            continue
        kept[key] = c
    return {**data, "contacts": kept, "clean_drops": dict(drops)}


def clean_messages(data, config):
    """Clean SMS: drop entries with no name, short-code phones, RCS group IDs."""
    contacts = data.get("contacts", [])
    kept = []
    drops = Counter()
    for c in contacts:
        name = c.get("name", "")
        phone = c.get("phone", "")
        # RCS group chat pseudo-addresses: "d4vtcmrr...@rcs.google.com" in the phone field
        if "@rcs.google.com" in phone.lower() or "@" in phone:
            drops["rcs_group"] += 1
            continue
        # Short-code senders (like 7726 = AT&T spam reports)
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) <= 5:
            drops["short_code"] += 1
            continue
        if not name and not phone:
            drops["no_id"] += 1
            continue
        kept.append(c)
    return {**data, "contacts": kept, "clean_drops": dict(drops)}


def clean_whatsapp(data, config):
    """Clean WhatsApp: already clean from phone exports, pass through."""
    return data


def load_and_clean(name, cleaner):
    """Load mined-{name}.json, apply cleaner, write mined-clean-{name}.json."""
    in_path = CACHE_DIR / f"mined-{name}.json"
    out_path = CACHE_DIR / f"mined-clean-{name}.json"

    if not in_path.exists():
        print(f"  [{name}] mined file not found, skipping", file=sys.stderr)
        return

    with open(in_path, encoding="utf-8") as f:
        data = json.load(f)

    config = load_config()
    cleaned = cleaner(data, config)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)

    # Report
    before = 0
    after = 0
    if name in ("gmail", "gcal", "krisp", "workflowy"):
        before = len(data.get("contacts", {}))
        after = len(cleaned.get("contacts", {}))
    elif name == "contacts":
        before = len(data.get("email_to_name", {}))
        after = len(cleaned.get("email_to_name", {}))
    elif name in ("whatsapp", "messages"):
        before = len(data.get("contacts", []))
        after = len(cleaned.get("contacts", []))

    drops = cleaned.get("clean_drops", {})
    drop_count = before - after
    print(f"  [{name:<10s}] {before:>5d} -> {after:>5d}  ({drop_count:>4d} dropped)  {drops}", file=sys.stderr)


def main():
    print("Cleaning mined sources...", file=sys.stderr)
    load_and_clean("gmail", clean_gmail)
    load_and_clean("gcal", clean_gcal)
    load_and_clean("contacts", clean_gcontacts)
    load_and_clean("krisp", clean_krisp)
    load_and_clean("workflowy", clean_workflowy)
    load_and_clean("whatsapp", clean_whatsapp)
    load_and_clean("messages", clean_messages)
    print("\nDone. Wrote mined-clean-*.json files.", file=sys.stderr)


if __name__ == "__main__":
    main()
