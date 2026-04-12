#!/usr/bin/env python3
"""
contact-aggregator.py — Merge cleaned sources into canonical contacts.

Inputs:  cache/mined-clean-*.json (pre-filtered by clean-sources.py)
Output:  cache/contacts-merged.json (canonical entities, scored, no LLM)

10-step flow:
  1. Load all cleaned sources
  2. Build contacts dict keyed by normalized email
  3. Merge source data (counts, signatures, dates, display_names)
  4. Normalize all names (Last,First -> First Last, handle.style -> First Last)
  5. Resolve names via saved Google Contacts (override handles)
  6. Detect aliases: manual config + GC same-name + fuzzy lastname+nickname
  7. Merge aliases (sum counts, union display_names, max dates)
  8. Apply is_person() gate
  9. Compute score from merged counts
 10. Write contacts-merged.json

Filter logic lives in mining_utils.is_person(). The aggregator is pure
data manipulation.
"""

import io
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import (
    CACHE_DIR,
    email_to_slug,
    is_person,
    load_config,
    name_to_slug,
    normalize_email,
    normalize_name,
    looks_like_real_name,
    looks_like_username,
    parse_date_any,
)

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ── Nickname map for fuzzy alias detection ─────────────────

NICKNAME_MAP = {
    "allie": {"alyssa", "alison"},
    "alyssa": {"allie"},
    "alison": {"allie"},
    "alex": {"alexander", "alexandra"},
    "alexander": {"alex"},
    "alexandra": {"alex"},
    "dan": {"daniel", "Chris"},
    "daniel": {"dan", "Chris"},
    "Chris": {"daniel", "dan"},
    "mike": {"michael"},
    "michael": {"mike"},
    "bob": {"robert", "rob"},
    "robert": {"bob", "rob"},
    "rob": {"robert"},
    "will": {"william", "bill"},
    "william": {"will", "bill"},
    "bill": {"william"},
    "kate": {"katherine", "catherine", "katie"},
    "katie": {"katherine", "catherine", "kate"},
    "katherine": {"kate", "katie"},
    "catherine": {"kate", "katie"},
    "liz": {"elizabeth", "beth"},
    "beth": {"elizabeth"},
    "elizabeth": {"liz", "beth"},
    "chris": {"christopher", "christine"},
    "christopher": {"chris"},
    "christine": {"chris"},
    "jen": {"jennifer"},
    "jennifer": {"jen"},
    "jon": {"jonathan"},
    "jonathan": {"jon"},
    "matt": {"matthew"},
    "matthew": {"matt"},
    "nick": {"nicholas"},
    "nicholas": {"nick"},
    "sam": {"samuel", "samantha"},
    "samuel": {"sam"},
    "samantha": {"sam"},
    "tom": {"thomas"},
    "thomas": {"tom", "tommy"},
    "tommy": {"thomas"},
    "steve": {"steven", "stephen"},
    "steven": {"steve"},
    "stephen": {"steve"},
}


def first_names_match(n1, n2):
    if not n1 or not n2:
        return False
    n1, n2 = n1.lower(), n2.lower()
    if n1 == n2:
        return True
    return n2 in NICKNAME_MAP.get(n1, set()) or n1 in NICKNAME_MAP.get(n2, set())


def fuzzy_str_match(a, b, threshold=0.85):
    if not a or not b:
        return False
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold


