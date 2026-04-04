#!/usr/bin/env python3
"""
fetch-and-rank.py — Fetch RSS feeds + LinkedIn, deduplicate, rank, output JSON.

Runs as part of the morning-edition cron. Outputs ranked articles to
cache/ranked-YYYY-MM-DD.json for the LLM to consume.

Usage: python3 fetch-and-rank.py
"""

import json
import os
import sys
import hashlib
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser

WORKSPACE = Path(os.path.expanduser("~/.openclaw/news-digest-workspace"))
CACHE_DIR = WORKSPACE / "cache"
PREFS_FILE = WORKSPACE / "preferences" / "model.json"

# RSS feed configuration
RSS_FEEDS = [
    # New York Times
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "source": "nyt", "label": "NYT"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "source": "nyt", "label": "NYT Tech"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "source": "nyt", "label": "NYT World"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "source": "nyt", "label": "NYT Business"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", "source": "nyt", "label": "NYT Politics"},
    # Wall Street Journal
    {"url": "https://feeds.content.dowjones.io/public/rss/RSSWorldNews", "source": "wsj", "label": "WSJ World"},
    {"url": "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness", "source": "wsj", "label": "WSJ Business"},
    {"url": "https://feeds.content.dowjones.io/public/rss/RSSWSJD", "source": "wsj", "label": "WSJ Tech"},
    {"url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "source": "wsj", "label": "WSJ Markets"},
    # Washington Post
    {"url": "https://feeds.washingtonpost.com/rss/politics", "source": "wapo", "label": "WaPo Politics"},
    {"url": "https://feeds.washingtonpost.com/rss/business/technology", "source": "wapo", "label": "WaPo Tech"},
    {"url": "https://feeds.washingtonpost.com/rss/world", "source": "wapo", "label": "WaPo World"},
    {"url": "https://feeds.washingtonpost.com/rss/business", "source": "wapo", "label": "WaPo Business"},
    # Slate
    {"url": "https://slate.com/feeds/all.rss", "source": "slate", "label": "Slate"},
    # Google News (topic-specific)
    {"url": "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en", "source": "google_news", "label": "Google AI"},
    {"url": "https://news.google.com/rss/search?q=economics+policy&hl=en&gl=US&ceid=US:en", "source": "google_news", "label": "Google Economics"},
    {"url": "https://news.google.com/rss/search?q=international+politics+geopolitics&hl=en&gl=US&ceid=US:en", "source": "google_news", "label": "Google Intl"},
    {"url": "https://news.google.com/rss/search?q=US+domestic+policy+legislation&hl=en&gl=US&ceid=US:en", "source": "google_news", "label": "Google US Policy"},
]


def article_id(url):
    """Generate a short article ID from URL (first 12 chars of MD5)."""
    return hashlib.md5(url.encode()).hexdigest()[:12]


def parse_pub_date(entry):
    """Extract publication datetime from a feed entry."""
    for field in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
    return datetime.now(timezone.utc)


def fetch_single_feed(feed_config):
    """Fetch and parse a single RSS feed. Returns list of article dicts."""
    url = feed_config["url"]
    source = feed_config["source"]
    label = feed_config["label"]
    articles = []

    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            return articles, {"source": label, "error": str(feed.bozo_exception)}

        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            summary = getattr(entry, "summary", getattr(entry, "description", "")).strip()
            # Strip HTML tags from summary
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            # Truncate overly long summaries
            if len(summary) > 500:
                summary = summary[:497] + "..."

            if not title or not link:
                continue

            pub_date = parse_pub_date(entry)
            articles.append({
                "id": article_id(link),
                "title": title,
                "link": link,
                "summary": summary,
                "source": source,
                "source_label": label,
                "pub_date": pub_date.isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })

        return articles, None

    except Exception as e:
        return [], {"source": label, "error": str(e)}


def fetch_linkedin():
    """Fetch LinkedIn feed updates and notifications. Returns list of article dicts."""
    articles = []
    linkedin_user = os.environ.get("LINKEDIN_USER")
    linkedin_pass = os.environ.get("LINKEDIN_PASS")

    if not linkedin_user or not linkedin_pass:
        return articles, {"source": "LinkedIn", "error": "credentials not configured"}

    try:
        from linkedin_api import Linkedin

        api = Linkedin(linkedin_user, linkedin_pass)

        # Fetch network updates (feed posts)
        updates = api.get_network_updates(limit=20)
        for update in updates:
            # Extract what we can from the update structure
            text = ""
            link = "https://www.linkedin.com"
            actor_name = "LinkedIn"

            if isinstance(update, dict):
                # Try to extract post text
                value = update.get("value", {})
                activity = value.get("activity", {})
                text = activity.get("text", "") or value.get("text", "")

                # Try to get the actor name
                actor = value.get("actor", {})
                actor_name = actor.get("name", "LinkedIn connection")

                # Try to get permalink
                link = activity.get("permalink", link)

            if text:
                title = f"{actor_name}: {text[:120]}{'...' if len(text) > 120 else ''}"
                articles.append({
                    "id": article_id(link + text[:50]),
                    "title": title,
                    "link": link,
                    "summary": text[:300] if len(text) > 300 else text,
                    "source": "linkedin",
                    "source_label": "LinkedIn",
                    "pub_date": datetime.now(timezone.utc).isoformat(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })

        # Fetch notifications (profile views, etc.)
        try:
            notifications = api.get_notifications(limit=10)
            for notif in notifications:
                if isinstance(notif, dict):
                    text = notif.get("text", notif.get("headline", ""))
                    if text:
                        articles.append({
                            "id": article_id("notif-" + text[:50]),
                            "title": text[:200],
                            "link": "https://www.linkedin.com/notifications/",
                            "summary": text,
                            "source": "linkedin",
                            "source_label": "LinkedIn Notification",
                            "pub_date": datetime.now(timezone.utc).isoformat(),
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        })
        except Exception:
            pass  # Notifications are best-effort

        return articles, None

    except ImportError:
        return [], {"source": "LinkedIn", "error": "linkedin-api not installed"}
    except Exception as e:
        return [], {"source": "LinkedIn", "error": str(e)}


def normalize_title(title):
    """Normalize a title for deduplication comparison."""
    title = title.lower()
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def word_trigrams(text):
    """Generate word-level trigrams from text."""
    words = text.split()
    if len(words) < 3:
        return {tuple(words)}
    return {tuple(words[i:i+3]) for i in range(len(words) - 2)}


def jaccard_similarity(set_a, set_b):
    """Compute Jaccard similarity between two sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def deduplicate(articles):
    """Remove duplicate articles by URL and title similarity."""
    seen_urls = set()
    kept = []
    kept_trigrams = []

    # Source priority for when we merge duplicates
    source_priority = {"wsj": 5, "nyt": 4, "wapo": 3, "slate": 2, "google_news": 1, "linkedin": 1}

    for article in articles:
        url = article["link"]

        # Exact URL dedup
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Title similarity dedup
        norm_title = normalize_title(article["title"])
        trigrams = word_trigrams(norm_title)
        is_dup = False

        for i, existing_trigrams in enumerate(kept_trigrams):
            sim = jaccard_similarity(trigrams, existing_trigrams)
            if sim > 0.6:
                # Keep the higher-priority source version
                existing_priority = source_priority.get(kept[i]["source"], 0)
                new_priority = source_priority.get(article["source"], 0)
                if new_priority > existing_priority:
                    # Replace with higher-priority source
                    also_in = kept[i].get("also_in", [])
                    also_in.append(kept[i]["source_label"])
                    article["also_in"] = also_in
                    kept[i] = article
                    kept_trigrams[i] = trigrams
                else:
                    also_in = kept[i].get("also_in", [])
                    also_in.append(article["source_label"])
                    kept[i]["also_in"] = also_in
                is_dup = True
                break

        if not is_dup:
            kept.append(article)
            kept_trigrams.append(trigrams)

    return kept


def extract_topics(article, vocabulary):
    """Extract topic tags from article title and summary using keyword matching."""
    text = (article["title"] + " " + article.get("summary", "")).lower()
    topics = []
    for topic, keywords in vocabulary.items():
        for keyword in keywords:
            if keyword.lower() in text:
                topics.append(topic)
                break
    return topics if topics else ["general"]


def recency_score(pub_date_str):
    """Score article by recency. 1.0 for <6h, decays to 0.3 for >24h."""
    try:
        pub_date = datetime.fromisoformat(pub_date_str)
        age_hours = (datetime.now(timezone.utc) - pub_date).total_seconds() / 3600
    except (ValueError, TypeError):
        age_hours = 12  # default to middle-aged

    if age_hours <= 6:
        return 1.0
    elif age_hours >= 24:
        return 0.3
    else:
        # Linear decay from 1.0 to 0.3 between 6h and 24h
        return 1.0 - 0.7 * (age_hours - 6) / 18


def rank_articles(articles, prefs):
    """Rank articles using the preference model."""
    topic_weights = prefs.get("topic_weights", {})
    source_weights = prefs.get("source_weights", {})
    vocabulary = prefs.get("topic_vocabulary", {})

    # Track source counts for diversity bonus
    source_counts = {}

    for article in articles:
        topics = extract_topics(article, vocabulary)
        article["topics"] = topics

        # Topic relevance: max weight across matched topics
        topic_rel = max((topic_weights.get(t, 1.0) for t in topics), default=1.0)

        # Source weight
        src_weight = source_weights.get(article["source"], 1.0)

        # Recency
        recency = recency_score(article["pub_date"])

        # Diversity bonus: boost underrepresented sources
        source_counts[article["source"]] = source_counts.get(article["source"], 0) + 1
        diversity = 1.2 if source_counts[article["source"]] <= 3 else 1.0

        article["score"] = topic_rel * src_weight * recency * diversity
        article["_debug_scores"] = {
            "topic_rel": round(topic_rel, 2),
            "src_weight": round(src_weight, 2),
            "recency": round(recency, 2),
            "diversity": round(diversity, 2),
        }

    articles.sort(key=lambda a: a["score"], reverse=True)
    return articles


def assign_categories(articles):
    """Assign display category to each article based on primary topic."""
    topic_to_category = {
        "ai": "🤖 AI & TECH",
        "tech": "🤖 AI & TECH",
        "economics": "💰 ECONOMICS",
        "markets": "💰 ECONOMICS",
        "international_politics": "🌍 WORLD",
        "us_domestic_policy": "��️ US POLICY",
        "regulation": "🏛️ US POLICY",
    }

    for article in articles:
        if article["source"] == "linkedin":
            article["category"] = "🔗 LINKEDIN"
        else:
            primary_topic = article["topics"][0] if article["topics"] else "general"
            article["category"] = topic_to_category.get(primary_topic, "📋 ALSO NOTED")

    return articles


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Load preference model
    try:
        with open(PREFS_FILE) as f:
            prefs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        prefs = {"topic_weights": {}, "source_weights": {}, "topic_vocabulary": {}}

    print(f"Fetching {len(RSS_FEEDS)} RSS feeds + LinkedIn...", file=sys.stderr)
    start_time = time.time()

    all_articles = []
    errors = []

    # Fetch RSS feeds in parallel (max 5 concurrent)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_single_feed, feed): feed for feed in RSS_FEEDS}
        for future in as_completed(futures):
            articles, error = future.result()
            all_articles.extend(articles)
            if error:
                errors.append(error)
            # Small delay to be polite
            time.sleep(0.5)

    # Fetch LinkedIn (separate, not in thread pool)
    linkedin_articles, linkedin_error = fetch_linkedin()
    all_articles.extend(linkedin_articles)
    if linkedin_error:
        errors.append(linkedin_error)

    fetch_duration = round(time.time() - start_time, 1)
    print(f"Fetched {len(all_articles)} raw articles in {fetch_duration}s", file=sys.stderr)
    if errors:
        print(f"Feed errors: {json.dumps(errors)}", file=sys.stderr)

    # Deduplicate
    deduped = deduplicate(all_articles)
    print(f"After dedup: {len(deduped)} articles", file=sys.stderr)

    # Rank
    ranked = rank_articles(deduped, prefs)

    # Assign categories
    ranked = assign_categories(ranked)

    # Save to cache
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = CACHE_DIR / f"ranked-{today}.json"

    output = {
        "date": today,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetch_duration_sec": fetch_duration,
        "total_raw": len(all_articles),
        "total_deduped": len(deduped),
        "errors": errors,
        "articles": ranked,
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)

    # Also save article index for preference system (engagement lookups)
    index_file = CACHE_DIR / f"index-{today}.jsonl"
    with open(index_file, "w") as f:
        for article in ranked:
            entry = {
                "id": article["id"],
                "title": article["title"],
                "topics": article["topics"],
                "source": article["source"],
                "link": article["link"],
            }
            f.write(json.dumps(entry) + "\n")

    # Print summary to stdout (agent reads this)
    print(json.dumps({
        "status": "ok",
        "date": today,
        "total_articles": len(ranked),
        "sources": list({a["source"] for a in ranked}),
        "errors": errors,
        "output_file": str(output_file),
        "fetch_duration_sec": fetch_duration,
    }))


if __name__ == "__main__":
    main()
