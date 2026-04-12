#!/usr/bin/env python3
"""
heuristic-enrich.py — Deterministic enrichment of merged contacts.

Reads cache/contacts-merged.json and produces cache/contacts-enriched.json
with LLM-equivalent fields (relationship_type, key_facts, tone, context_notes,
circle_suggestion) derived purely from signal heuristics and signature data.

No API keys, no external LLM calls. Scripts do I/O only; Sam's spot-check
at Stage E provides the quality gate.

Usage:
  python3 heuristic-enrich.py
"""

import io
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, load_config

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


BRIAN_FAMILY_LAST = "Smith"
FAMILY_EMAIL_MAP = {
    "robin.smith@example.com": ("Robin Smith", "mother", "family-extended"),
    "pat.smith@example.com": ("Pat Smith", "father", "family-extended"),
    "pat.smith@example.net": ("Pat Smith", "father", "family-extended"),
    "chris.smith@example.com": ("Chris Smith", "brother", "family-inner"),
    "dana.smith@example.com": ("Dana Smith", "sister-in-law", "family-extended"),
    "alex.smith@example.com": ("Alex Smith", "family", "family-extended"),
    "alex.rivera@example.com": ("Alex Rivera", "wife", "family-inner"),
    "alex.rivera@example.com": ("Alex Rivera", "wife", "family-inner"),
}

RECRUITER_KEYWORDS = {
    "recruit", "opportunity", "talent", "role", "candidate", "interview",
    "hiring", "position", "offer", "compensation",
}
CLIENT_KEYWORDS = {"engagement", "proposal", "invoice", "contract", "sow", "deliverable"}
VENDOR_KEYWORDS = {"subscription", "renewal", "payment", "billing", "account", "receipt"}


def tokenize(s):
    return re.findall(r"[a-z]+", (s or "").lower())


def classify_relationship(c, config):
    """Derive relationship_type from signals."""
    email = (c.get("email") or "").lower()
    name = c.get("name") or ""
    domain = email.split("@", 1)[1] if "@" in email else ""
    personal_domains = set(config.get("personal_domains", []))
    family_first = set((n.lower() for n in config.get("family_inner_names", [])))

    # Explicit family mapping
    if email in FAMILY_EMAIL_MAP:
        return "family"

    # Alex by first name match (also handled by manual map above)
    first = name.split()[0].lower() if name else ""
    if first in family_first:
        return "family"

    # Shared surname Smith + not Sam himself
    parts = name.lower().split()
    if len(parts) >= 2 and parts[-1] == BRIAN_FAMILY_LAST:
        return "family"

    sent = c.get("gmail_sent", 0) or 0
    recv = c.get("gmail_received", 0) or 0
    mtg = c.get("meeting_count", 0) or 0
    krisp = c.get("krisp_meetings", 0) or 0
    wa = c.get("whatsapp_messages", 0) or 0
    sms = c.get("sms_messages", 0) or 0

    is_personal_dom = domain in personal_domains or email.startswith(("sms:", "whatsapp:"))
    has_rich_messaging = wa >= 20 or sms >= 50
    has_bilateral_email = sent >= 3 and recv >= 3

    # Recruiter / vendor / client heuristics from subject + meeting corpus
    corpus = " ".join(c.get("gmail_subjects", [])[:50] + c.get("meeting_titles", [])[:20])
    corpus_tokens = set(tokenize(corpus))
    if corpus_tokens & RECRUITER_KEYWORDS and mtg <= 3 and wa == 0 and sms == 0:
        return "recruiter"
    if corpus_tokens & VENDOR_KEYWORDS and sent <= 2:
        return "vendor"
    if corpus_tokens & CLIENT_KEYWORDS:
        return "client"

    # Friends: personal domain + rich messaging (whatsapp/sms substantial)
    if (is_personal_dom or has_rich_messaging) and has_rich_messaging and mtg < 10:
        return "friend"

    # Colleagues: lots of meetings or krisp meetings
    if mtg >= 3 or krisp >= 3:
        return "colleague"

    # Friend by personal domain + meaningful email or messaging
    if is_personal_dom and (has_bilateral_email or has_rich_messaging):
        return "friend"

    return "acquaintance"


def derive_tone(rel_type, c):
    if rel_type == "family":
        return "warm"
    if rel_type in ("friend",):
        return "casual"
    if rel_type in ("client", "recruiter"):
        return "professional"
    if rel_type in ("vendor",):
        return "formal"
    return "professional"


