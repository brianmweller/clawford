#!/usr/bin/env python3
"""
on-demand.py — Search for articles about a topic across cached RSS + Google News + Brave.

Used for on-demand "what's happening with X?" queries. Searches today's
cached articles, fetches fresh Google News RSS, and optionally calls
Brave Search API. Outputs ranked results for the LLM to synthesize.

Usage: python3 on-demand.py "topic query"
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus

# feedparser is imported lazily inside search_rss() so bare invocation on a
# machine without the dep can still emit a contract-compliant JSON envelope
# via the __main__ tail.

WORKSPACE = Path(os.path.expanduser("~/.clawford/news-digest-workspace"))
CACHE_DIR = WORKSPACE / "cache"


def search_cache(query):
    """Search today's article cache for keyword matches."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    index_file = CACHE_DIR / f"index-{today}.jsonl"
    matches = []

    if not index_file.exists():
        return matches

    query_terms = query.lower().split()

    with open(index_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                article = json.loads(line)
                title_lower = article.get("title", "").lower()
                # Match if any query term appears in the title
                if any(term in title_lower for term in query_terms):
                    article["match_source"] = "cache"
                    matches.append(article)
            except json.JSONDecodeError:
                continue

    return matches


def fetch_google_news(query):
    """Fetch Google News RSS for the query."""
    import feedparser
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en&gl=US&ceid=US:en"
    articles = []

    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:10]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            summary = getattr(entry, "summary", "").strip()
            summary = re.sub(r"<[^>]+>", "", summary).strip()

            if title and link:
                articles.append({
                    "title": title,
                    "link": link,
                    "summary": summary[:300] if len(summary) > 300 else summary,
                    "source": "google_news",
                    "source_label": "Google News",
                    "match_source": "google_news_fresh",
                })
    except Exception as e:
        print(f"Google News fetch error: {e}", file=sys.stderr)

    return articles


def search_brave(query):
    """Search Brave API for the query."""
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        return []

    articles = []
    try:
        import urllib.request
        import urllib.error

        url = f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}&count=5&freshness=pd"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        })

        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        for result in data.get("web", {}).get("results", []):
            articles.append({
                "title": result.get("title", ""),
                "link": result.get("url", ""),
                "summary": result.get("description", ""),
                "source": "brave_search",
                "source_label": result.get("meta_url", {}).get("hostname", "Web"),
                "match_source": "brave_search",
            })
    except Exception as e:
        print(f"Brave Search error: {e}", file=sys.stderr)

    return articles


def deduplicate_results(results):
    """Simple URL-based dedup for on-demand results."""
    seen_urls = set()
    deduped = []
    for r in results:
        url = r.get("link", "")
        if url not in seen_urls:
            seen_urls.add(url)
            deduped.append(r)
    return deduped


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "message": "usage: on-demand.py 'query'"}))
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    start_time = time.time()

    # Search in parallel: cache + Google News + Brave
    all_results = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(search_cache, query): "cache",
            executor.submit(fetch_google_news, query): "google_news",
            executor.submit(search_brave, query): "brave",
        }
        for future in as_completed(futures):
            results = future.result()
            all_results.extend(results)

    # Deduplicate
    deduped = deduplicate_results(all_results)

    # Prioritize: cache matches first (already ranked), then fresh results
    cache_results = [r for r in deduped if r.get("match_source") == "cache"]
    fresh_results = [r for r in deduped if r.get("match_source") != "cache"]
    ranked = cache_results[:5] + fresh_results[:5]  # Max 10 results

    duration = round(time.time() - start_time, 1)

    output = {
        "status": "ok",
        "query": query,
        "total_results": len(ranked),
        "duration_sec": duration,
        "results": ranked,
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    import json as _contract_json
    import sys as _contract_sys
    _contract_status = "ok"
    _contract_error = None
    try:
        _contract_rc = main()
        if _contract_rc not in (0, None):
            _contract_status = "error"
            _contract_error = f"main returned {_contract_rc}"
    except SystemExit as _contract_e:
        if _contract_e.code not in (0, None):
            _contract_status = "error"
            _contract_error = f"main exited with code {_contract_e.code}"
    except BaseException as _contract_e:  # noqa: BLE001
        _contract_status = "error"
        _contract_error = str(_contract_e)[:200]
    _contract_envelope = {"status": _contract_status}
    if _contract_error:
        _contract_envelope["error"] = _contract_error
    print(_contract_json.dumps(_contract_envelope))
    _contract_sys.exit(0)
