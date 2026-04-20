#!/usr/bin/env python3
"""
llm-enrich.py — LLM enrichment for merged contacts (Haiku + thinking).

Reads cache/contacts-merged.json, classifies each contact, extracts
durable facts, and writes cache/contacts-enriched.json.

Family members (see family_map.py) bypass LLM entirely — hardcoded
classification is more reliable than model inference.

Holiday card list (holiday_card.py) acts as a hard "not an acquaintance"
signal injected into the prompt; post-response validation upgrades any
holiday-card contact still classified as acquaintance to friend.

Usage:
  python3 llm-enrich.py --dry-run              # Build prompts, no API calls
  python3 llm-enrich.py --dry-run --slug alex-rivera  # One contact only
  python3 llm-enrich.py --execute              # Call Haiku (requires key)
  python3 llm-enrich.py --execute --resume     # Continue from checkpoint
  python3 llm-enrich.py --execute --limit 10   # First N contacts

Cost: ~$1.30 for 472 contacts at 5/50 sample bounds + 1024 thinking.
"""

import io
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import CACHE_DIR, load_checkpoint, load_config, save_checkpoint
from family_map import lookup_family, lookup_special, lookup_circle_override
from holiday_card import load_holiday_card, is_on_list

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ── Sample selection ──────────────────────────────────────────

SAMPLE_FLOOR = 5
SAMPLE_CEILING = 50


def sample_budget(score):
    """Per-channel sample cap. Alex (~35K) gets 50, tail gets 5."""
    return max(SAMPLE_FLOOR, min(SAMPLE_CEILING, int(math.sqrt(max(score, 0))) + 2))


def _take_recent(items, n, newest_first=True):
    """Return up to n items, most-recent first.

    If newest_first is True, the source list is already in newest→oldest
    order (Gmail/Calendar API default), so slice [:n].
    Otherwise the source is oldest→newest (phone exports), slice [-n:]
    and reverse so the output is newest-first for the LLM.
    """
    if not items:
        return []
    if newest_first:
        return list(items[:n])
    return list(reversed(items[-n:]))


def select_samples(contact):
    """Pick samples per channel, bounded by score."""
    n = sample_budget(contact.get("score", 0))

    subjects = _take_recent(contact.get("gmail_subjects", []), n, newest_first=True)
    meetings = _take_recent(contact.get("meeting_titles", []), n, newest_first=True)
    body_excerpts = _take_recent(contact.get("gmail_body_excerpts", []), n, newest_first=True)

    wa = _take_recent(contact.get("whatsapp_samples", []), n, newest_first=False)
    sms = _take_recent(contact.get("sms_samples", []), n, newest_first=False)

    return {
        "n": n,
        "subjects": subjects,
        "meetings": meetings,
        "body_excerpts": body_excerpts,
        "whatsapp": wa,
        "sms": sms,
    }


# ── Prompt ────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are analyzing interaction data between Sam Smith and one of his contacts. Your job: classify the relationship, extract durable facts about the person, and compose a one-sentence note to prime Sam before his next interaction.

Respond with ONLY a JSON object, no markdown fencing, no preamble."""


CLASSIFICATION_GUIDANCE = """═══════════════════════════════════════════════════════════════

CLASSIFICATION GUIDANCE

relationship_type — pick exactly ONE:
  family       Spouse, kids, siblings, parents, in-laws, cousins, aunts/uncles.
               Signals: shared surname, family events/holidays, terms of
               endearment, kids' names in conversation, Smith/Rivera/Lee
               surnames.
  friend       Personal relationship — social hangouts, life updates, casual
               chat, shared interests. High message volume ALONE is NOT
               sufficient; many SMS-heavy contacts are former colleagues
               or family.
  colleague    Current or former work relationship. Corporate or .edu
               domains, meeting-heavy, project language.
  client       Someone paying Sam for services (or vice versa).
               Engagement/proposal/contract/invoice language.
  recruiter    Hiring/opportunity/role outreach; usually few meetings.
  vendor       Service provider — insurance, accountant, doctor, contractor.
               One-sided utility relationship.
  acquaintance Loose connection, lapsed, or unclear purpose.

