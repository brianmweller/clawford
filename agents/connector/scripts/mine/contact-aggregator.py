#!/usr/bin/env python3
"""
contact-aggregator.py — Merge contacts from all 6 mining sources.

Deduplicates by email (primary) and name (secondary for Krisp/WhatsApp).
Computes importance scores. Auto-assigns circles. Checks for existing
people files to avoid duplicates.

Usage:
  python3 contact-aggregator.py

Input: cache/mined-*.json files
Output: cache/aggregated-contacts.json
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import (
    CACHE_DIR,
    email_to_slug,
    is_brian,
    is_noreply,
    load_config,
    name_to_slug,
    normalize_email,
    save_mined,
)

BRAIN_PEOPLE = os.path.expanduser("~/Dropbox/openclaw-backup/people")


def load_mined(name):
    """Load a mined-*.json file from cache."""
    path = CACHE_DIR / f"mined-{name}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def existing_people_slugs():
    """Get set of existing people file slugs."""
    slugs = set()
    if os.path.exists(BRAIN_PEOPLE):
        for f in os.listdir(BRAIN_PEOPLE):
            if f.endswith(".md") and f != "_template.md":
                slugs.add(f.replace(".md", ""))
    return slugs


def best_name(display_names):
    """Pick the best display name from a list. Prefer longest non-email name."""
    if not display_names:
        return ""
    # Filter out email-like names
    real_names = [n for n in display_names if "@" not in n and len(n) > 1]
    if not real_names:
        return display_names[0]
    # Longest name (most complete)
    return max(real_names, key=len)


def fuzzy_match(name1, name2, threshold=0.8):
    """Check if two names are similar enough to merge."""
    if not name1 or not name2:
        return False
    return SequenceMatcher(None, name1.lower(), name2.lower()).ratio() >= threshold


def compute_score(contact, config):
    """Compute importance score for a contact."""
    rules = config.get("circle_rules", {})
    now = datetime.now(timezone.utc).date()

    gmail_sent = contact.get("gmail_sent", 0)
    gmail_received = contact.get("gmail_received", 0)
    meetings = contact.get("meeting_count", 0)
    krisp = contact.get("krisp_meetings", 0)
    whatsapp = contact.get("whatsapp_messages", 0)
    sms = contact.get("sms_messages", 0)

    score = (
        gmail_sent * 3.0
        + gmail_received * 1.0
        + meetings * 5.0
        + krisp * 4.0
        + whatsapp * 2.0
        + sms * 2.0
    )

    # Recency bonus
    last = contact.get("last_interaction")
    if last:
        try:
            last_date = datetime.strptime(last, "%Y-%m-%d").date()
            days_ago = (now - last_date).days
            if days_ago <= 30:
                score += 10
            elif days_ago <= 90:
                score += 5
        except ValueError:
            pass

    return round(score, 1)


def auto_circle(contact, config):
    """Auto-assign a circle based on interaction patterns."""
    rules = config.get("circle_rules", {})
    now = datetime.now(timezone.utc).date()

    meetings = contact.get("meeting_count", 0)
    gmail_sent = contact.get("gmail_sent", 0)
    whatsapp = contact.get("whatsapp_messages", 0)
    email = contact.get("email", "")
    last = contact.get("last_interaction")

    # Check recency
    recent_30d = False
    if last:
        try:
            last_date = datetime.strptime(last, "%Y-%m-%d").date()
            recent_30d = (now - last_date).days <= rules.get("high_meeting_recency_days", 30)
        except ValueError:
            pass

    personal_domains = config.get("personal_domains", [])
    is_personal = any(email.endswith(f"@{d}") for d in personal_domains)

    # Professional-inner: frequent meetings + recent
    if meetings >= rules.get("high_meeting_threshold", 5) and recent_30d:
        return "professional-inner"

    # Friends-close: high personal email + WhatsApp
    if is_personal and (gmail_sent >= rules.get("high_email_sent_threshold", 10) or whatsapp >= rules.get("high_whatsapp_threshold", 5)):
        return "friends-close"

    # Professional-outer: had meetings
    if meetings >= 1:
        return "professional-outer"

    # Default based on domain
    if is_personal:
        return "friends-close"
    return "professional-outer"


def aggregate():
    config = load_config()
    min_score = config.get("min_importance_score", 5.0)

    # Load all mined sources
    gmail_data = load_mined("gmail")
    gcal_data = load_mined("gcal")
    krisp_data = load_mined("krisp")
    workflowy_data = load_mined("workflowy")
    whatsapp_data = load_mined("whatsapp")
    messages_data = load_mined("messages")
    gcontacts_data = load_mined("contacts")

    # Build Google Contacts lookup tables
    gc_email_to_name = {}
    gc_phone_to_name = {}
    if gcontacts_data and gcontacts_data.get("status") == "ok":
        gc_email_to_name = gcontacts_data.get("email_to_name", {})
        gc_phone_to_name = gcontacts_data.get("phone_to_name", {})
        print(f"Google Contacts: {len(gc_email_to_name)} email lookups, {len(gc_phone_to_name)} phone lookups", file=sys.stderr)

    # Master contact list keyed by email
    contacts = {}

    # ── Merge Gmail ──────────────────────────────────────────
    if gmail_data and gmail_data.get("status") == "ok":
        for email, data in gmail_data.get("contacts", {}).items():
            email = normalize_email(email)
            if not email or is_brian(email, config) or is_noreply(email, config):
                continue
            contacts[email] = {
                "email": email,
                "name": best_name(data.get("display_names", [])),
                "display_names": data.get("display_names", []),
                "gmail_sent": data.get("sent_count", 0),
                "gmail_received": data.get("received_count", 0),
                "gmail_subjects": data.get("subjects", []),
                "gmail_body_excerpts": data.get("body_excerpts", []),
                "signature": data.get("signature", {}),
                "meeting_count": 0,
                "krisp_meetings": 0,
                "whatsapp_messages": 0,
                "sms_messages": 0,
                "first_interaction": data.get("first_seen"),
                "last_interaction": data.get("last_seen"),
                "meeting_titles": [],
                "platforms": ["email"],
            }
        print(f"Gmail: {len(gmail_data.get('contacts', {}))} contacts", file=sys.stderr)

    # ── Merge Calendar ───────────────────────────────────────
    if gcal_data and gcal_data.get("status") == "ok":
        for email, data in gcal_data.get("contacts", {}).items():
            email = normalize_email(email)
            if not email or is_brian(email, config):
                continue

            if email in contacts:
                c = contacts[email]
                c["meeting_count"] = data.get("meeting_count", 0)
                c["meeting_titles"] = data.get("meeting_titles", [])
                if "calendar" not in c["platforms"]:
                    c["platforms"].append("calendar")
                # Merge display names
                for n in data.get("display_names", []):
                    if n not in c["display_names"]:
                        c["display_names"].append(n)
                # Update name if better
                if not c["name"] or len(best_name(data.get("display_names", []))) > len(c["name"]):
                    c["name"] = best_name(data.get("display_names", []))
                # Update interaction dates
                fm = data.get("first_meeting")
                if fm and (not c["first_interaction"] or fm < c["first_interaction"]):
                    c["first_interaction"] = fm
                lm = data.get("last_meeting")
                if lm and (not c["last_interaction"] or lm > c["last_interaction"]):
                    c["last_interaction"] = lm
            else:
                contacts[email] = {
                    "email": email,
                    "name": best_name(data.get("display_names", [])),
                    "display_names": data.get("display_names", []),
                    "gmail_sent": 0,
                    "gmail_received": 0,
                    "gmail_subjects": [],
                    "gmail_body_excerpts": [],
                    "signature": {},
                    "meeting_count": data.get("meeting_count", 0),
                    "krisp_meetings": 0,
                    "whatsapp_messages": 0,
                    "sms_messages": 0,
                    "first_interaction": data.get("first_meeting"),
                    "last_interaction": data.get("last_meeting"),
                    "meeting_titles": data.get("meeting_titles", []),
                    "platforms": ["calendar"],
                }
        print(f"Calendar: {len(gcal_data.get('contacts', {}))} contacts", file=sys.stderr)

    # ── Merge Workflowy (name resolution only) ───────────────
    if workflowy_data and workflowy_data.get("status") == "ok":
        for email, data in workflowy_data.get("contacts", {}).items():
            email = normalize_email(email)
            if email in contacts:
                names = data.get("display_names", [])
                if names:
                    # Workflowy resolution is highest confidence
                    contacts[email]["name"] = names[0]
                    for n in names:
                        if n not in contacts[email]["display_names"]:
                            contacts[email]["display_names"].append(n)
        print(f"Workflowy: {len(workflowy_data.get('contacts', {}))} name resolutions", file=sys.stderr)

    # ── Merge Krisp (name-only, fuzzy match) ─────────────────
    unresolved_krisp = []
    if krisp_data and krisp_data.get("status") == "ok":
        all_contact_names = {c["name"].lower(): email for email, c in contacts.items() if c.get("name")}

        for key, data in krisp_data.get("contacts", {}).items():
            krisp_name = data.get("name", "")
            if not krisp_name:
                continue

            # Try exact match
            matched_email = all_contact_names.get(krisp_name.lower())

            # Try fuzzy match
            if not matched_email:
                for contact_name, email in all_contact_names.items():
                    if fuzzy_match(krisp_name, contact_name):
                        matched_email = email
                        break

            if matched_email:
                contacts[matched_email]["krisp_meetings"] = data.get("meeting_count", 0)
                if "krisp" not in contacts[matched_email]["platforms"]:
                    contacts[matched_email]["platforms"].append("krisp")
            else:
                unresolved_krisp.append(data)

        print(f"Krisp: {len(krisp_data.get('contacts', {}))} participants ({len(unresolved_krisp)} unresolved)", file=sys.stderr)

    # ── Merge WhatsApp ───────────────────────────────────────
    whatsapp_contacts = []
    if whatsapp_data and whatsapp_data.get("status") == "ok":
        for key, data in whatsapp_data.get("contacts", {}).items():
            name = data.get("name", "")
            if not name:
                continue

            # Try to match by name to existing email-based contacts
            matched = False
            for email, c in contacts.items():
                if fuzzy_match(name, c.get("name", "")):
                    c["whatsapp_messages"] = data.get("message_count", 0)
                    if "whatsapp" not in c["platforms"]:
                        c["platforms"].append("whatsapp")
                    matched = True
                    break

            # Try Google Contacts phone lookup
            if not matched and gc_phone_to_name:
                for phone, gc in gc_phone_to_name.items():
                    if fuzzy_match(name, gc.get("name", "")):
                        # Found in Google Contacts — create or merge
                        gc_email = normalize_email(gc.get("email", ""))
                        if gc_email and gc_email in contacts:
                            contacts[gc_email]["whatsapp_messages"] = data.get("message_count", 0)
                            if "whatsapp" not in contacts[gc_email]["platforms"]:
                                contacts[gc_email]["platforms"].append("whatsapp")
                        elif gc_email:
                            contacts[gc_email] = {
                                "email": gc_email, "name": gc["name"],
                                "display_names": [gc["name"]], "phone": phone,
                                "gmail_sent": 0, "gmail_received": 0,
                                "gmail_subjects": [], "gmail_body_excerpts": [],
                                "signature": {}, "meeting_count": 0,
                                "krisp_meetings": 0,
                                "whatsapp_messages": data.get("message_count", 0),
                                "sms_messages": 0,
                                "first_interaction": data.get("first_message"),
                                "last_interaction": data.get("last_message"),
                                "meeting_titles": [],
                                "platforms": ["whatsapp"],
                            }
                        matched = True
                        break

            if not matched:
                whatsapp_contacts.append({**data, "platform": "whatsapp"})

        print(f"WhatsApp: {len(whatsapp_data.get('contacts', {}))} contacts", file=sys.stderr)

    # ── Merge Google Messages ────────────────────────────────
    messages_contacts = []
    if messages_data and messages_data.get("status") == "ok":
        for data in messages_data.get("contacts", []):
            name = data.get("name", "")
            if not name:
                continue

            matched = False
            for email, c in contacts.items():
                if fuzzy_match(name, c.get("name", "")):
                    c["sms_messages"] = data.get("message_count", 0)
                    if "sms" not in c["platforms"]:
                        c["platforms"].append("sms")
                    matched = True
                    break

            # Try Google Contacts phone lookup
            if not matched and gc_phone_to_name:
                for phone, gc in gc_phone_to_name.items():
                    if fuzzy_match(name, gc.get("name", "")):
                        gc_email = normalize_email(gc.get("email", ""))
                        if gc_email and gc_email in contacts:
                            contacts[gc_email]["sms_messages"] = data.get("message_count", 0)
                            if "sms" not in contacts[gc_email]["platforms"]:
                                contacts[gc_email]["platforms"].append("sms")
                        elif gc_email:
                            contacts[gc_email] = {
                                "email": gc_email, "name": gc["name"],
                                "display_names": [gc["name"]], "phone": phone,
                                "gmail_sent": 0, "gmail_received": 0,
                                "gmail_subjects": [], "gmail_body_excerpts": [],
                                "signature": {}, "meeting_count": 0,
                                "krisp_meetings": 0, "whatsapp_messages": 0,
                                "sms_messages": data.get("message_count", 0),
                                "first_interaction": data.get("first_message"),
                                "last_interaction": data.get("last_message"),
                                "meeting_titles": [],
                                "platforms": ["sms"],
                            }
                        matched = True
                        break

            if not matched:
                messages_contacts.append({**data, "platform": "sms"})

        print(f"Messages: {len(messages_data.get('contacts', []))} contacts", file=sys.stderr)

    # ── Resolve names via Google Contacts ────────────────────
    resolved_names = 0
    resolved_phones = 0
    for email, c in contacts.items():
        gc = gc_email_to_name.get(email)
        if gc:
            # Fill in blank or email-like names
            if not c["name"] or "@" in c["name"]:
                c["name"] = gc["name"]
                resolved_names += 1
            elif gc["name"] not in c["display_names"]:
                c["display_names"].append(gc["name"])
            # Add phone if we don't have one
            if gc.get("phone") and not c.get("phone"):
                c["phone"] = gc["phone"]
                resolved_phones += 1

    if gc_email_to_name:
        print(f"Google Contacts resolved: {resolved_names} names, {resolved_phones} phones", file=sys.stderr)

    # ── Score and classify ───────────────────────────────────
    existing_slugs = existing_people_slugs()

    scored_contacts = []
    for email, c in contacts.items():
        score = compute_score(c, config)
        if score < min_score:
            continue

        slug = name_to_slug(c["name"]) if c["name"] else email_to_slug(email)
        exists = slug in existing_slugs or email_to_slug(email) in existing_slugs

        # Ensure phone is included
        if not c.get("phone"):
            gc = gc_email_to_name.get(email, {})
            if gc.get("phone"):
                c["phone"] = gc["phone"]

        scored_contacts.append({
            **c,
            "score": score,
            "auto_circle": auto_circle(c, config),
            "auto_circle_auto": True,
            "slug": slug,
            "exists_in_brain": exists,
        })

    # Sort by score descending
    scored_contacts.sort(key=lambda c: -c["score"])

    result = {
        "status": "ok",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "total_contacts": len(scored_contacts),
        "contacts": scored_contacts,
        "unresolved_krisp": unresolved_krisp,
        "unmatched_whatsapp": whatsapp_contacts,
        "unmatched_messages": messages_contacts,
        "existing_people_count": len(existing_slugs),
    }

    save_mined("aggregated", result)
    # Also save as aggregated-contacts.json for downstream scripts
    output_path = CACHE_DIR / "aggregated-contacts.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nDone. {len(scored_contacts)} contacts above score threshold ({min_score}).", file=sys.stderr)
    print(f"  Unresolved Krisp: {len(unresolved_krisp)}", file=sys.stderr)
    print(f"  Unmatched WhatsApp: {len(whatsapp_contacts)}", file=sys.stderr)
    print(f"  Unmatched Messages: {len(messages_contacts)}", file=sys.stderr)


if __name__ == "__main__":
    aggregate()
