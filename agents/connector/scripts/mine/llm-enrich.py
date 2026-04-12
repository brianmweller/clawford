#!/usr/bin/env python3
"""
llm-enrich.py — LLM enrichment pass for mined contacts.

Uses OpenAI gpt-5.4-nano to extract per-person intelligence:
relationship type, key facts, topics, context notes, tone, circle suggestion.

Usage:
  python3 llm-enrich.py                  # Enrich all contacts
  python3 llm-enrich.py --resume         # Resume from checkpoint
  python3 llm-enrich.py --limit 10       # Only enrich first N contacts

Input: cache/aggregated-contacts.json
Output: cache/enriched-contacts.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, load_checkpoint, load_config, save_checkpoint

ENRICHMENT_PROMPT = """You are analyzing interaction data between Sam Smith and one of his contacts. Based on the evidence below, extract structured relationship intelligence.

Contact: {name}
Email: {email}
Platforms: {platforms}

Interaction stats:
- Emails sent by Sam: {gmail_sent}
- Emails received from them: {gmail_received}
- Calendar meetings together: {meetings}
- First interaction: {first_interaction}
- Last interaction: {last_interaction}

Email subjects (sample): {subjects}

Email signature data: {signature}

Meeting titles (sample): {meeting_titles}

Message samples: {message_samples}

---

Based on this evidence, provide a JSON object with these fields:

{{
  "relationship_type": "colleague" | "friend" | "family" | "client" | "acquaintance" | "vendor" | "recruiter",
  "key_facts": ["3-5 facts about this person: job, company, location, interests, life events"],
  "topics": ["list of recurring discussion topics between Sam and this person"],
  "context_notes": "1-2 sentence summary of what Sam should know for the next interaction with this person",
  "tone": "casual" | "warm" | "professional" | "formal",
  "circle_suggestion": "professional-inner" | "professional-outer" | "friends-close" | "family-extended" | "holiday-card"
}}

