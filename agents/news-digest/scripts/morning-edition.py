#!/usr/bin/env python3
"""morning-edition.py — News digest LLM annotation + structuring step.

Phase 3b + Phase 3b-fix architecture. Pipeline:

  1. Read cache/ranked-<today>.json produced by fetch-and-rank.py.
  2. Python-side select_items() chooses the top N articles with
     deterministic LinkedIn slot reservation — so LinkedIn signal
     survives even when news items out-rank it.
  3. Call agents.shared.llm.infer(json_mode=True) asking the LLM to
     annotate each selected article (keyed by id) with a category
     and an extended_headline. The LLM does NOT do selection or
     ordering — Python owns the final list.
  4. Parse the wrapper {"items": [...]} the Responses API returns,
     merge each annotation back onto the Python-owned selection
     (with deterministic fallbacks when the LLM drops items).
  5. Atomic-write cache/morning-items.json for morning-fleet-deliver
     AND cache/item-map-<today>.json for engagement-poller lookup.

Why this shape:

The first Phase 3b day-in-the-life surfaced two LLM-selection bugs:
the model disregarded the "top 15-20" soft constraint (returned 30
items) and dropped every LinkedIn item from 7 ranked down to 0
selected. Splitting selection (deterministic Python) from
annotation (LLM) makes both failures impossible by construction.

The item-map write was also missing — engagement-poller needs
cache/item-map-<today>.json to resolve a like:N callback back to
an article, and the previous architecture left that file empty.
Every like for 2026-04-14 was silently dropped until this script
started writing it.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# --- shared library sys.path shim ---
# Find the first ancestor containing agents/shared/ and prepend it to
# sys.path so `from agents.shared import X` resolves in both the local
# repo layout and the deployed <workspace>/agents/shared/ layout.
# See agents/shared/deploy.py::sync_shared_library.
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared import llm


WORKSPACE = Path(os.path.expanduser("~/.clawford/news-digest-workspace"))
CACHE_DIR = WORKSPACE / "cache"

# Selection tuning. 18 total items with 4 LinkedIn reserved means the
# digest is ~14 news + ~4 LinkedIn on a normal day, matching the
# Phase-3a manifest target of "top 15-20" without leaving LinkedIn
# signal to the LLM's judgment.
TARGET_TOTAL = 18
LINKEDIN_RESERVED = 4

# Generous timeout — the annotation prompt is smaller than the
# pre-fix compose prompt (headlines only, no article bodies) but
# 18 items × ~200 tokens of output = ~3.5k tokens, and codex
# backend latency can spike on the first cold call of the day.
LLM_TIMEOUT_S = 120

# Category labels the LLM must choose from. The fleet-delivery script
# doesn't inspect the exact string — these are purely for human
# readability in the Telegram message — but the fallback classifier
# below matches these labels so the prompt and the Python fallback
# stay in lockstep.
CATEGORY_LABELS = (
    "🤖 AI & Tech",
    "💰 Economics",
    "🌍 World",
    "🏛️ US Policy",
    "🔗 LinkedIn",
    "📋 Also Noted",
    # LinkedIn notifications and DMs render as their own sections at
    # the END of the digest so they don't get lost among news items.
    "🔔 LinkedIn Notifications",
    "💬 LinkedIn Messages",
)

_CATEGORY_ORDER = {label: i for i, label in enumerate(CATEGORY_LABELS)}


PROMPT_TEMPLATE = """\
Annotate each of the following articles for the morning news digest.

