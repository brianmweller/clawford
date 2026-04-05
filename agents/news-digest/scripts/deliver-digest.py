#!/usr/bin/env python3
"""
deliver-digest.py — Send the morning digest as individual Telegram messages.

Reads the ranked article cache, formats each item, and sends via Telegram
Bot API directly (not through OpenClaw) so we get:
- One message per item (enables per-item reactions)
- No link previews (disable_web_page_preview=True)
- No mid-item splits (each message is short)
- Deduplication across runs (skips previously sent items)
- Saves item-number-to-article mapping for preference learning

Usage: python3 deliver-digest.py [--date YYYY-MM-DD]
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import urllib.request
import urllib.error

WORKSPACE = Path(os.path.expanduser("~/.openclaw/news-digest-workspace"))
CACHE_DIR = WORKSPACE / "cache"

TELEGRAM_BOT_TOKEN = os.environ.get("NEWSDIGEST_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Tracking params to strip
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "utm_reader", "utm_cid", "utm_pubreferrer",
    "ref", "reflink", "source", "via", "rcm",
    "oc", "ucbcb", "ceid", "gl", "hl",
    "mod", "cx_testId", "cx_testVariant",
    "smid", "smtyp", "smprod",
    "fbclid", "gclid", "msclkid", "dclid",
    "mc_cid", "mc_eid",
}


def clean_url(url):
    """Strip tracking parameters from URLs."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {k: v for k, v in params.items() if k.lower() not in TRACKING_PARAMS}
    clean_query = urlencode(cleaned, doseq=True)
    return urlunparse(parsed._replace(query=clean_query, fragment=""))


def clean_html_entities(text):
    """Strip common HTML entities."""
    return (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
            .replace("&quot;", '"'))


