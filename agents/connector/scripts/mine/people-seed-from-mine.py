#!/usr/bin/env python3
"""
people-seed-from-mine.py — Create people files + brain facts from mined data.

Durable facts only (Flux-informed):
- Keep: job+company, family relationships, stable locations, enduring interests
- Drop: commitments, deadlines, plans, project/topic details (ephemeral)

Numeric relationship fields (ported from Flux relationship_inference.py):
- social_distance (0-1)
- power_differential (-1 to +1)
- communication_direction (personal/lateral/upward/downward/external)

Usage:
  python3 people-seed-from-mine.py --dry-run         # Preview
  python3 people-seed-from-mine.py                    # Create files

Input: cache/review-ready.json
Output: people files + facts file
"""

import io
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, email_to_slug, load_config, name_to_slug

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# The Clawford Dropbox lives at E:\Dropbox\ on Windows (the work Dropbox
# that syncs to the VPS). DO NOT use expanduser("~/Dropbox/...") — Sam
# has a separate personal Dropbox at C:\Users\Sam\Dropbox that does NOT
# sync to the VPS, and Python's expanduser resolves to that one.
_WINDOWS_CLAWFORD_DROPBOX = "E:/Dropbox/openclaw-backup"
_UNIX_FALLBACK = os.path.expanduser("~/Dropbox/openclaw-backup")
_DROPBOX_ROOT = _WINDOWS_CLAWFORD_DROPBOX if os.path.isdir(_WINDOWS_CLAWFORD_DROPBOX) else _UNIX_FALLBACK
BRAIN_PEOPLE = os.path.join(_DROPBOX_ROOT, "people")
BRAIN_FACTS = os.path.join(_DROPBOX_ROOT, "facts")

PERSON_TEMPLATE = """# {name}

- **slug:** {slug}
- **circles:** {circles}
- **relationship:** {relationship}
- **relationship_type:** {relationship_type}
- **preferred_channel:** {preferred_channel}
- **tone:** {tone}
- **email:** {email}
- **phone:** {phone}
- **platforms:** {platforms}
- **last_interaction:** {last_interaction}
- **social_distance:** {social_distance}
- **power_differential:** {power_differential}
- **communication_direction:** {communication_direction}
- **context_notes:** {context_notes}
- **notes:** Auto-created by mining pipeline on {seed_date}. Score: {score}.
"""

FACT_TEMPLATE = """
---

- **id:** {fact_id}
- **content:** {content}
- **subject:** {subject}
- **source_type:** {source_type}
- **source_detail:** {source_detail}
- **source_agent:** connector
- **confidence:** {confidence}
- **category:** {category}
- **recorded_at:** {recorded_at}
"""


# ── Durable fact classification ──────────────────────────────

EPHEMERAL_MARKERS = [
    # Commitments / promises (ephemeral)
    r"\b(said|mentioned|promised|committed|agreed|told|plan(s|ning)? to)\b",
    r"\b(will|gonna|going to) (send|do|meet|call|visit|email)",
    # Specific dates (plan-category, stale after 2 years)
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}",
    r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b",
    r"\bnext (week|month|quarter|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    r"\blast (week|month|quarter|year)\b",
    # Meeting/event ephemera
    r"\b(meeting|call|interview|sync|standup|check-in|1:1) (about|on|at|with|regarding)",
    # Topic/project details (go in context_notes, not facts)
    r"\b(working on|discussing|talking about|focused on) the\b",
    # Action items
    r"\b(needs to|should|must|required to)\b",
]

DURABLE_PATTERNS = {
    # Job + company
    "job": [
        r"\bworks? (at|for|as|in)\b",
        r"\b(ceo|cto|cfo|coo|vp|vice president|director|manager|engineer|designer|founder|partner|principal|senior|staff|lead|head of|recruiter|attorney|cpa|doctor|nurse|teacher|professor)\b",
        r"\bemployed (at|by)\b",
    ],
    # Family relationships
    "family": [
        r"\b(is Sam'?s?|Sam'?s?) (spouse|wife|husband|partner|son|daughter|child|kid|parent|mom|mother|dad|father|brother|sister|sibling|uncle|aunt|cousin|grandma|grandpa|grandmother|grandfather|nephew|niece|in-law|sister-in-law|brother-in-law|mother-in-law|father-in-law)\b",
        r"\b(family|relatives?|relative)\b",
    ],
    # Stable locations
    "location": [
        r"\b(lives? in|based in|located in|resides? in|from|hails? from)\b",
        r"\b(san francisco|new york|los angeles|chicago|seattle|boston|austin|portland|sunnyvale|palo alto|mountain view|menlo park|berkeley|oakland|silicon valley|bay area|east coast|west coast)\b",
    ],
    # Long-term health/life context
    "situation": [
        r"\b(has|suffers from|recovering from|diagnosed with|undergoing|going through) (cancer|chemo|chemotherapy|surgery|treatment|illness|disease|divorce|grief|loss|pregnancy)\b",
        r"\bpregnan(t|cy)\b",
        r"\b(has|have) \d+ (kid|child|children|son|daughter|baby|babies)",
    ],
    # Enduring interests / hobbies
    "interest": [
        r"\b(runs|plays|teaches|studies|practices) (marathons?|piano|guitar|tennis|golf|chess|yoga|meditation)",
        r"\b(loves|enjoys|passionate about|hobby|hobbies)\b",
    ],
}