def normalize_phone(phone):
    """Normalize phone to digits only, strip leading US country code."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    # Strip US +1 country code (11 digits starting with 1)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


# ── I/O helpers ────────────────────────────────────────────

def load_clean(name):
    path = CACHE_DIR / f"mined-clean-{name}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def best_name(display_names):
    """Pick the best name: real names first, then capitalized, then longest."""
    if not display_names:
        return ""
    candidates = [n for n in display_names if n and "@" not in n]
    if not candidates:
        return display_names[0] if display_names else ""

    # Tier 1: real names (spaces + capitalized, no digits)
    real = [n for n in candidates if looks_like_real_name(n)]
    if real:
        return max(real, key=len)

    # Tier 2: capitalized without digits
    cap = [n for n in candidates if n and n[0].isupper() and not any(c.isdigit() for c in n)]
    if cap:
        return max(cap, key=len)

    # Tier 3: longest
    return max(candidates, key=len)


def blank_contact(email):
    return {
        "email": email,
        "name": "",
        "display_names": [],
        "phone": "",
        "gmail_sent": 0,
        "gmail_received": 0,
        "gmail_subjects": [],
        "gmail_body_excerpts": [],
        "signature": {},
        "meeting_count": 0,
        "krisp_meetings": 0,
        "whatsapp_messages": 0,
        "sms_messages": 0,
        "whatsapp_samples": [],
        "sms_samples": [],
        "first_interaction": None,
        "last_interaction": None,
        "meeting_titles": [],
        "platforms": [],
    }


def update_dates(contact, first, last):
    first_iso = parse_date_any(first)
    last_iso = parse_date_any(last)
    if first_iso:
        cur = contact.get("first_interaction")
        if not cur or first_iso < cur:
            contact["first_interaction"] = first_iso
    if last_iso:
        cur = contact.get("last_interaction")
        if not cur or last_iso > cur:
            contact["last_interaction"] = last_iso


def add_display_name(contact, name):
    if name and name not in contact["display_names"]:
        contact["display_names"].append(name)


# ── Score ──────────────────────────────────────────────────

def compute_score(c, config):
    """Raw signal score with exponential recency decay.

    Base = volume-weighted sum of interactions. Recency decay halves the
    score every 2 years since last contact, so a friend Sam talked to
    yesterday outranks an old vendor with 10x the message volume.
    """
    now = datetime.now(timezone.utc).date()
    gmail_sent = c.get("gmail_sent", 0) or 0
    gmail_recv = c.get("gmail_received", 0) or 0
    meetings = c.get("meeting_count", 0) or 0
    krisp = c.get("krisp_meetings", 0) or 0
    wa = c.get("whatsapp_messages", 0) or 0
    sms = c.get("sms_messages", 0) or 0

    base = (
        gmail_sent * 3.0
        + gmail_recv * 1.0
        + meetings * 5.0
        + krisp * 4.0
        + wa * 2.0
        + sms * 2.0
    )

    # Recency factor: 1.0 for recent, halves every 2 years (730 days)
    recency = 1.0
    last = c.get("last_interaction")
    if last:
        try:
            last_date = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
            days_ago = max((now - last_date).days, 0)
            if days_ago <= 30:
                recency = 1.2  # fresh boost
            elif days_ago <= 90:
                recency = 1.1
            else:
                # Exponential half-life at 2 years
                recency = 0.5 ** (days_ago / 730.0)
        except ValueError:
            pass

    return round(base * recency, 1)


def auto_circle(c, config):
    rules = config.get("circle_rules", {})
    now = datetime.now(timezone.utc).date()
    meetings = c.get("meeting_count", 0) or 0
    gmail_sent = c.get("gmail_sent", 0) or 0
    wa = c.get("whatsapp_messages", 0) or 0
    email = c.get("email", "")
    last = c.get("last_interaction")

    recent_30d = False
    if last:
        try:
            last_date = datetime.strptime(last[:10], "%Y-%m-%d").date()
            recent_30d = (now - last_date).days <= rules.get("high_meeting_recency_days", 30)
        except ValueError:
            pass

    personal_domains = config.get("personal_domains", [])
    is_personal = any(email.endswith(f"@{d}") for d in personal_domains)

    if meetings >= rules.get("high_meeting_threshold", 5) and recent_30d:
        return "professional-inner"
    if is_personal and (gmail_sent >= rules.get("high_email_sent_threshold", 10) or wa >= rules.get("high_whatsapp_threshold", 5)):
        return "friends-close"
    if meetings >= 1:
        return "professional-outer"
    if is_personal:
        return "friends-close"
    return "professional-outer"


# ── Main ──────────────────────────────────────────────────

def aggregate():
    config = load_config()
    email_aliases_manual = config.get("email_aliases", {})
    name_to_email = config.get("name_to_email", {})

    # ── Step 1: Load cleaned sources ───────────────────────
    gmail_data = load_clean("gmail")
    gcal_data = load_clean("gcal")
    gcontacts_data = load_clean("contacts")
    krisp_data = load_clean("krisp")
    workflowy_data = load_clean("workflowy")
    whatsapp_data = load_clean("whatsapp")
    messages_data = load_clean("messages")

    # Build Google Contacts lookup (only saved entries for authoritative names)
    gc_email_to_name = {}
    saved_gc_emails = set()
    gc_phone_to_name = {}
    # Cross-reference: normalized phone digits -> email (built from BOTH directions)
    gc_phone_to_email = {}
    # Cross-reference: normalized phone digits -> canonical name (for SMS-only entries)
    gc_phone_to_cname = {}
    if gcontacts_data:
        for email, info in gcontacts_data.get("email_to_name", {}).items():
            gc_email_to_name[email] = info
            if info.get("source") == "saved":
                saved_gc_emails.add(email)
            # Build phone->email from email_to_name entries that have a phone
            ph = normalize_phone(info.get("phone", ""))
            if ph and email not in gc_phone_to_email.values():
                gc_phone_to_email.setdefault(ph, email)
                gc_phone_to_cname.setdefault(ph, info.get("name", ""))
        gc_phone_to_name = gcontacts_data.get("phone_to_name", {})
        for phone_raw, info in gc_phone_to_name.items():
            ph = normalize_phone(phone_raw)
            if not ph:
                continue
            # If phone_to_name entry has an email, use it
            e = normalize_email(info.get("email", ""))
            if e:
                gc_phone_to_email.setdefault(ph, e)
            gc_phone_to_cname.setdefault(ph, info.get("name", ""))
        print(f"Google Contacts: {len(gc_email_to_name)} emails ({len(saved_gc_emails)} saved), {len(gc_phone_to_name)} phones, {len(gc_phone_to_email)} phone->email xrefs", file=sys.stderr)

    # ── Step 2-3: Build contacts by merging sources ────────
    contacts = {}

    # Gmail
    if gmail_data and gmail_data.get("status") == "ok":
        for email, data in gmail_data.get("contacts", {}).items():
            email = normalize_email(email)
            if not email:
                continue
            c = contacts.setdefault(email, blank_contact(email))
            c["gmail_sent"] += data.get("sent_count", 0)
            c["gmail_received"] += data.get("received_count", 0)
            c["gmail_subjects"].extend(data.get("subjects", []))
            c["gmail_body_excerpts"].extend(data.get("body_excerpts", []))
            if data.get("signature") and not c["signature"]:
                c["signature"] = data["signature"]
            for dn in data.get("display_names", []):
                add_display_name(c, dn)
            update_dates(c, data.get("first_seen"), data.get("last_seen"))
            if "email" not in c["platforms"]:
                c["platforms"].append("email")
        print(f"Gmail: merged {len(gmail_data.get('contacts', {}))} contacts", file=sys.stderr)

    # Calendar
    if gcal_data and gcal_data.get("status") == "ok":
        for email, data in gcal_data.get("contacts", {}).items():
            email = normalize_email(email)
            if not email:
                continue
            c = contacts.setdefault(email, blank_contact(email))
            c["meeting_count"] += data.get("meeting_count", 0)
            c["meeting_titles"].extend(data.get("meeting_titles", []))
            for dn in data.get("display_names", []):
                add_display_name(c, dn)
            update_dates(c, data.get("first_meeting"), data.get("last_meeting"))
            if "calendar" not in c["platforms"]:
                c["platforms"].append("calendar")
        print(f"Calendar: merged {len(gcal_data.get('contacts', {}))} contacts", file=sys.stderr)

    # Workflowy (hashtag name resolution, not primary contact source)
    workflowy_count = 0
    if workflowy_data and workflowy_data.get("status") == "ok":
        workflowy_count = len(workflowy_data.get("contacts", {}))
        print(f"Workflowy: {workflowy_count} hashtag names (used for display_names enrichment)", file=sys.stderr)

    # Krisp (name-only, cross-reference with Calendar later)
    unresolved_krisp = []
    krisp_by_name = {}
    if krisp_data and krisp_data.get("status") == "ok":
        for key, data in krisp_data.get("contacts", {}).items():
            krisp_by_name[data.get("name", "").lower()] = data
        print(f"Krisp: {len(krisp_by_name)} participant names", file=sys.stderr)

    # Match Krisp names to existing email-based contacts
    cal_name_to_email = {}
    if gcal_data:
        for email, data in gcal_data.get("contacts", {}).items():
            for dn in data.get("display_names", []):
                cal_name_to_email[dn.lower()] = normalize_email(email)

    for krisp_name_lower, data in krisp_by_name.items():
        krisp_name = data.get("name", "")
        matched_email = None

        # Exact name match against existing contacts
        for email, c in contacts.items():
            if c.get("name", "").lower() == krisp_name_lower:
                matched_email = email
                break
            for dn in c.get("display_names", []):
                if dn.lower() == krisp_name_lower:
                    matched_email = email
                    break
            if matched_email:
                break

        # Fallback: match via Calendar display names or first-name
        if not matched_email:
            for cal_name, cemail in cal_name_to_email.items():
                if fuzzy_str_match(krisp_name, cal_name, 0.85):
                    matched_email = cemail
                    break
                # First-name match
                cal_first = cal_name.split()[0] if cal_name else ""
                if cal_first and krisp_name.lower() == cal_first.lower():
                    matched_email = cemail
                    break

        if matched_email and matched_email in contacts:
            c = contacts[matched_email]
            c["krisp_meetings"] += data.get("meeting_count", 0)
            if "krisp" not in c["platforms"]:
                c["platforms"].append("krisp")
        else:
            unresolved_krisp.append(data)

    # WhatsApp (phone export — list format)
    if whatsapp_data and whatsapp_data.get("status") == "ok":
        # Name→email lookup for WA merge. Prefer saved GC + personal domain.
        _WA_PERSONAL_DOMS = ("gmail.com", "icloud.com", "yahoo.com", "hotmail.com", "outlook.com", "me.com")

        def _wa_rank(e, info):
            src_rank = 0 if info.get("source") == "saved" else 1
            domain = e.split("@")[-1].lower() if "@" in e else ""
            dom_rank = 0 if domain in _WA_PERSONAL_DOMS else 1
            return (src_rank, dom_rank, e)

        wa_gc_name_to_email = {}
        _wa_candidates = {}
        for ge, gi in gc_email_to_name.items():
            nm = (gi.get("name") or "").strip().lower()
            if nm:
                _wa_candidates.setdefault(nm, []).append((ge, gi))
        for nm, candidates in _wa_candidates.items():
            candidates.sort(key=lambda x: _wa_rank(x[0], x[1]))
            wa_gc_name_to_email[nm] = candidates[0][0]

        def _wa_ensure_contact(email):
            email = normalize_email(email)
            if not email:
                return None
            if email not in contacts:
                contacts[email] = blank_contact(email)
                gc = gc_email_to_name.get(email)
                if gc and gc.get("name"):
                    add_display_name(contacts[email], gc["name"])
            return email

        for data in whatsapp_data.get("contacts", []):
            name = data.get("name", "")
            if not name:
                continue

            matched_email = None

            # 1. Manual name_to_email config
            mapped = name_to_email.get(name.lower())
            if mapped:
                matched_email = _wa_ensure_contact(mapped)

            # 2. GC name lookup — resolves to real email
            if not matched_email:
                ge = wa_gc_name_to_email.get(name.lower())
                if ge:
                    matched_email = _wa_ensure_contact(ge)

            # 3. Fuzzy-match existing contacts
            if not matched_email:
                for email, c in contacts.items():
                    if fuzzy_str_match(name, c.get("name", ""), 0.85):
                        matched_email = email
                        break

            if matched_email and matched_email in contacts:
                c = contacts[matched_email]
                c["whatsapp_messages"] += data.get("message_count", 0)
                if "whatsapp" not in c["platforms"]:
                    c["platforms"].append("whatsapp")
                c["whatsapp_samples"].extend(data.get("messages", [])[-50:])
                update_dates(c, data.get("first_message"), data.get("last_message"))
                add_display_name(c, name)
                continue

            # Orphan: create a contact with a synthetic email
            synth_email = f"whatsapp:{name_to_slug(name)}"
            c = contacts.setdefault(synth_email, blank_contact(synth_email))
            c["name"] = name
            add_display_name(c, name)
            c["whatsapp_messages"] += data.get("message_count", 0)
            c["whatsapp_samples"].extend(data.get("messages", [])[-50:])
            if "whatsapp" not in c["platforms"]:
                c["platforms"].append("whatsapp")
            update_dates(c, data.get("first_message"), data.get("last_message"))
        print(f"WhatsApp: {len(whatsapp_data.get('contacts', []))} chats merged", file=sys.stderr)

    # Messages (SMS/MMS from phone export — list format, keyed by phone)
    if messages_data and messages_data.get("status") == "ok":
        # Build GC name → email lookup for name-based resolution
        gc_name_to_email_lookup = {}
        _PERSONAL_DOMS = ("gmail.com", "icloud.com", "yahoo.com", "hotmail.com", "outlook.com", "me.com")

        def _email_rank(e, info):
            # Lower is better. Prefer saved > other; personal > other domains.
            src_rank = 0 if info.get("source") == "saved" else 1
            domain = e.split("@")[-1].lower() if "@" in e else ""
            dom_rank = 0 if domain in _PERSONAL_DOMS else 1
            return (src_rank, dom_rank, e)

        # Group by name, pick best email per name
        _name_candidates = {}
        for ge, gi in gc_email_to_name.items():
            nm = (gi.get("name") or "").strip().lower()
            if nm:
                _name_candidates.setdefault(nm, []).append((ge, gi))
        for nm, candidates in _name_candidates.items():
            candidates.sort(key=lambda x: _email_rank(x[0], x[1]))
            gc_name_to_email_lookup[nm] = candidates[0][0]

        def _ensure_contact(email):
            """Ensure an email has a contact entry, creating blank if missing."""
            email = normalize_email(email)
            if not email:
                return None
            if email not in contacts:
                contacts[email] = blank_contact(email)
                # Seed name from GC if available
                gc = gc_email_to_name.get(email)
                if gc and gc.get("name"):
                    add_display_name(contacts[email], gc["name"])
            return email

        for data in messages_data.get("contacts", []):
            name = data.get("name", "")
            phone = data.get("phone", "")
            phone_norm = normalize_phone(phone)

            # 1. Manual name_to_email config (e.g. "spouse-nick" -> alex.rivera@example.com)
            matched_email = None
            if name:
                mapped = name_to_email.get(name.lower())
                if mapped:
                    matched_email = _ensure_contact(mapped)

            # 2. Phone xref → GC email (creates contact if missing)
            if not matched_email and phone_norm and phone_norm in gc_phone_to_email:
                matched_email = _ensure_contact(gc_phone_to_email[phone_norm])

            # 3. GC resolved canonical name → GC email (creates contact if missing)
            if not matched_email and phone_norm:
                canonical_name = gc_phone_to_cname.get(phone_norm, "")
                if canonical_name:
                    # Try direct name→email lookup in GC data
                    ge = gc_name_to_email_lookup.get(canonical_name.lower())
                    if ge:
                        matched_email = _ensure_contact(ge)
                    else:
                        # Fuzzy against existing contacts
                        for email, c in contacts.items():
                            if fuzzy_str_match(canonical_name, c.get("name", ""), 0.85):
                                matched_email = email
                                break

            # 4. Fallback: SMS name → GC email or fuzzy match existing
            if not matched_email and name:
                # Direct GC name lookup
                ge = gc_name_to_email_lookup.get(name.lower())
                if ge:
                    matched_email = _ensure_contact(ge)
                else:
                    # Fuzzy against existing contacts first
                    for email, c in contacts.items():
                        if fuzzy_str_match(name, c.get("name", ""), 0.9):
                            matched_email = email
                            break
                    # Then fuzzy against GC name→email lookup (lets
                    # "Alex Guest" match "Alex Guest")
                    if not matched_email:
                        name_lower = name.lower()
                        for gc_name, ge in gc_name_to_email_lookup.items():
                            # Match if one name is a prefix of the other
                            # (covers Zhen→Zhenhuan, Kat→Katherine, etc.)
                            if (len(gc_name) >= 5 and len(name_lower) >= 4
                                    and (gc_name.startswith(name_lower.split()[0])
                                         or name_lower.startswith(gc_name.split()[0]))
                                    and name_lower.split()[-1] == gc_name.split()[-1]):
                                matched_email = _ensure_contact(ge)
                                break

            if matched_email and matched_email in contacts:
                c = contacts[matched_email]
                c["sms_messages"] += data.get("message_count", 0)
                if "sms" not in c["platforms"]:
                    c["platforms"].append("sms")
                c["sms_samples"].extend(data.get("messages", [])[-50:])
                if not c.get("phone") and phone:
                    c["phone"] = phone
                update_dates(c, data.get("first_message"), data.get("last_message"))
                # Upgrade name: if GC has a canonical name, prefer it
                gc_name = gc_phone_to_cname.get(phone_norm, "") if phone_norm else ""
                if gc_name:
                    add_display_name(c, gc_name)
                if name:
                    add_display_name(c, name)
            else:
                # Orphan SMS contact: key by normalized phone for dedup
                synth_email = f"sms:{phone_norm}" if phone_norm else f"sms:{name_to_slug(name) if name else 'unknown'}"
                c = contacts.setdefault(synth_email, blank_contact(synth_email))
                # Prefer the GC canonical name if we have one
                gc_name = gc_phone_to_cname.get(phone_norm, "") if phone_norm else ""
                best = gc_name or name
                if best and not c["name"]:
                    c["name"] = best
                if gc_name:
                    add_display_name(c, gc_name)
                if name:
                    add_display_name(c, name)
                c["phone"] = phone
                c["sms_messages"] += data.get("message_count", 0)
                c["sms_samples"].extend(data.get("messages", [])[-50:])
                if "sms" not in c["platforms"]:
                    c["platforms"].append("sms")
                update_dates(c, data.get("first_message"), data.get("last_message"))
        print(f"Messages: {len(messages_data.get('contacts', []))} contacts merged", file=sys.stderr)

    print(f"\nAfter source merge: {len(contacts)} contacts", file=sys.stderr)

    # ── Step 4: Pick best names ────────────────────────────
    for email, c in contacts.items():
        if not c["name"]:
            c["name"] = best_name(c["display_names"])
        # Ensure name is in display_names
        if c["name"] and c["name"] not in c["display_names"]:
            c["display_names"].insert(0, c["name"])

    # ── Step 5: Normalize all names (Last,First -> First Last, etc.) ──
    for email, c in contacts.items():
        original = c["name"]
        fixed = normalize_name(original)
        if fixed != original:
            c["name"] = fixed
            add_display_name(c, fixed)

    # ── Step 6: Resolve names via saved Google Contacts (authoritative) ──
    resolved_names = 0
    for email, c in contacts.items():
        gc = gc_email_to_name.get(email)
        if not gc or gc.get("source") != "saved":
            continue
        gc_name = gc.get("name", "")
        if not gc_name:
            continue
        add_display_name(c, gc_name)
        current = c.get("name", "")
        # Override if: blank, or current is handle-style, or GC name is more "real"
        if (not current
                or looks_like_username(current)
                or (looks_like_real_name(gc_name) and not looks_like_real_name(current))):
            if c["name"] != gc_name:
                c["name"] = gc_name
                resolved_names += 1
        if gc.get("phone") and not c.get("phone"):
            c["phone"] = gc["phone"]
    if resolved_names:
        print(f"Name resolution via saved GC: {resolved_names}", file=sys.stderr)

    # Re-normalize after GC resolution (in case GC name was Last,First etc.)
    for email, c in contacts.items():
        c["name"] = normalize_name(c["name"])

    # ── Step 7: Alias detection ────────────────────────────
    auto_aliases = dict(email_aliases_manual)

    # 7a: From saved Google Contacts — same name under multiple emails
    gc_name_to_emails = defaultdict(list)
    for email, entry in gc_email_to_name.items():
        if entry.get("source") == "saved" and entry.get("name"):
            gc_name_to_emails[entry["name"]].append(email)

    for gc_name, email_list in gc_name_to_emails.items():
        if len(email_list) < 2:
            continue
        present = [e for e in email_list if e in contacts]
        if len(present) < 2:
            continue
        # Canonical = highest raw activity
        def activity(e):
            c = contacts[e]
            return (c.get("gmail_sent", 0) * 3 + c.get("gmail_received", 0)
                    + c.get("meeting_count", 0) * 5 + c.get("whatsapp_messages", 0)
                    + c.get("sms_messages", 0))
        present.sort(key=activity, reverse=True)
        canonical = present[0]
        for alias in present[1:]:
            if alias != canonical and alias not in auto_aliases:
                auto_aliases[alias] = canonical
    gc_alias_count = len(auto_aliases) - len(email_aliases_manual)

    # 7b: Fuzzy lastname + nickname matching — pairwise within each lastname
    def is_synthetic(e):
        return e.startswith("sms:") or e.startswith("whatsapp:")

    def raw_activity(c):
        return (
            (c.get("gmail_sent", 0) or 0) * 3
            + (c.get("gmail_received", 0) or 0)
            + (c.get("meeting_count", 0) or 0) * 5
            + (c.get("whatsapp_messages", 0) or 0) * 2
            + (c.get("sms_messages", 0) or 0) * 2
        )

    by_last_name = defaultdict(list)
    for email, c in contacts.items():
        name = c.get("name", "")
        parts = name.split()
        if len(parts) < 2:
            continue
        last = parts[-1].lower()
        first = parts[0].lower()
        by_last_name[last].append((email, first, raw_activity(c)))

    fuzzy_count = 0
    for last, entries in by_last_name.items():
        if len(entries) < 2:
            continue
        # Group entries by matching first-name (with nickname resolution).
        # Union-find-style: each entry joins the first existing group whose
        # first name is nickname-compatible.
        groups = []  # list of lists of (email, first, activity)
        for entry in entries:
            _, first, _ = entry
            placed = False
            for g in groups:
                if first_names_match(g[0][1], first):
                    g.append(entry)
                    placed = True
                    break
            if not placed:
                groups.append([entry])

        # Within each group of size >=2, pick canonical (real email > synthetic, high activity)
        for g in groups:
            if len(g) < 2:
                continue
            g.sort(key=lambda x: (is_synthetic(x[0]), -x[2]))
            canonical_email = g[0][0]
            for alias_email, _, _ in g[1:]:
                if alias_email == canonical_email or alias_email in auto_aliases:
                    continue
                auto_aliases[alias_email] = canonical_email
                fuzzy_count += 1

    print(f"Aliases: {len(email_aliases_manual)} manual + {gc_alias_count} from GC + {fuzzy_count} fuzzy = {len(auto_aliases)} total", file=sys.stderr)

    # ── Step 8: Merge aliases into canonicals ─────────────
    merged = 0
    for alias_email, canonical_email in auto_aliases.items():
        if alias_email not in contacts:
            continue
        if canonical_email not in contacts:
            # Rename alias to canonical
            contacts[canonical_email] = contacts.pop(alias_email)
            contacts[canonical_email]["email"] = canonical_email
            continue
        if alias_email == canonical_email:
            continue

        alias = contacts[alias_email]
        canon = contacts[canonical_email]

        canon["gmail_sent"] += alias.get("gmail_sent", 0)
        canon["gmail_received"] += alias.get("gmail_received", 0)
        canon["meeting_count"] += alias.get("meeting_count", 0)
        canon["krisp_meetings"] += alias.get("krisp_meetings", 0)
        canon["whatsapp_messages"] += alias.get("whatsapp_messages", 0)
        canon["sms_messages"] += alias.get("sms_messages", 0)
        canon["gmail_subjects"].extend(alias.get("gmail_subjects", []))
        canon["gmail_body_excerpts"].extend(alias.get("gmail_body_excerpts", []))
        canon["meeting_titles"].extend(alias.get("meeting_titles", []))
        canon["whatsapp_samples"].extend(alias.get("whatsapp_samples", []))
        canon["sms_samples"].extend(alias.get("sms_samples", []))
        for dn in alias.get("display_names", []):
            add_display_name(canon, dn)
        for p in alias.get("platforms", []):
            if p not in canon["platforms"]:
                canon["platforms"].append(p)
        if not canon.get("signature") and alias.get("signature"):
            canon["signature"] = alias["signature"]
        update_dates(canon, alias.get("first_interaction"), alias.get("last_interaction"))
        if not canon.get("phone") and alias.get("phone"):
            canon["phone"] = alias["phone"]

        del contacts[alias_email]
        merged += 1
    print(f"Merged {merged} alias pairs into canonicals. Contacts now: {len(contacts)}", file=sys.stderr)

    # ── Step 9: Apply is_person gate ──────────────────────
    kept = {}
    drop_reasons = defaultdict(int)
    for email, c in contacts.items():
        ok, reason = is_person(c, saved_gc_emails=saved_gc_emails)
        if ok:
            kept[email] = c
        else:
            drop_reasons[reason] += 1
    print(f"is_person gate: kept {len(kept)}, dropped {sum(drop_reasons.values())}", file=sys.stderr)
    if drop_reasons:
        print(f"  Reasons: {dict(drop_reasons)}", file=sys.stderr)

    # ── Step 10: Score + finalize ─────────────────────────
    final = []
    for email, c in kept.items():
        score = compute_score(c, config)
        slug = name_to_slug(c["name"]) if c["name"] else email_to_slug(email)
        # Synthetic keys (sms:..., whatsapp:...) aren't real emails — clear
        # the email field for display and promote phone/handle metadata.
        display_email = c.get("email", "") or ""
        if display_email.startswith("sms:"):
            if not c.get("phone"):
                c["phone"] = "+" + display_email[4:] if display_email[4:].isdigit() else display_email[4:]
            display_email = ""
        elif display_email.startswith("whatsapp:"):
            display_email = ""
        final.append({
            **c,
            "email": display_email,
            "score": score,
            "auto_circle": auto_circle(c, config),
            "slug": slug,
        })

    final.sort(key=lambda c: -c["score"])

    result = {
        "status": "ok",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "total_contacts": len(final),
        "contacts": final,
        "unresolved_krisp": unresolved_krisp,
    }

    output_path = CACHE_DIR / "contacts-merged.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nWritten: {output_path}", file=sys.stderr)
    print(f"Total canonical contacts: {len(final)}", file=sys.stderr)
    print(f"Unresolved Krisp: {len(unresolved_krisp)}", file=sys.stderr)


if __name__ == "__main__":
    aggregate()