def send_telegram(text):
    """Send a message via Telegram Bot API with previews disabled."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[dry-run] {text[:100]}...", file=sys.stderr)
        return True

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Try plain text first (most reliable)
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
    })

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if not result.get("ok"):
            print(f"Telegram error: {result}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)
        return False


def get_source_name(source):
    """Map source ID to display name."""
    return {
        "nyt": "NYT",
        "wsj": "WSJ",
        "wapo": "WaPo",
        "slate": "Slate",
        "google_news": "Google News",
        "linkedin": "LinkedIn",
    }.get(source, source)


def load_sent_ids(date_str):
    """Load previously sent article IDs for deduplication across runs."""
    sent_file = CACHE_DIR / f"sent-{date_str}.json"
    if sent_file.exists():
        with open(sent_file) as f:
            return set(json.load(f).get("ids", []))
    return set()


def save_sent_ids(date_str, sent_ids):
    """Save the set of article IDs that have been sent today."""
    sent_file = CACHE_DIR / f"sent-{date_str}.json"
    with open(sent_file, "w") as f:
        json.dump({"ids": sorted(sent_ids), "updated_at": datetime.now(timezone.utc).isoformat()}, f)


def save_item_mapping(date_str, mapping):
    """Save item-number-to-article mapping for preference learning.

    When the user sends /like 3, the agent looks up item 3 in this mapping
    to find the article's topics and source for engagement logging.
    """
    mapping_file = CACHE_DIR / f"item-map-{date_str}.json"
    with open(mapping_file, "w") as f:
        json.dump(mapping, f, indent=2)


def main():
    # Determine date
    date_str = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "--date" else None
    if not date_str and len(sys.argv) > 2 and sys.argv[1] == "--date":
        date_str = sys.argv[2]
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Load ranked articles
    ranked_file = CACHE_DIR / f"ranked-{date_str}.json"
    if not ranked_file.exists():
        print(json.dumps({"status": "error", "message": f"No ranked file for {date_str}"}))
        sys.exit(1)

    with open(ranked_file) as f:
        data = json.load(f)

    articles = data.get("articles", [])
    errors = data.get("errors", [])

    if not articles:
        print(json.dumps({"status": "error", "message": "No articles to deliver"}))
        sys.exit(1)

    # Load previously sent IDs for deduplication
    previously_sent = load_sent_ids(date_str)
    is_refresh = len(previously_sent) > 0

    # Select top 20 items, filtering out already-sent ones if refreshing
    top_articles = []
    for article in articles:
        if len(top_articles) >= 20:
            break
        article_id = article.get("id", "")
        if is_refresh and article_id in previously_sent:
            continue
        top_articles.append(article)

    if not top_articles:
        if is_refresh:
            send_telegram("🐛 No new items since the last edition.")
            print(json.dumps({"status": "ok", "items_sent": 0, "message": "no new items"}))
            return
        else:
            print(json.dumps({"status": "error", "message": "No articles to deliver"}))
            sys.exit(1)

    # Group by category
    categories = {}
    for article in top_articles:
        cat = article.get("category", "📋 ALSO NOTED")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(article)

    # Desired section order
    section_order = [
        "🤖 AI & TECH",
        "💰 ECONOMICS",
        "🌍 WORLD",
        "🏛️ US POLICY",
        "🔗 LINKEDIN",
        "📋 ALSO NOTED",
    ]

    # Send header
    today_display = datetime.strptime(date_str, "%Y-%m-%d").strftime("%B %d, %Y")
    header = f"🐛📰 Morning Edition — {today_display}"
    if is_refresh:
        header += " (update)"
    send_telegram(header)
    time.sleep(0.5)

    item_num = 0
    sent_count = 0
    sources_seen = set()
    new_sent_ids = set(previously_sent)  # Start with existing, add new
    item_mapping = {}  # item_num -> article metadata for preference learning

    for section in section_order:
        if section not in categories:
            continue

        # Send section header
        send_telegram(f"{section}\n━━━━━━━━━━━━━━━")
        time.sleep(0.3)

        for article in categories[section]:
            item_num += 1
            title = clean_html_entities(article.get("title", "Untitled"))
            link = clean_url(article.get("link", ""))
            summary = clean_html_entities(article.get("summary", ""))
            source = article.get("source", "")
            sources_seen.add(source)
            article_id = article.get("id", "")

            # Build context line from summary, skip if it duplicates the title
            context = ""
            if summary:
                first_sentence = summary.split(".")[0] + "."
                if first_sentence.strip().lower() not in title.lower() and len(first_sentence) > 20:
                    context = first_sentence
                    if len(context) > 200:
                        context = context[:197] + "..."

            # Format message
            if context:
                msg = f"{item_num}. {title}\n{context}\n{link}"
            else:
                msg = f"{item_num}. {title}\n{link}"

            send_telegram(msg)
            sent_count += 1
            new_sent_ids.add(article_id)

            # Save mapping for preference learning
            item_mapping[str(item_num)] = {
                "id": article_id,
                "title": title,
                "topics": article.get("topics", []),
                "source": source,
                "link": link,
            }

            time.sleep(0.3)

    # LinkedIn notifications section
    linkedin_file = CACHE_DIR / f"linkedin-{date_str}.json"
    if linkedin_file.exists():
        with open(linkedin_file) as f:
            li_data = json.load(f)

        notifs = li_data.get("notifications", [])
        if notifs:
            send_telegram("🔔 LINKEDIN NOTIFICATIONS\n━━━━━━━━━━━━━━━")
            time.sleep(0.3)

            for notif in notifs:
                text = notif.get("text", "")
                time_ago = notif.get("time_ago", "")
                detail = notif.get("detail", "")

                if not text:
                    continue

                msg = text
                if detail:
                    msg += f"\n→ {detail}"
                if time_ago:
                    msg += f"\n{time_ago}"

                send_telegram(msg)
                sent_count += 1
                time.sleep(0.3)

    # Check for LinkedIn auth errors and alert
    linkedin_auth_error = False
    for err in errors:
        err_msg = err.get("error", "")
        if "LinkedIn" in err.get("source", "") and ("session" in err_msg.lower() or "expired" in err_msg.lower() or "login" in err_msg.lower()):
            linkedin_auth_error = True
            break

    # Send footer
    error_note = ""
    if errors:
        failed = ", ".join(e.get("source", "?") for e in errors)
        error_note = f"\n⚠️ Feed issues: {failed}"
    if linkedin_auth_error:
        error_note += "\n🔑 LinkedIn session expired — re-run linkedin-auth.py to fix"

    footer = f"🐛 {sent_count} items · {len(sources_seen)} sources · React 👍/👎 to shape future editions{error_note}"
    send_telegram(footer)

    # Save sent IDs for deduplication
    save_sent_ids(date_str, new_sent_ids)

    # Save item mapping for preference learning
    save_item_mapping(date_str, item_mapping)

    print(json.dumps({
        "status": "ok",
        "items_sent": sent_count,
        "sources": sorted(sources_seen),
        "errors": errors,
        "is_refresh": is_refresh,
        "new_items": len(top_articles),
    }))


if __name__ == "__main__":
    main()