def classify_fact(fact_text):
    """Classify an LLM-extracted fact as durable + category, or drop as ephemeral.

    Returns dict with {category, epistemic_type, confidence} or None if ephemeral.
    """
    if not fact_text or len(fact_text) < 5:
        return None

    text_lower = fact_text.lower()

    # Ephemeral filter first — if any ephemeral marker matches, drop it
    for pattern in EPHEMERAL_MARKERS:
        if re.search(pattern, text_lower):
            return None

    # Family relationships never decay
    for pattern in DURABLE_PATTERNS["family"]:
        if re.search(pattern, text_lower):
            return {
                "category": "identity",
                "source_type": "observed",
                "confidence": 0.9,
            }

    # Job/company and locations are "established" (365d half-life)
    for pattern in DURABLE_PATTERNS["job"]:
        if re.search(pattern, text_lower):
            return {
                "category": "established",
                "source_type": "observed",
                "confidence": 0.75,
            }

    for pattern in DURABLE_PATTERNS["location"]:
        if re.search(pattern, text_lower):
            return {
                "category": "established",
                "source_type": "observed",
                "confidence": 0.7,
            }

    # Long-term situations (90d half-life)
    for pattern in DURABLE_PATTERNS["situation"]:
        if re.search(pattern, text_lower):
            return {
                "category": "situation",
                "source_type": "observed",
                "confidence": 0.6,
            }

    # Enduring interests
    for pattern in DURABLE_PATTERNS["interest"]:
        if re.search(pattern, text_lower):
            return {
                "category": "established",
                "source_type": "inference",
                "confidence": 0.6,
            }

    return None  # Didn't match any durable pattern — skip


# ── Numeric relationship metrics (ported from Flux) ──────────

def compute_relationship_metrics(contact, config):
    """Compute social_distance, power_differential, communication_direction
    from aggregated interaction data. Ported from Flux relationship_inference.py.
    """
    gmail_sent = contact.get("gmail_sent", 0) or 0
    gmail_recv = contact.get("gmail_received", 0) or 0
    meetings = contact.get("meeting_count", 0) or 0
    wa = contact.get("whatsapp_messages", 0) or 0
    sms = contact.get("sms_messages", 0) or 0

    total_msgs = gmail_sent + gmail_recv + wa + sms + meetings * 5

    # social_distance: 0 (close) to 1 (stranger)
    # Log-scaled so huge counts still approach 0 and small counts approach 1
    if total_msgs <= 0:
        social_distance = 1.0
    else:
        # 1000+ messages → ~0.1; 100 → ~0.3; 10 → ~0.6; 1 → ~0.9
        social_distance = max(0.0, min(1.0, 1.0 - math.log10(total_msgs + 1) / 4.0))

    # power_differential: -1 (they're senior) to +1 (I'm senior)
    # Based on who initiates: if Sam sends way more than he receives, it's downward
    total_gmail = gmail_sent + gmail_recv
    if total_gmail == 0:
        power_differential = 0.0
    else:
        power_differential = round((gmail_sent - gmail_recv) / total_gmail, 2)

    # communication_direction
    email = (contact.get("email") or "").lower()
    personal_domains = config.get("personal_domains", [])
    is_personal_domain = any(email.endswith(f"@{d}") for d in personal_domains)
    llm_type = (contact.get("llm") or {}).get("relationship_type", "")

    if llm_type == "family":
        direction = "personal"
    elif is_personal_domain and wa + sms > 0:
        direction = "personal"
    elif llm_type in ("recruiter", "vendor", "client"):
        direction = "external"
    elif llm_type == "colleague":
        if power_differential > 0.3:
            direction = "downward"
        elif power_differential < -0.3:
            direction = "upward"
        else:
            direction = "lateral"
    else:
        direction = "lateral"

    return {
        "social_distance": round(social_distance, 2),
        "power_differential": power_differential,
        "communication_direction": direction,
    }