def derive_circle(rel_type, c, config):
    if rel_type == "family":
        email = (c.get("email") or "").lower()
        if email in FAMILY_EMAIL_MAP:
            return FAMILY_EMAIL_MAP[email][2]
        first = c.get("name", "").split()[0].lower() if c.get("name") else ""
        if first in {n.lower() for n in config.get("family_inner_names", [])}:
            return "family-inner"
        return "family-extended"

    mtg = c.get("meeting_count", 0) or 0
    krisp = c.get("krisp_meetings", 0) or 0
    last = c.get("last_interaction")
    recent_30d = False
    if last:
        try:
            last_date = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
            recent_30d = (datetime.now(timezone.utc).date() - last_date).days <= 30
        except ValueError:
            pass

    if rel_type == "colleague" and (mtg + krisp) >= 5 and recent_30d:
        return "professional-inner"
    if rel_type == "friend":
        wa = c.get("whatsapp_messages", 0) or 0
        sms = c.get("sms_messages", 0) or 0
        if wa >= 50 or sms >= 100 or recent_30d:
            return "friends-close"
        return "friends-acquaintance"
    if rel_type in ("client", "colleague"):
        return "professional-outer"
    if rel_type == "recruiter":
        return "professional-outer"
    if rel_type == "vendor":
        return "holiday-card"
    return "professional-outer"


# ── Key fact extraction ────────────────────────────────────

_BAD_SIG_TOKENS = (
    "wrote:", "@", "pm>", "am>", "http", "rights reserved", "unsubscribe",
    "daycare", "daily", "birth+",
)
_QUOTE_START = re.compile(r"^[\s>\-*<]+|^(on\s+\w{3,}|sent\s+from)", re.IGNORECASE)
_HAS_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_VALID_SIG_FIELD = re.compile(r"^[A-Z][A-Za-z0-9 .,&'\-/()]{2,60}$")
_VALID_PHONE = re.compile(r"^[+\d][\d\s\-().]{8,20}\d$")


def _clean_sig_field(value):
    """Strict signature-field validation: accept only clean Title Case strings."""
    if not value:
        return None
    v = str(value).strip()
    if not v or len(v) > 80 or len(v) < 3:
        return None
    if _QUOTE_START.match(v):
        return None
    if any(t in v.lower() for t in _BAD_SIG_TOKENS):
        return None
    if _HAS_YEAR.search(v):
        return None
    # Must look like a proper noun / title (starts uppercase, alpha-dominant)
    if not _VALID_SIG_FIELD.match(v):
        return None
    return v


def _clean_phone(value):
    """Strict phone validation: 10-15 digits, no decimal coords."""
    if not value:
        return None
    v = str(value).strip()
    if not v or "." in v:  # coords have decimals
        return None
    digits = "".join(ch for ch in v if ch.isdigit())
    if 10 <= len(digits) <= 15 and _VALID_PHONE.match(v):
        return v
    return None


def extract_signature_facts(c):
    """Extract durable facts from parsed signature.

    Cached signature data is noise-prone (quote markers, narrative fragments,
    addresses). We only trust LinkedIn URLs and phone numbers — never the
    free-text title/company fields for fact generation. Sam can add job
    facts manually from LinkedIn if needed.
    """
    facts = []
    sig = c.get("signature") or {}
    phone = _clean_phone(sig.get("phone"))
    linkedin = sig.get("linkedin")
    if linkedin and "linkedin.com" not in str(linkedin).lower():
        linkedin = None

    if phone:
        facts.append(f"Phone: {phone}")
    if linkedin:
        facts.append(f"LinkedIn: {linkedin}")
    return facts


TOPIC_STOPWORDS = {
    "re", "fwd", "fw", "the", "a", "an", "on", "in", "at", "of", "for", "to",
    "from", "with", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "being", "has", "have", "had", "do", "does", "did", "will",
    "would", "should", "could", "may", "might", "meeting", "call", "chat",
    "hi", "hello", "hey", "thanks", "thank", "please", "about", "your",
    "you", "my", "our", "we", "us", "they", "them", "this", "that", "it",
    "its", "as", "by", "so", "if", "no", "not", "can", "get", "got",
    "new", "up", "out", "me", "Sam", "fyi", "question", "questions",
    "quick",
}


def extract_topic_facts(c):
    """Mine top recurring topics from subjects + meeting titles."""
    subjects = c.get("gmail_subjects", [])[:100]
    meetings = c.get("meeting_titles", [])[:50]
    tokens = []
    for s in subjects + meetings:
        for t in tokenize(s):
            if len(t) >= 4 and t not in TOPIC_STOPWORDS:
                tokens.append(t)
    counter = Counter(tokens)
    top = [t for t, n in counter.most_common(6) if n >= 3]
    if not top:
        return []
    facts = []
    if len(top) >= 3:
        facts.append(f"Recurring topics: {', '.join(top[:5])}")
    return facts


