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

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.shared import telegram_api

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


def send_telegram(text, reply_markup=None, silent=True):
    """Send a message via Telegram Bot API with previews disabled.

    Delegates to agents.shared.telegram_api.send_message. When bot
    credentials are missing, prints a dry-run log line to stderr so
    tests and manual runs without env still see what would have been
    sent.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[dry-run] {text[:100]}...", file=sys.stderr)
        return True

    return telegram_api.send_message(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        text,
        silent=silent,
        disable_web_preview=True,
        reply_markup=reply_markup,
    )


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


SENT_HISTORY_FILE = CACHE_DIR / "sent-history.json"
HISTORY_MAX_DAYS = 3  # Keep titles from last 3 days for cross-day dedup


def normalize_title(title):
    """Normalize a title for fuzzy matching."""
    import re
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def load_sent_history():
    """Load sent article IDs + normalized titles across days."""
    if SENT_HISTORY_FILE.exists():
        try:
            with open(SENT_HISTORY_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"ids": [], "titles": [], "updated_at": ""}


def save_sent_history(history):
    """Save sent history, pruning entries older than HISTORY_MAX_DAYS."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_MAX_DAYS)).isoformat()
    # Keep only recent entries
    entries = list(zip(history.get("ids", []), history.get("titles", []), history.get("timestamps", [])))
    fresh = [(i, t, ts) for i, t, ts in entries if ts > cutoff]
    history["ids"] = [e[0] for e in fresh]
    history["titles"] = [e[1] for e in fresh]
    history["timestamps"] = [e[2] for e in fresh]
    history["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(SENT_HISTORY_FILE, "w") as f:
        json.dump(history, f)


def is_duplicate(article, history):
    """Check if an article was already sent (by ID or similar title)."""
    article_id = article.get("id", "")
    if article_id in history.get("ids", []):
        return True

    # Fuzzy title match — catch same story from different outlets
    norm = normalize_title(article.get("title", ""))
    if len(norm) < 10:
        return False
    norm_words = set(norm.split())
    for prev_title in history.get("titles", []):
        prev_words = set(prev_title.split())
        if not norm_words or not prev_words:
            continue
        # Jaccard similarity on words
        intersection = len(norm_words & prev_words)
        union = len(norm_words | prev_words)
        if union > 0 and intersection / union > 0.5:
            return True
    return False


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

    # Load sent history for cross-day deduplication
    history = load_sent_history()
    is_refresh = len(history.get("ids", [])) > 0

    # Select up to 20 unseen items from the ranked list
    # Fresh fetch + dedup: skip anything already sent, take the next 20
    top_articles = []
    skipped = 0
    for article in articles:
        if len(top_articles) >= 20:
            break
        if is_duplicate(article, history):
            skipped += 1
            continue
        top_articles.append(article)

    if skipped:
        print(f"Skipped {skipped} duplicate articles", file=sys.stderr)

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
        "🔬 SCIENCE",
        "🚀 STARTUPS",
        "🌍 WORLD",
        "🏛️ US POLICY",
        "🔗 LINKEDIN",
        "📋 ALSO NOTED",
    ]

    # Wait until the top of the next hour (12:00 UTC / 5:00 AM PT) before sending
    # Cron triggers at :55, fetch+rank takes ~3 min, then hold until :00
    now = datetime.now(timezone.utc)
    if now.minute >= 50:
        # We're in the :55-:59 window — wait until the top of the next hour
        wait_seconds = (60 - now.minute) * 60 - now.second
        if wait_seconds > 0 and wait_seconds <= 600:  # Cap at 10 min
            target_hour = (now.hour + 1) % 24
            print(f"Holding delivery for {wait_seconds}s until {target_hour:02d}:00", file=sys.stderr)
            time.sleep(wait_seconds)

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

            # Inline 👍/👎 buttons for engagement
            buttons = {
                "inline_keyboard": [[
                    {"text": "\ud83d\udc4d", "callback_data": f"like:{item_num}"},
                    {"text": "\ud83d\udc4e", "callback_data": f"dislike:{item_num}"},
                ]]
            }

            send_telegram(msg, reply_markup=buttons)
            sent_count += 1

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

        # LinkedIn messages/InMail section
        msgs = li_data.get("messages", [])
        if msgs:
            send_telegram("💬 LINKEDIN MESSAGES\n━━━━━━━━━━━━━━━")
            time.sleep(0.3)

            for m in msgs:
                sender = m.get("sender", "")
                preview = m.get("preview", "")
                time_ago = m.get("time_ago", "")
                url = m.get("url", "https://www.linkedin.com/messaging/")
                unread = m.get("unread", False)

                if not sender or not preview:
                    continue

                indicator = "🔵 " if unread else ""
                msg = f"{indicator}{sender}\n{preview}\n{url}"
                if time_ago:
                    msg = f"{indicator}{sender} ({time_ago})\n{preview}\n{url}"

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
    send_telegram(footer, silent=False)  # Only the footer dings

    # Update sent history for cross-day deduplication
    now_iso = datetime.now(timezone.utc).isoformat()
    for article in top_articles:
        history["ids"] = history.get("ids", []) + [article.get("id", "")]
        history["titles"] = history.get("titles", []) + [normalize_title(article.get("title", ""))]
        history["timestamps"] = history.get("timestamps", []) + [now_iso]
    save_sent_history(history)

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