# ── Main ─────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    enrich_existing = "--enrich-existing" in sys.argv

    review_path = CACHE_DIR / "review-ready.json"
    if not review_path.exists():
        print("ERROR: Run contact-review.py --finalize first.", file=sys.stderr)
        sys.exit(1)

    config = load_config()

    with open(review_path, encoding="utf-8") as f:
        data = json.load(f)

    contacts = data.get("contacts", [])
    seed_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recorded_at = datetime.now(timezone.utc).isoformat()
    fact_month = datetime.now(timezone.utc).strftime("%Y-%m")

    created = 0
    skipped = 0
    enriched = 0
    facts = []
    fact_seq = 1
    facts_dropped = 0

    if not dry_run:
        os.makedirs(BRAIN_PEOPLE, exist_ok=True)
        os.makedirs(BRAIN_FACTS, exist_ok=True)

    for contact in contacts:
        slug = contact.get("slug", "")
        if not slug:
            name = contact.get("name", "")
            slug = name_to_slug(name) if name else email_to_slug(contact.get("email", ""))
        if not slug:
            continue

        filepath = os.path.join(BRAIN_PEOPLE, f"{slug}.md")
        llm = contact.get("llm", {}) or {}
        sig = contact.get("signature", {}) or {}

        if os.path.exists(filepath) and not enrich_existing:
            skipped += 1
            continue

        # Build people file fields
        circle = contact.get("circle") or llm.get("circle_suggestion") or contact.get("auto_circle", "professional-outer")
        rel_type = llm.get("relationship_type") or contact.get("relationship_type", "")
        tone = llm.get("tone", "professional")
        context = (llm.get("context_notes") or "").replace("\n", " ")[:500]
        phone = sig.get("phone", "—") or "—"
        platforms = ", ".join(contact.get("platforms", ["email"]))

        preferred = "email"
        if "whatsapp" in contact.get("platforms", []):
            preferred = "WhatsApp"
        elif "sms" in contact.get("platforms", []):
            preferred = "iMessage"

        # Numeric metrics (Step 2b)
        metrics = compute_relationship_metrics(contact, config)

        content = PERSON_TEMPLATE.format(
            name=contact.get("name", slug),
            slug=slug,
            circles=circle,
            relationship=rel_type or "contact",
            relationship_type=rel_type or "",
            preferred_channel=preferred,
            tone=tone,
            email=contact.get("email", "—") or "—",
            phone=phone,
            platforms=platforms,
            last_interaction=contact.get("last_interaction", seed_date) or seed_date,
            social_distance=metrics["social_distance"],
            power_differential=metrics["power_differential"],
            communication_direction=metrics["communication_direction"],
            context_notes=context or "—",
            seed_date=seed_date,
            score=contact.get("score", 0),
        ).strip() + "\n"

        if dry_run:
            print(f"  CREATE: {slug} ({contact.get('name', slug)[:30]}, {circle})", file=sys.stderr)
        else:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

        created += 1

        # ── Generate durable facts only ──
        fact_sources = []

        # Signature-derived facts disabled: cached parse_signature output is
        # too noisy (quote markers, addresses, narrative fragments). Trust
        # only LLM key_facts that pass the durable-pattern filter below.

        # LLM key_facts, filtered through classify_fact()
        for llm_fact in llm.get("key_facts", []):
            classification = classify_fact(llm_fact)
            if classification is None:
                facts_dropped += 1
                continue
            # Don't duplicate signature-derived job facts
            if sig.get("title") and sig.get("company") and (
                sig["company"].lower() in llm_fact.lower()
                and sig["title"].lower() in llm_fact.lower()
            ):
                continue
            fact_sources.append({
                "content": llm_fact,
                "source_type": classification["source_type"],
                "source_detail": "LLM-extracted from interaction data",
                "category": classification["category"],
                "confidence": classification["confidence"],
            })

        # Format and add to facts list
        for fs in fact_sources:
            fact_id = f"connector-{seed_date}-{fact_seq:03d}"
            fact_seq += 1
            facts.append(FACT_TEMPLATE.format(
                fact_id=fact_id,
                content=fs["content"],
                subject=slug,
                source_type=fs["source_type"],
                source_detail=fs["source_detail"],
                category=fs["category"],
                confidence=fs["confidence"],
                recorded_at=recorded_at,
            ))

    # Write facts file
    if facts and not dry_run:
        facts_path = os.path.join(BRAIN_FACTS, f"{fact_month}.md")
        mode = "a" if os.path.exists(facts_path) else "w"
        with open(facts_path, mode, encoding="utf-8") as f:
            if mode == "w":
                f.write(f"# Facts — {fact_month}\n")
            for fact in facts:
                f.write(fact)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Results:", file=sys.stderr)
    print(f"  People files created: {created}", file=sys.stderr)
    print(f"  Skipped (already exist): {skipped}", file=sys.stderr)
    print(f"  Durable facts generated: {len(facts)}", file=sys.stderr)
    print(f"  LLM facts dropped as ephemeral: {facts_dropped}", file=sys.stderr)

    if not dry_run and facts:
        print(f"  Facts written to: {BRAIN_FACTS}/{fact_month}.md", file=sys.stderr)


if __name__ == "__main__":
    main()