def extract_platform_facts(c):
    """Describe how Sam interacts with this person across channels."""
    facts = []
    platforms = c.get("platforms", [])
    sent = c.get("gmail_sent", 0) or 0
    recv = c.get("gmail_received", 0) or 0
    mtg = c.get("meeting_count", 0) or 0
    krisp = c.get("krisp_meetings", 0) or 0
    wa = c.get("whatsapp_messages", 0) or 0
    sms = c.get("sms_messages", 0) or 0

    if wa >= 1000 or sms >= 1000:
        facts.append("Daily personal communication (heavy WhatsApp/SMS)")
    elif wa >= 100 or sms >= 100:
        facts.append("Regular personal communication via messaging")
    elif wa + sms >= 10:
        facts.append("Occasional messaging contact")

    if mtg >= 20:
        facts.append(f"Frequent meeting collaborator ({mtg} meetings)")
    elif mtg >= 5:
        facts.append(f"Meets periodically ({mtg} meetings)")

    if krisp >= 5:
        facts.append(f"Recorded Krisp conversations ({krisp})")

    if sent >= 20 and recv >= 20:
        facts.append(f"Active bilateral email correspondence ({sent} sent / {recv} received)")
    elif sent >= 5 and recv >= 5:
        facts.append(f"Regular email correspondence")
    return facts


def extract_time_facts(c):
    """Facts about interaction recency / longevity."""
    facts = []
    first = c.get("first_interaction")
    last = c.get("last_interaction")
    now = datetime.now(timezone.utc).date()

    def parse(d):
        if not d:
            return None
        try:
            return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    first_d = parse(first)
    last_d = parse(last)

    if first_d and last_d:
        years = (last_d - first_d).days // 365
        if years >= 1:
            facts.append(f"Known since {first_d.year} ({years}+ year relationship)")
    if last_d:
        days_ago = (now - last_d).days
        if days_ago <= 14:
            facts.append(f"Contacted within last 2 weeks")
        elif days_ago >= 365 * 3:
            facts.append(f"Lapsed — no contact in {days_ago // 365}+ years")
    return facts


def _iso_date_str(value):
    """Return YYYY-MM-DD part of any date-like string, or 'unknown'."""
    if not value:
        return "unknown"
    s = str(value)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    return s[:10]


def compose_context_notes(c, rel_type, facts):
    """1-2 sentence summary for Sam before next interaction."""
    name = c.get("name", "") or "This contact"
    last = c.get("last_interaction")
    last_str = _iso_date_str(last)

    if rel_type == "family":
        return f"Family member. Last contact {last_str}."
    if rel_type == "friend":
        return f"Friend — check in if it's been a while. Last contact {last_str}."
    if rel_type == "colleague":
        mtg = c.get("meeting_count", 0)
        return f"Work colleague — {mtg} meetings on record. Last contact {last_str}."
    if rel_type == "client":
        return f"Client engagement. Last contact {last_str}."
    if rel_type == "recruiter":
        return f"Recruiter outreach contact. Last contact {last_str}."
    if rel_type == "vendor":
        return f"Service provider. Last contact {last_str}."
    return f"Acquaintance. Last contact {last_str}."


def enrich_contact(c, config):
    rel_type = classify_relationship(c, config)
    tone = derive_tone(rel_type, c)
    circle = derive_circle(rel_type, c, config)

    facts = []
    facts.extend(extract_signature_facts(c))
    facts.extend(extract_platform_facts(c))
    facts.extend(extract_topic_facts(c))
    facts.extend(extract_time_facts(c))

    # Dedup, cap to 5
    seen = set()
    dedup = []
    for f in facts:
        k = f.lower()
        if k not in seen:
            dedup.append(f)
            seen.add(k)
    dedup = dedup[:5]

    notes = compose_context_notes(c, rel_type, dedup)

    return {
        "relationship_type": rel_type,
        "key_facts": dedup,
        "topics": [],
        "context_notes": notes,
        "tone": tone,
        "circle_suggestion": circle,
    }


def main():
    merged_path = CACHE_DIR / "contacts-merged.json"
    if not merged_path.exists():
        print(f"ERROR: {merged_path} not found. Run contact-aggregator.py first.", file=sys.stderr)
        sys.exit(1)

    with open(merged_path, encoding="utf-8") as f:
        data = json.load(f)

    contacts = data.get("contacts", [])
    config = load_config()

    print(f"Enriching {len(contacts)} contacts (heuristic, no LLM)...", file=sys.stderr)

    type_counts = Counter()
    facts_counter = Counter()
    for c in contacts:
        llm = enrich_contact(c, config)
        c["llm"] = llm
        type_counts[llm["relationship_type"]] += 1
        facts_counter[len(llm["key_facts"])] += 1

    data["contacts"] = contacts
    data["enriched_at"] = datetime.now(timezone.utc).isoformat()
    data["enrichment_method"] = "heuristic"

    out_path = CACHE_DIR / "contacts-enriched.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\nRelationship types: {dict(type_counts.most_common())}", file=sys.stderr)
    print(f"Facts per contact: {dict(sorted(facts_counter.items()))}", file=sys.stderr)
    print(f"\nWritten: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
