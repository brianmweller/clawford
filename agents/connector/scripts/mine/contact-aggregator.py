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
    with open(path, encoding="utf-8") as f:
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

    # Email alias resolution
    email_aliases = config.get("email_aliases", {})
    def resolve_email(email):
        return email_aliases.get(email, email)

    # Newsletter/mailing list domain filter
    newsletter_domains = config.get("newsletter_domains", [])
    def is_newsletter(email):
        if not email: return False
        return any(email.endswith(f"@{d}") or email.endswith(f".{d}") for d in newsletter_domains)

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
            email = resolve_email(email)
            if is_newsletter(email):
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
            email = resolve_email(email)

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

    # ── Merge Krisp (resolve via Calendar using date + title + name) ──
    # Murphy's pattern: match Krisp meetings to Calendar events by date
    # proximity and title similarity, then match participant names to
    # Calendar attendee names/emails.
    unresolved_krisp = []
    if krisp_data and krisp_data.get("status") == "ok":
        all_contact_names = {c["name"].lower(): email for email, c in contacts.items() if c.get("name")}

        # Build Calendar lookup: date → [(title, {attendee_emails})]
        cal_by_date = defaultdict(list)
        if gcal_data and gcal_data.get("status") == "ok":
            for email, data in gcal_data.get("contacts", {}).items():
                for title in data.get("meeting_titles", []):
                    # We need date→title→emails. Build from raw events if available.
                    pass

            # Better: build from raw gcal contacts — each contact has meeting_titles
            # We need to invert: for each calendar contact, record which dates they attended
            # But we don't have per-meeting dates in the gcal contacts output.
            # Instead: build title→emails lookup AND use attendee name matching
            cal_title_to_emails = defaultdict(set)
            cal_name_to_email = {}
            for email, data in gcal_data.get("contacts", {}).items():
                for title in data.get("meeting_titles", []):
                    cal_title_to_emails[title.lower().strip()].add(email)
                for dname in data.get("display_names", []):
                    cal_name_to_email[dname.lower()] = email

        # Also build from Krisp raw meetings if available
        krisp_meetings = krisp_data.get("meetings", [])
        krisp_resolved_via_cal = 0

        for key, data in krisp_data.get("contacts", {}).items():
            krisp_name = data.get("name", "")
            if not krisp_name:
                continue

            # 1. Try exact name match against existing contacts
            matched_email = all_contact_names.get(krisp_name.lower())

            # 2. Try fuzzy name match against existing contacts
            if not matched_email:
                for contact_name, email in all_contact_names.items():
                    if fuzzy_match(krisp_name, contact_name):
                        matched_email = email
                        break

            # 3. Try Calendar attendee name match (first name or full name)
            if not matched_email and gcal_data:
                for cal_name, cemail in cal_name_to_email.items():
                    if fuzzy_match(krisp_name, cal_name, 0.7):
                        matched_email = cemail
                        krisp_resolved_via_cal += 1
                        break
                    # Also try first-name match (Krisp often uses first names only)
                    cal_first = cal_name.split()[0] if cal_name else ""
                    if cal_first and krisp_name.lower() == cal_first:
                        matched_email = cemail
                        krisp_resolved_via_cal += 1
                        break

            # 4. Try meeting title cross-reference
            if not matched_email and gcal_data:
                for krisp_title in data.get("meeting_titles", []):
                    kt = krisp_title.lower().strip()
                    candidate_emails = set()
                    for cal_title, emails in cal_title_to_emails.items():
                        if fuzzy_match(kt, cal_title, 0.6):
                            candidate_emails.update(emails)

                    for cemail in candidate_emails:
                        cnames = gcal_data.get("contacts", {}).get(cemail, {}).get("display_names", [])
                        for cname in cnames:
                            if fuzzy_match(krisp_name, cname, 0.7) or krisp_name.lower() == cname.split()[0].lower():
                                matched_email = cemail
                                krisp_resolved_via_cal += 1
                                break
                        if matched_email:
                            break
                    if matched_email:
                        break

            if matched_email:
                if matched_email not in contacts:
                    contacts[matched_email] = {
                        "email": matched_email, "name": krisp_name,
                        "display_names": [krisp_name],
                        "gmail_sent": 0, "gmail_received": 0,
                        "gmail_subjects": [], "gmail_body_excerpts": [],
                        "signature": {}, "meeting_count": 0,
                        "krisp_meetings": 0, "whatsapp_messages": 0,
                        "sms_messages": 0, "first_interaction": None,
                        "last_interaction": None, "meeting_titles": [],
                        "platforms": [],
                    }
                contacts[matched_email]["krisp_meetings"] = data.get("meeting_count", 0)
                if "krisp" not in contacts[matched_email]["platforms"]:
                    contacts[matched_email]["platforms"].append("krisp")
            else:
                unresolved_krisp.append(data)

        print(f"Krisp: {len(krisp_data.get('contacts', {}))} participants ({krisp_resolved_via_cal} resolved via Calendar, {len(unresolved_krisp)} unresolved)", file=sys.stderr)

    # ── Merge WhatsApp ───────────────────────────────────────
    name_to_email = config.get("name_to_email", {})
    whatsapp_contacts = []
    if whatsapp_data and whatsapp_data.get("status") == "ok":
        wa_contacts = whatsapp_data.get("contacts", {})
        # Handle both dict (from web scraper) and list (from phone export)
        wa_items = wa_contacts.items() if isinstance(wa_contacts, dict) else enumerate(wa_contacts)
        for key, data in wa_items:
            name = data.get("name", "")
            if not name:
                continue

            # Try explicit name→email mapping first
            matched = False
            mapped_email = name_to_email.get(name.lower())
            if mapped_email and mapped_email in contacts:
                contacts[mapped_email]["whatsapp_messages"] += data.get("message_count", 0)
                if "whatsapp" not in contacts[mapped_email]["platforms"]:
                    contacts[mapped_email]["platforms"].append("whatsapp")
                matched = True

            # Try to match by name to existing email-based contacts
            if not matched:
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

    # ── Merge email aliases (combine split contacts) ────────
    merged_aliases = 0
    for alias_email, canonical_email in email_aliases.items():
        alias_email = alias_email.lower()
        canonical_email = canonical_email.lower()
        if alias_email in contacts and canonical_email in contacts and alias_email != canonical_email:
            # Merge alias into canonical
            alias = contacts[alias_email]
            canon = contacts[canonical_email]
            canon["gmail_sent"] += alias.get("gmail_sent", 0)
            canon["gmail_received"] += alias.get("gmail_received", 0)
            canon["gmail_subjects"].extend(alias.get("gmail_subjects", []))
            canon["gmail_body_excerpts"].extend(alias.get("gmail_body_excerpts", []))
            canon["meeting_count"] += alias.get("meeting_count", 0)
            canon["krisp_meetings"] += alias.get("krisp_meetings", 0)
            canon["whatsapp_messages"] += alias.get("whatsapp_messages", 0)
            canon["sms_messages"] += alias.get("sms_messages", 0)
            canon["meeting_titles"].extend(alias.get("meeting_titles", []))
            for n in alias.get("display_names", []):
                if n not in canon["display_names"]:
                    canon["display_names"].append(n)
            for p in alias.get("platforms", []):
                if p not in canon["platforms"]:
                    canon["platforms"].append(p)
            if not canon.get("signature") and alias.get("signature"):
                canon["signature"] = alias["signature"]
            # Update interaction dates
            af = alias.get("first_interaction")
            if af and (not canon["first_interaction"] or af < canon["first_interaction"]):
                canon["first_interaction"] = af
            al = alias.get("last_interaction")
            if al and (not canon["last_interaction"] or al > canon["last_interaction"]):
                canon["last_interaction"] = al
            del contacts[alias_email]
            merged_aliases += 1
        elif alias_email in contacts and canonical_email not in contacts:
            # Rename alias to canonical
            contacts[canonical_email] = contacts.pop(alias_email)
            contacts[canonical_email]["email"] = canonical_email
            merged_aliases += 1

    if merged_aliases:
        print(f"Email aliases merged: {merged_aliases}", file=sys.stderr)

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
    mailing_list_cfg = config.get("mailing_list_heuristic", {})
    ml_min_recv = mailing_list_cfg.get("min_received", 10)
    ml_max_sent = mailing_list_cfg.get("max_sent", 0)

    for email, c in contacts.items():
        # Skip mailing lists: high received, zero/low sent, no meetings, no SMS/WA
        if (c.get("gmail_received", 0) >= ml_min_recv
            and c.get("gmail_sent", 0) <= ml_max_sent
            and c.get("meeting_count", 0) == 0
            and c.get("whatsapp_messages", 0) == 0
            and c.get("sms_messages", 0) == 0):
            continue

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