For each article, produce exactly two fields:
  - `category`: one of these labels (with the leading emoji):
        🤖 AI & Tech       — AI, ML, chips, semiconductors, startups building software/hardware
        💰 Economics       — markets, inflation, fed, rates, earnings, M&A, business cycles
        🌍 World           — international politics, diplomacy, foreign policy, wars, trade
        🏛️ US Policy       — Congress, SCOTUS, White House, regulation, domestic legislation
        🔗 LinkedIn        — anything with source=linkedin (always goes here)
        📋 Also Noted      — FALLBACK ONLY: use this when no other label fits

  - `extended_headline`: a one-sentence rewritten headline.
     - For NEWS items (source != linkedin): explain WHY the item
       matters — context, stakes, who it affects. Concrete and
       specific; no filler like "could be important".
     - For LINKEDIN items (source = linkedin): LEAD with the author
       and their credibility, then what they actually said. Shape:
       "<Author>, <short role/company descriptor>: <what they posted>."
       Use the provided `author` and `author_headline` fields — the
       headline is the author's LinkedIn tagline and gives the
       credibility signal. Drop job titles that don't matter, keep
       company if it anchors authority. NEVER write "A LinkedIn post
       argues…", "A LinkedIn update says…", or any generic platform
       framing — always name the person. If author_headline is
       empty, just use the author name. Quote or paraphrase the
       substance; don't generalize into platitudes.

LinkedIn items (source=linkedin) always go in the 🔗 LinkedIn category.

Categorization rules:
  - Assign 📋 Also Noted ONLY when the item genuinely doesn't fit any
    of the five topic-specific labels. It is a fallback, not a default.
    If you're tempted to use Also Noted because categorization is
    ambiguous, pick the closest topic-specific bucket instead.
  - Aim for a spread across the first five categories rather than
    clustering everything into one. A healthy digest has items in
    at least 3 different topic-specific buckets.
  - A business/markets story about AI goes in 💰 Economics only if
    the story is primarily about money; if it's primarily about the
    tech, it goes in 🤖 AI & Tech.

Return a JSON object with a single top-level key `items` whose value
is an array of objects. Each object MUST include the original `id`
string so Python can match annotations back to the source article.
Do not reorder, drop, or add articles — return exactly the same set
of ids, with annotations:

{
  "items": [
    {"id": "<original id>", "category": "<label>", "extended_headline": "<sentence>"}
  ]
}

Articles to annotate:
{articles_json}
"""


# ─── load_ranked_articles ────────────────────────────────────────────


def load_ranked_articles() -> tuple[list[dict], str]:
    """Read cache/ranked-<today>.json.

    Returns (articles, date_str). Raises FileNotFoundError if the
    ranked file is missing and RuntimeError if it's empty.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ranked_file = CACHE_DIR / f"ranked-{today}.json"
    if not ranked_file.exists():
        raise FileNotFoundError(f"ranked file not found: {ranked_file}")
    with open(ranked_file, encoding="utf-8") as f:
        data = json.load(f)
    articles = data.get("articles", [])
    if not articles:
        raise RuntimeError(f"ranked file has no articles: {ranked_file}")
    return articles, today


# ─── select_items ───────────────────────────────────────────────────


# LinkedIn notification titles/summaries matching any of these substrings
# are routine platform-activity pings (profile views, reactions, endorsements,
# birthdays/anniversaries, follow-announcements) with no news content.
# They bypass the LLM categorization pass on the notifications path, so
# without this filter they'd land in the morning digest unfiltered — exactly
# how "Omar Shahine viewed your profile" reached the operator on 2026-04-18.
_LOW_SIGNAL_NOTIFICATION_PATTERNS: tuple[str, ...] = (
    "viewed your profile",
    "profile view",
    "reacted to",
    "endorsed you",
    "work anniversary",
    "is now following",
    "birthday",
    "celebrate",
    "suggested for you",
    "people you may know",
    "take a quiz",
)


def _filter_low_signal_notifications(notifs: list[dict]) -> list[dict]:
    """Drop LinkedIn notifications whose title/summary is routine
    platform-activity noise. See _LOW_SIGNAL_NOTIFICATION_PATTERNS.

    Items with ``_exempt_low_signal=True`` pass through unconditionally.
    This flag is set by fetch-and-rank's `_build_profile_view_summary`
    on the synthesized "Profile visitors — last 24h (N)" rollup, whose
    summary lists viewer names alongside the literal text "viewed your
    profile" — that content would otherwise trip the substring filter
    and drop the very rollup the filter was meant to promote."""
    kept: list[dict] = []
    for n in notifs:
        if n.get("_exempt_low_signal"):
            kept.append(n)
            continue
        blob = f"{n.get('title', '')}\n{n.get('summary', '')}".lower()
        if any(pat in blob for pat in _LOW_SIGNAL_NOTIFICATION_PATTERNS):
            continue
        kept.append(n)
    return kept