CRITICAL: Volume does NOT imply closeness or type. A former colleague
with 2000 old SMS messages from 2015 is NOT a current close friend.
Weight recent behavior, language tone, and shared topics more than raw
counts. A parent with only 50 SMS messages IS family.

key_facts — 3 to 5 items. Each must be DURABLE (still true in 2 years)
and SPECIFIC. Good examples:
  • "Sam's wife; mother of Avery and Jordan"
  • "VP of Engineering at Stripe; based in Sunnyvale, CA"
  • "Former Example Corp Data Science colleague (2015-2020)"
  • "Has two young kids, recently moved to NYC"
  • "Recovering from breast cancer diagnosed 2024"
AVOID: commitments ("said she'd send the deck"), plans ("meeting
Tuesday"), project details ("working on Q4 roadmap"), temporary status
("busy this week").

topics — 2 to 5 short phrases for recurring discussion themes.

context_notes — ONE sentence, actionable for Sam's next interaction.
Example: "Former Example Corp DS colleague now at Stripe; ask about her
recent NYC move."

tone — pick ONE: casual, warm, professional, formal.

circle_suggestion — pick ONE:
  family-inner          spouse, kids, siblings
  family-extended       parents, cousins, aunts/uncles, in-laws
  friends-close         active personal friendships
  friends-acquaintance  lapsed or loose friendships
  professional-inner    current close work collaborators (recent meetings)
  professional-outer    former colleagues, loose work ties
  holiday-card          once-a-year contacts

═══════════════════════════════════════════════════════════════

OUTPUT (JSON only, no markdown):

{
  "relationship_type": "...",
  "key_facts": ["...", "...", "..."],
  "topics": ["...", "..."],
  "context_notes": "...",
  "tone": "...",
  "circle_suggestion": "..."
}"""


def _fmt_samples(items, render_msg=None):
    """Format a list of samples as numbered bullets."""
    if not items:
        return "  (none)"
    out = []
    for i, item in enumerate(items, 1):
        if render_msg:
            s = render_msg(item)
        elif isinstance(item, dict):
            direction = item.get("direction", "?")
            text = (item.get("text") or item.get("message") or "").strip()
            s = f"({direction}) {text}"
        else:
            s = str(item).strip()
        s = s.replace("\n", " ")[:280]
        out.append(f"  [{i}] {s}")
    return "\n".join(out)


def _fmt_bullets(items):
    if not items:
        return "  (none)"
    return "\n".join(f"  • {str(s).strip()[:200]}" for s in items if s)


def _days_since(last_interaction):
    if not last_interaction:
        return "unknown"
    try:
        d = datetime.strptime(str(last_interaction)[:10], "%Y-%m-%d").date()
        return (datetime.now(timezone.utc).date() - d).days
    except ValueError:
        return "unknown"


def build_prompt(contact, samples, known_signals=None):
    """Build the full user-turn prompt for Haiku."""
    sig = contact.get("signature") or {}
    sig_str = json.dumps(sig) if sig else "none"

    known_block = ""
    if known_signals:
        known_block = (
            "KNOWN SIGNALS (hard facts — do not contradict)\n"
            + "\n".join(f"  - {s}" for s in known_signals)
            + "\n\n"
        )

    return f"""{known_block}CONTACT
Name:           {contact.get("name", "")}
Email:          {contact.get("email", "") or "—"}
Phone:          {contact.get("phone", "") or "—"}
Platforms:      {", ".join(contact.get("platforms", []))}
First contact:  {contact.get("first_interaction", "unknown")}
Last contact:   {contact.get("last_interaction", "unknown")} ({_days_since(contact.get("last_interaction"))} days ago)

VOLUME
Emails sent by Sam:   {contact.get("gmail_sent", 0)}
Emails received:        {contact.get("gmail_received", 0)}
Calendar meetings:      {contact.get("meeting_count", 0)}
Krisp-recorded calls:   {contact.get("krisp_meetings", 0)}
WhatsApp messages:      {contact.get("whatsapp_messages", 0)}
SMS/Messages:           {contact.get("sms_messages", 0)}

RECENT CONTEXT (up to {samples["n"]} most-recent samples per channel)

Email subjects:
{_fmt_bullets(samples["subjects"])}

Meeting titles:
{_fmt_bullets(samples["meetings"])}

Email body excerpts:
{_fmt_samples(samples["body_excerpts"], render_msg=lambda x: str(x))}

WhatsApp messages:
{_fmt_samples(samples["whatsapp"])}

SMS messages:
{_fmt_samples(samples["sms"])}

Parsed email signature (may be noisy — ignore if it looks like quoted text, a mailing address, or body prose):
  {sig_str}

{CLASSIFICATION_GUIDANCE}"""


# ── Pre-seed + validation ────────────────────────────────────

def enrich_from_family(family_info):
    """Build a full enrichment from a family_map entry (no LLM)."""
    return {
        "relationship_type": "family",
        "key_facts": list(family_info.get("facts", [])),
        "topics": [],
        "context_notes": f"{family_info['name']} is Sam's {family_info['relation']}.",
        "tone": "warm",
        "circle_suggestion": family_info["circle"],
        "_source": "family_map",
    }


def enrich_from_special(special_info):
    """Build a full enrichment from a SPECIAL_CONTACTS entry (no LLM)."""
    return {
        "relationship_type": special_info.get("relationship_type", "vendor"),
        "key_facts": list(special_info.get("facts", [])),
        "topics": [],
        "context_notes": special_info.get(
            "context_notes",
            f"{special_info['name']} — {special_info.get('relation','contact')}",
        ),
        "tone": special_info.get("tone", "professional"),
        "circle_suggestion": special_info["circle"],
        "_source": "special_contacts",
    }


def build_known_signals(contact, on_holiday_list):
    signals = []
    if on_holiday_list:
        signals.append(
            "Sam sends this person a holiday card — they are a personally "
            "valued contact. relationship_type MUST be one of: family, friend, "
            "colleague (if still-active work relationship). Do NOT return "
            "acquaintance. A former colleague who stays in touch is a friend."
        )
    return signals


VALID_RELATIONSHIP_TYPES = {
    "family", "friend", "colleague", "client", "recruiter", "vendor", "acquaintance"
}
VALID_CIRCLES = {
    "family-inner", "family-extended",
    "friends-close", "friends-acquaintance",
    "professional-inner", "professional-outer",
    "holiday-card",
}
VALID_TONES = {"casual", "warm", "professional", "formal"}


def validate_and_fix(enrichment, contact, on_holiday_list):
    """Post-LLM sanity checks: enum validation + holiday card override."""
    if not isinstance(enrichment, dict):
        return enrichment

    notes = []
    rt = (enrichment.get("relationship_type") or "").strip().lower()

    # Coerce freeform relationship_type to enum
    if rt not in VALID_RELATIONSHIP_TYPES:
        if "friend" in rt or "colleague turned" in rt:
            enrichment["relationship_type"] = "friend"
        elif "colleague" in rt or "coworker" in rt:
            enrichment["relationship_type"] = "colleague"
        elif "family" in rt or "relative" in rt or "cousin" in rt or "sibling" in rt:
            enrichment["relationship_type"] = "family"
        elif "vendor" in rt or "service" in rt:
            enrichment["relationship_type"] = "vendor"
        elif "client" in rt:
            enrichment["relationship_type"] = "client"
        elif "recruiter" in rt or "hiring" in rt:
            enrichment["relationship_type"] = "recruiter"
        else:
            enrichment["relationship_type"] = "acquaintance"
        notes.append(f"coerced_type:{rt}->{enrichment['relationship_type']}")

    # Holiday-card members must never be acquaintance
    if on_holiday_list and enrichment["relationship_type"] == "acquaintance":
        enrichment["relationship_type"] = "friend"
        notes.append("holiday-card: acquaintance->friend")

    # Coerce circle_suggestion to enum
    circle = (enrichment.get("circle_suggestion") or "").strip().lower()
    if circle not in VALID_CIRCLES:
        # Try mapping common alternatives
        mapping = {
            "friends": "friends-close",
            "close-friends": "friends-close",
            "professional": "professional-outer",
            "work": "professional-outer",
            "family": "family-extended",
            "holiday": "holiday-card",
        }
        enrichment["circle_suggestion"] = mapping.get(circle, "professional-outer")
        notes.append(f"coerced_circle:{circle}->{enrichment['circle_suggestion']}")

    # Coerce tone
    tone = (enrichment.get("tone") or "").strip().lower()
    if tone not in VALID_TONES:
        enrichment["tone"] = "professional"

    if notes:
        enrichment["_validation"] = "; ".join(notes)
    return enrichment


# ── LLM call (Haiku + thinking) ──────────────────────────────

import re as _re
_JSON_OBJECT_RE = _re.compile(r"\{[\s\S]*\}", _re.MULTILINE)


def _extract_json(text):
    """Best-effort JSON extraction from LLM output."""
    if not text:
        raise ValueError("empty response")
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: greedy outermost { ... } match
    m = _JSON_OBJECT_RE.search(text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"no JSON object found in: {text[:200]!r}")


def call_haiku(system, user, thinking_budget=1024, max_output=800):
    """Call Claude Haiku 4.5 with extended thinking.

    Requires ANTHROPIC_API_KEY in env. One-time pipeline use only.
    """
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=thinking_budget + max_output,
        thinking={"type": "enabled", "budget_tokens": thinking_budget},
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text = block.text.strip()
            break

    result = _extract_json(text)

    usage = getattr(response, "usage", None)
    usage_dict = {}
    if usage:
        usage_dict = {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
        }

    return result, usage_dict


# ── Main ──────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    execute = "--execute" in args
    resume = "--resume" in args

    limit = None
    slug_filter = None
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        if a == "--slug" and i + 1 < len(args):
            slug_filter = args[i + 1]

    if not dry_run and not execute:
        print("Must specify --dry-run or --execute", file=sys.stderr)
        sys.exit(1)
    if execute and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: --execute requires ANTHROPIC_API_KEY", file=sys.stderr)
        sys.exit(1)

    # Load inputs
    merged_path = CACHE_DIR / "contacts-merged.json"
    if not merged_path.exists():
        print("ERROR: Run contact-aggregator.py first.", file=sys.stderr)
        sys.exit(1)
    with open(merged_path, encoding="utf-8") as f:
        data = json.load(f)

    contacts = data.get("contacts", [])
    email_set, name_set = load_holiday_card()
    print(f"Holiday card list: {len(email_set)} emails, {len(name_set)} names", file=sys.stderr)

    # Filter
    if slug_filter:
        contacts = [c for c in contacts if c.get("slug") == slug_filter]
    if limit:
        contacts = contacts[:limit]

    # Resume support
    enriched_map = {}
    if resume and execute:
        ck = load_checkpoint("llm-enrich-v2")
        if ck:
            enriched_map = ck.get("enriched", {})
            print(f"Resuming: {len(enriched_map)} already enriched", file=sys.stderr)

    total_input = 0
    total_output = 0
    family_skipped = 0
    llm_called = 0
    errors = 0

    print(f"Processing {len(contacts)} contacts...", file=sys.stderr, flush=True)

    # First pass: resolve family pre-seeds and build work queue
    to_llm = []
    for i, contact in enumerate(contacts):
        email = contact.get("email", "")
        slug = contact.get("slug", "")
        key = email or slug or contact.get("name", "")
        if key in enriched_map:
            continue
        family = lookup_family(contact)
        if family:
            enriched_map[key] = enrich_from_family(family)
            family_skipped += 1
            if dry_run:
                print(f"  [{i+1}/{len(contacts)}] FAMILY: {family['name']} → {family['relation']} ({family['circle']})", file=sys.stderr, flush=True)
            continue
        on_holiday = is_on_list(contact, email_set, name_set)
        signals = build_known_signals(contact, on_holiday)
        samples = select_samples(contact)
        prompt = build_prompt(contact, samples, signals)
        to_llm.append((i, key, contact, on_holiday, prompt))

    if dry_run:
        for idx, key, contact, on_holiday, prompt in to_llm:
            print(f"\n{'='*72}", file=sys.stderr, flush=True)
            print(f"[{idx+1}/{len(contacts)}] {contact.get('name','?')}  score={contact.get('score',0):.0f}  holiday_card={on_holiday}", file=sys.stderr, flush=True)
            print(f"{'='*72}", file=sys.stderr, flush=True)
            print(f"SYSTEM:\n{SYSTEM_PROMPT}\n", file=sys.stderr, flush=True)
            print(f"USER:\n{prompt}", file=sys.stderr, flush=True)
    elif to_llm:
        # Parallel LLM calls via thread pool
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading
        lock = threading.Lock()
        done = [0]

        def worker(item):
            idx, key, contact, on_holiday, prompt = item
            try:
                result, usage = call_haiku(SYSTEM_PROMPT, prompt, thinking_budget=1024)
                result = validate_and_fix(result, contact, on_holiday)
                return (idx, key, contact, on_holiday, result, usage, None)
            except Exception as e:
                return (idx, key, contact, on_holiday, None, {}, e)

        print(f"Dispatching {len(to_llm)} LLM calls with 10 workers...", file=sys.stderr, flush=True)
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(worker, item) for item in to_llm]
            for fut in as_completed(futures):
                idx, key, contact, on_holiday, result, usage, err = fut.result()
                with lock:
                    done[0] += 1
                    d = done[0]
                    if err:
                        errors += 1
                        enriched_map[key] = {"error": str(err)}
                        print(f"  [{d}/{len(to_llm)}] {contact.get('name','?')[:30]}: ERROR {err}", file=sys.stderr, flush=True)
                    else:
                        enriched_map[key] = result
                        llm_called += 1
                        total_input += usage.get("input_tokens", 0)
                        total_output += usage.get("output_tokens", 0)
                        tag = "HOL" if on_holiday else "   "
                        print(
                            f"  [{d}/{len(to_llm)}] {tag} {contact.get('name','?')[:28]:<28s}"
                            f" → {result.get('relationship_type','?'):<12s}"
                            f" {result.get('circle_suggestion','?')}"
                            f" (in={usage.get('input_tokens',0)} out={usage.get('output_tokens',0)})",
                            file=sys.stderr, flush=True,
                        )
                    # Checkpoint every 25
                    if d % 25 == 0:
                        save_checkpoint("llm-enrich-v2", {"enriched": enriched_map})

    # Final checkpoint
    if not dry_run:
        save_checkpoint("llm-enrich-v2", {"enriched": enriched_map})

    # Merge enrichments back into contacts
    for contact in contacts:
        key = contact.get("email") or contact.get("slug") or contact.get("name", "")
        llm = enriched_map.get(key)
        if llm and "error" not in llm:
            contact["llm"] = llm

    if not dry_run:
        out_path = CACHE_DIR / "contacts-enriched.json"
        data["contacts"] = contacts
        data["enriched_at"] = datetime.now(timezone.utc).isoformat()
        data["enrichment_method"] = "haiku-4.5-thinking-1024"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"\nWritten: {out_path}", file=sys.stderr)

    # Summary
    print(f"\n── Summary ──", file=sys.stderr)
    print(f"  Family pre-seeded: {family_skipped}", file=sys.stderr)
    print(f"  LLM enriched:      {llm_called}", file=sys.stderr)
    print(f"  Errors:            {errors}", file=sys.stderr)
    if total_input or total_output:
        # Haiku 4.5: $1/M input, $5/M output (thinking billed as output)
        cost_in = total_input * 1.0 / 1_000_000
        cost_out = total_output * 5.0 / 1_000_000
        print(f"  Input tokens:  {total_input:>9,d}  (${cost_in:.4f})", file=sys.stderr)
        print(f"  Output tokens: {total_output:>9,d}  (${cost_out:.4f})", file=sys.stderr)
        print(f"  Total cost:    ${cost_in + cost_out:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