Respond with ONLY the JSON object, no markdown fencing or explanation."""


def build_prompt(contact):
    """Build the enrichment prompt with sqrt-scaled message sampling.

    Core relationships (high score) get rich context — many message samples,
    subjects, and meeting titles. Acquaintances get minimal context.
    Sample count ≈ sqrt(score), capped at 150.
    """
    import math

    score = contact.get("score", 5)
    sample_budget = min(int(math.sqrt(score)) + 2, 150)

    # Allocate budget across data types proportionally
    subjects = contact.get("gmail_subjects", [])[:sample_budget]
    meeting_titles = contact.get("meeting_titles", [])[:max(sample_budget // 3, 3)]

    # Message samples: body excerpts + WhatsApp + SMS
    msg_budget = max(sample_budget // 2, 3)
    message_samples = []

    for excerpt in contact.get("gmail_body_excerpts", [])[:msg_budget]:
        message_samples.append(f"[email] {excerpt[:300]}")

    # WhatsApp/SMS messages from the mined data (if available in contact)
    for msg in contact.get("whatsapp_samples", [])[:msg_budget]:
        direction = msg.get("direction", "?")
        text = msg.get("text", "")[:200]
        message_samples.append(f"[whatsapp {direction}] {text}")

    for msg in contact.get("sms_samples", [])[:msg_budget]:
        direction = msg.get("direction", "?")
        text = msg.get("text", "")[:200]
        message_samples.append(f"[sms {direction}] {text}")

    message_samples = message_samples[:sample_budget]

    sig = contact.get("signature", {})
    sig_str = json.dumps(sig) if sig else "none"

    return ENRICHMENT_PROMPT.format(
        name=contact.get("name", "Unknown"),
        email=contact.get("email", ""),
        platforms=", ".join(contact.get("platforms", [])),
        gmail_sent=contact.get("gmail_sent", 0),
        gmail_received=contact.get("gmail_received", 0),
        meetings=contact.get("meeting_count", 0),
        first_interaction=contact.get("first_interaction", "unknown"),
        last_interaction=contact.get("last_interaction", "unknown"),
        subjects="; ".join(subjects) if subjects else "none",
        signature=sig_str,
        meeting_titles="; ".join(meeting_titles) if meeting_titles else "none",
        message_samples="\n".join(message_samples) if message_samples else "none",
    )


def call_llm(prompt, config):
    """Call LLM API for enrichment. Tries Anthropic first, falls back to OpenAI."""
    import os

    # Try Anthropic (Claude)
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
        model = config.get("anthropic", {}).get("model", "claude-haiku-4-5-20251001")
        response = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
    # Fallback to OpenAI
    elif os.environ.get("OPENAI_API_KEY"):
        import openai
        model = config.get("openai", {}).get("model", "gpt-5.4-nano")
        client = openai.OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        text = response.choices[0].message.content.strip()
    else:
        raise RuntimeError("No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.")

    # Strip markdown fencing if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    return json.loads(text)


def enrich():
    config = load_config()
    batch_size = config.get("openai", {}).get("batch_size", 10)
    batch_delay = config.get("openai", {}).get("batch_delay_seconds", 1.0)

    # Load aggregated contacts
    agg_path = CACHE_DIR / "aggregated-contacts.json"
    if not agg_path.exists():
        print("ERROR: Run contact-aggregator.py first.", file=sys.stderr)
        sys.exit(1)

    with open(agg_path, encoding="utf-8") as f:
        agg_data = json.load(f)

    contacts = agg_data.get("contacts", [])

    # Load message samples from source files for richer prompts
    wa_samples = {}  # name → [messages]
    sms_samples = {}  # name/phone → [messages]

    wa_path = CACHE_DIR / "mined-whatsapp.json"
    if wa_path.exists():
        wa_data = json.load(open(wa_path, encoding="utf-8"))
        wa_list = wa_data.get("contacts", {})
        if isinstance(wa_list, list):
            for c in wa_list:
                wa_samples[c.get("name", "").lower()] = c.get("messages", [])
        elif isinstance(wa_list, dict):
            for k, c in wa_list.items():
                wa_samples[c.get("name", "").lower()] = c.get("messages", [])

    sms_path = CACHE_DIR / "mined-messages.json"
    if sms_path.exists():
        sms_data = json.load(open(sms_path, encoding="utf-8"))
        sms_list = sms_data.get("contacts", [])
        for c in sms_list:
            name = c.get("name", "").lower()
            phone = c.get("phone", "")
            msgs = c.get("messages", [])
            if name:
                sms_samples[name] = msgs
            if phone:
                sms_samples[phone] = msgs

    # Attach samples to contacts
    for contact in contacts:
        name = contact.get("name", "").lower()
        # WhatsApp samples
        for wa_name, msgs in wa_samples.items():
            if wa_name and (wa_name == name or wa_name in contact.get("display_names_lower", [name])):
                contact["whatsapp_samples"] = msgs
                break
        # SMS samples
        for sms_key, msgs in sms_samples.items():
            if sms_key == name:
                contact["sms_samples"] = msgs
                break

    print(f"Loaded message samples: {sum(1 for c in contacts if c.get('whatsapp_samples'))} WhatsApp, {sum(1 for c in contacts if c.get('sms_samples'))} SMS", file=sys.stderr)

    # Parse args
    resume = "--resume" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    if limit:
        contacts = contacts[:limit]

    # Check for resume
    checkpoint = None
    enriched_map = {}
    if resume:
        checkpoint = load_checkpoint("llm-enrich")
        if checkpoint:
            enriched_map = checkpoint.get("enriched", {})
            print(f"Resuming: {len(enriched_map)} already enriched", file=sys.stderr)

    print(f"Enriching {len(contacts)} contacts with LLM...", file=sys.stderr)

    for i, contact in enumerate(contacts):
        email = contact.get("email", "")
        name = contact.get("name", "")
        key = email or name

        if key in enriched_map:
            continue

        prompt = build_prompt(contact)

        try:
            llm_result = call_llm(prompt, config)
            enriched_map[key] = llm_result
            print(f"  [{i + 1}/{len(contacts)}] {name}: {llm_result.get('relationship_type', '?')}", file=sys.stderr)
        except Exception as e:
            print(f"  [{i + 1}/{len(contacts)}] {name}: ERROR — {e}", file=sys.stderr)
            enriched_map[key] = {"error": str(e)}

        # Checkpoint every batch
        if (i + 1) % batch_size == 0:
            save_checkpoint("llm-enrich", {"enriched": enriched_map})
            time.sleep(batch_delay)

    # Save final checkpoint
    save_checkpoint("llm-enrich", {"enriched": enriched_map})

    # Merge LLM results into contacts
    for contact in contacts:
        key = contact.get("email", "") or contact.get("name", "")
        llm = enriched_map.get(key, {})
        if llm and "error" not in llm:
            contact["llm"] = llm
            # Override auto-circle with LLM suggestion if present
            if llm.get("circle_suggestion"):
                contact["llm_circle"] = llm["circle_suggestion"]

    # Also enrich unresolved contacts
    for group_name in ["unresolved_krisp", "unmatched_whatsapp", "unmatched_messages"]:
        for item in agg_data.get(group_name, []):
            name = item.get("name", "")
            if name and name in enriched_map:
                item["llm"] = enriched_map[name]

    # Save enriched output
    agg_data["contacts"] = contacts
    agg_data["enriched_at"] = datetime.now(timezone.utc).isoformat()

    output_path = CACHE_DIR / "enriched-contacts.json"
    with open(output_path, "w") as f:
        json.dump(agg_data, f, indent=2)

    print(f"\nDone. Enriched {sum(1 for c in contacts if 'llm' in c)}/{len(contacts)} contacts.", file=sys.stderr)
    print(f"Written: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    enrich()