def select_items(
    articles: list[dict],
    *,
    target_total: int = TARGET_TOTAL,
    linkedin_reserved: int = LINKEDIN_RESERVED,
) -> list[dict]:
    """Pick the digest's articles from the ranked feed.

    Deterministic selection:
      - Top-ranked non-LinkedIn (news) + top LinkedIn feed posts
        fill target_total slots (LinkedIn feed gets linkedin_reserved).
      - LinkedIn NOTIFICATIONS and MESSAGES are appended OUTSIDE
        target_total — every notif/msg scraped makes it into the
        digest so the operator sees them. These render as their own
        sections at the end of the digest.

    Assigns sequential `num` (1..N) after final ordering so
    morning-fleet-deliver can label each Telegram message with a
    stable number that matches /like N / callback_data=like:N.
    """
    def _rank(a: dict) -> float:
        r = a.get("rank", 0)
        try:
            return float(r)
        except (TypeError, ValueError):
            return 0.0

    notifications = _filter_low_signal_notifications([
        a for a in articles
        if a.get("source") == "linkedin" and a.get("_is_notification")
    ])
    messages = [
        a for a in articles
        if a.get("source") == "linkedin" and a.get("_is_message")
    ]

    linkedin_feed = sorted(
        (a for a in articles
         if a.get("source") == "linkedin"
         and not a.get("_is_notification")
         and not a.get("_is_message")),
        key=_rank,
        reverse=True,
    )
    non_linkedin = sorted(
        (a for a in articles if a.get("source") != "linkedin"),
        key=_rank,
        reverse=True,
    )

    li_slot_count = min(len(linkedin_feed), linkedin_reserved)
    non_li_slot_count = target_total - li_slot_count
    if non_li_slot_count < 0:
        non_li_slot_count = 0

    main = linkedin_feed[:li_slot_count] + non_linkedin[:non_li_slot_count]
    # Re-sort the combined main set by rank so delivery order reflects
    # importance rather than source buckets.
    main.sort(key=_rank, reverse=True)
    main = main[:target_total]

    # Append notifications and messages AFTER the main pool. Sort
    # each group by rank-desc so the most-engaging items render first
    # within their section.
    notifications.sort(key=_rank, reverse=True)
    messages.sort(key=_rank, reverse=True)

    selected = main + notifications + messages

    # Assign num 1..N
    for i, item in enumerate(selected, 1):
        item["num"] = i

    return selected


# ─── build_prompt ───────────────────────────────────────────────────


def build_prompt(selected: list[dict]) -> str:
    """Format the prompt template with a compact JSON list of
    articles — just enough fields for the LLM to reason about
    category + headline without the full summary text.

    LinkedIn notifications and messages are excluded — they render
    deterministically (see _render_notification_headline) and never
    need LLM rewording, which used to produce the generic "A LinkedIn
    notification showing…" framing the operator flagged 2026-04-20.
    """
    compact: list[dict] = []
    for item in selected:
        if item.get("_is_notification") or item.get("_is_message"):
            continue
        entry = {
            "id": item["id"],
            "title": item.get("title", ""),
            "source_label": item.get("source_label", ""),
            "source": item.get("source", ""),
            "topics": item.get("topics", []),
            "summary": (item.get("summary") or "")[:400],
        }
        # LinkedIn feed items ship with author + headline so the LLM
        # can write "Dario Amodei, Anthropic CEO: …" instead of a
        # generic "A LinkedIn post argues…" with no credibility signal.
        if item.get("source") == "linkedin":
            entry["author"] = item.get("author", "")
            entry["author_headline"] = item.get("author_headline", "")
        compact.append(entry)
    articles_json = json.dumps(compact, indent=2, ensure_ascii=False)
    return PROMPT_TEMPLATE.replace("{articles_json}", articles_json)


# ─── parse_response ─────────────────────────────────────────────────


def parse_response(text: str) -> list[dict]:
    """Parse the LLM's annotation response.

    Expects {"items": [{id, category, extended_headline}, ...]} per
    the prompt contract. Strips markdown fences defensively because
    json_mode doesn't always prevent wrapping.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        body = text[3:]
        if body.lower().startswith("json"):
            body = body[4:]
        body = body.strip()
        if body.endswith("```"):
            body = body[:-3].strip()
        text = body

    data = json.loads(text)

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")

    if "items" not in data:
        raise ValueError(
            f"expected \"items\" key in response object, got {sorted(data.keys())}"
        )

    items = data["items"]
    if not isinstance(items, list):
        raise ValueError(f"expected items to be a list, got {type(items).__name__}")
    return items


# ─── merge_annotations ──────────────────────────────────────────────


def _fallback_category(item: dict) -> str:
    """Pick a category from the item's topics when the LLM didn't
    annotate it. Keeps the final output complete even if the LLM
    drops an id from its response.
    """
    if item.get("source") == "linkedin":
        if item.get("_is_notification"):
            return "🔔 LinkedIn Notifications"
        if item.get("_is_message"):
            return "💬 LinkedIn Messages"
        return "🔗 LinkedIn"
    topics = set(item.get("topics", []))
    if topics & {"ai", "tech", "artificial_intelligence", "chatbots", "generative_ai"}:
        return "🤖 AI & Tech"
    if topics & {"economics", "markets", "business", "stocks", "startups"}:
        return "💰 Economics"
    if topics & {"international_politics", "world", "geopolitics"}:
        return "🌍 World"
    if topics & {"us_domestic_policy", "regulation"}:
        return "🏛️ US Policy"
    return "📋 Also Noted"


def _render_notification_headline(item: dict) -> str:
    """Deterministic render for a LinkedIn notification.

    the operator's feedback 2026-04-20: notifications were being wrapped with
    "A LinkedIn notification showing a recruiter viewed the profile
    signals potential hiring interest…" — LLM generalization that
    buries the actual who/what. Instead, show the raw notification
    text (already contains name + action) with a time-ago suffix, no
    LLM pass.

    For rollup-shape items (the profile-view rollup has a short title
    like '👁️ Profile visitors — last 24h (3)' AND a multi-line
    bulleted summary), prefix the title so the rendered block shows
    both the header and the per-viewer lines. Single-line notifs keep
    the summary-only shape.
    """
    title = (item.get("title") or "").strip()
    summary = (item.get("summary") or "").strip()
    if title and "\n" in summary:
        text = f"{title}\n{summary}"
    else:
        text = summary or title
    # LinkedIn notification text often trails a "See more" or
    # "View all" CTA. Strip trailing CTAs so the rendered line is
    # just the substantive bit.
    for tail in (" See all views", " View all", " See more", " See all"):
        if text.endswith(tail):
            text = text[: -len(tail)].rstrip(" .")
    time_ago = (item.get("time_ago") or "").strip()
    if time_ago:
        return f"{text} ({time_ago})"
    return text


def merge_annotations(
    selected: list[dict],
    annotations: list[dict],
) -> list[dict]:
    """Attach `category` and `extended_headline` from the LLM
    annotations to each Python-owned selected item.

    The LLM might drop items, echo extra fields, or miss ids — this
    function defends by owning the final shape. Only `category` and
    `extended_headline` are taken from the annotations; everything
    else comes from the `selected` list.

    LinkedIn notifications skip the LLM entirely: their headline is
    rendered deterministically from the scraped text + time_ago, so
    the digest reads "Omar Shahine reacted to your post (2h)" instead
    of a generic LLM paraphrase.
    """
    by_id: dict[str, dict] = {}
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        id_ = ann.get("id")
        if id_:
            by_id[id_] = ann

    merged: list[dict] = []
    for item in selected:
        ann = by_id.get(item["id"], {})
        # LinkedIn notifications/messages get forced into their
        # distinct categories regardless of LLM output — the prompt
        # doesn't disambiguate these sub-types, so Python owns the
        # final section routing.
        is_notification = (
            item.get("source") == "linkedin" and item.get("_is_notification")
        )
        is_message = (
            item.get("source") == "linkedin" and item.get("_is_message")
        )
        if is_notification:
            category = "🔔 LinkedIn Notifications"
            headline = _render_notification_headline(item)
        elif is_message:
            category = "💬 LinkedIn Messages"
            headline = ann.get("extended_headline") or item.get("title", "")
        else:
            category = ann.get("category") or _fallback_category(item)
            headline = ann.get("extended_headline") or item.get("title", "")
        merged.append({
            "num": item["num"],
            "id": item["id"],
            "category": category,
            "extended_headline": headline,
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "source_label": item.get("source_label", ""),
            "source": item.get("source", ""),
            "topics": item.get("topics", []),
        })
    return merged


# ─── group_by_section ───────────────────────────────────────────────


def group_by_section(items: list[dict]) -> list[dict]:
    """Reorder merged items by section and reassign num 1..N.

    Section order follows CATEGORY_LABELS. Within each section the
    input order is preserved — since `merged` arrives rank-descending
    from select_items → merge_annotations, a stable sort on category
    gives rank-descending order inside each section. Unknown labels
    sort after Also Noted so the final list never drops an item.

    num is reassigned against the new display order so Telegram
    numbering, callback_data, and item-map-<date>.json all agree.
    Returns a new list; the input is not mutated.
    """
    def _section_key(item: dict) -> int:
        return _CATEGORY_ORDER.get(
            item.get("category", ""),
            len(CATEGORY_LABELS),
        )

    grouped = sorted(items, key=_section_key)
    out: list[dict] = []
    for i, item in enumerate(grouped, 1):
        new = dict(item)
        new["num"] = i
        out.append(new)
    return out


# ─── write_morning_items ────────────────────────────────────────────


def write_morning_items(items: list[dict], date_str: str) -> Path:
    """Atomic write of the structured items list for
    morning-fleet-deliver.py to read at 12:00 UTC."""
    path = CACHE_DIR / "morning-items.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


# ─── write_item_map ─────────────────────────────────────────────────


def write_item_map(items: list[dict], date_str: str) -> Path:
    """Atomic write of cache/item-map-<date>.json for engagement-poller.

    engagement-poller reads the short-form source (`nyt`, `linkedin`)
    and the topic list when logging a thumbs_up — so the map's value
    shape must match `.get('id')`, `.get('title')`, `.get('topics', [])`,
    `.get('source', '')`. Includes `link` for future telegram command
    handlers that want to echo the URL.
    """
    path = CACHE_DIR / f"item-map-{date_str}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, dict] = {}
    for item in items:
        mapping[str(item["num"])] = {
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "topics": item.get("topics", []),
            "source": item.get("source", ""),
            "link": item.get("url", ""),
        }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


# ─── run() orchestrator ─────────────────────────────────────────────


def run() -> dict:
    articles, date_str = load_ranked_articles()
    selected = select_items(articles)
    prompt = build_prompt(selected)
    result = llm.infer(prompt, json_mode=True, timeout=LLM_TIMEOUT_S)
    if not result.ok:
        raise RuntimeError(f"LLM infer failed: {result.error}")
    annotations = parse_response(result.text or "")
    merged = merge_annotations(selected, annotations)
    if not merged:
        raise RuntimeError("merged items list is empty")
    sectioned = group_by_section(merged)
    items_path = write_morning_items(sectioned, date_str)
    map_path = write_item_map(sectioned, date_str)

    linkedin_count = sum(1 for m in merged if m.get("source") == "linkedin")
    return {
        "status": "ok",
        "date": date_str,
        "items_count": len(merged),
        "linkedin_count": linkedin_count,
        "items_path": str(items_path),
        "item_map_path": str(map_path),
        "ranked_article_count": len(articles),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "model": result.model,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"🐛 morning-edition failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
