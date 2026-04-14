#!/usr/bin/env python3
"""morning-edition.py — News digest LLM composition step.

Phase 3b replacement for the OpenClaw LLM cron that used to compose
the structured morning items. Pipeline:

  1. Read cache/ranked-<today>.json (written by fetch-and-rank.py,
     which the host-cron wrapper runs just before this script).
  2. Call agents.shared.llm.infer(json_mode=True) with a prompt that
     includes the ranked articles and asks for a structured items array.
  3. Parse the LLM response (defensive markdown-fence unwrap).
  4. Atomic-write cache/morning-items.json for morning-fleet-deliver.py
     to consume at 12:00 UTC.

No Telegram delivery here — the morning-fleet-deliver.py orchestrator
at 12:00 UTC reads the items file and sends each item with the inline
keyboard buttons via the NEWSDIGEST_BOT_TOKEN.

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


WORKSPACE = Path(os.path.expanduser("~/.openclaw/news-digest-workspace"))
CACHE_DIR = WORKSPACE / "cache"

# Generous timeout — the prompt can be ~5k tokens once ranked articles
# are injected, and the codex backend occasionally takes 30-60s on the
# first cold call of the day.
LLM_TIMEOUT_S = 120

# The prompt template. Kept as a module constant so the script and
# its prompt ship as one file. The `{ranked_json}` placeholder is
# replaced at runtime with a JSON dump of the articles list.
PROMPT_TEMPLATE = """\
Build the morning news digest for Lowly Worm (news-digest agent).

Below is a JSON array of articles that fetch-and-rank.py scored against
the user's preference model. Your job is to:

  1. Select the top 15-20 items (skip boring, duplicate, or low-signal
     entries — prioritize variety across topics).
  2. Write a one-sentence extended headline for each that explains WHY
     the item matters (context, stakes, who it affects).
  3. Group items by topic category, using these exact labels (with the
     leading emoji):

        🤖 AI & Tech
        💰 Economics
        🌍 World
        🏛️ US Policy
        🔗 LinkedIn
        📋 Also Noted

  4. Order items by category in the order above, then by importance
     within each category.

For LinkedIn message thread items (source=linkedin, source_label contains
"Message"), the title already contains the sender name and the summary
is an LLM-generated thread summary — use those as the headline fields
directly.

Return a JSON array. One object per selected item, in delivery order:

[
  {
    "num": 1,
    "category": "🤖 AI & Tech",
    "extended_headline": "<1 sentence rewritten headline + why it matters>",
    "title": "<raw article title>",
    "url": "<canonical url>",
    "source_label": "<source display, e.g. Reuters>",
    "topics": ["<topic tags from the ranked file>"]
  }
]

Ranked articles:
{ranked_json}
"""


def load_ranked_articles() -> tuple[list[dict], str]:
    """Read cache/ranked-<today>.json.

    Returns (articles, date_str). Raises FileNotFoundError if the ranked
    file is missing (host-cron wrapper should have run fetch-and-rank.py
    before this script). Raises RuntimeError if the file parses but
    contains no articles — we don't want to hand the LLM an empty list
    and ask it to invent a digest.
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


def build_prompt(articles: list[dict]) -> str:
    """Substitute the ranked articles JSON into the prompt template."""
    ranked_json = json.dumps(articles, indent=2)
    return PROMPT_TEMPLATE.replace("{ranked_json}", ranked_json)


def parse_items_response(text: str) -> list[dict]:
    """Parse the LLM response into a list of item dicts.

    Defensive markdown-fence unwrap — `json_mode=True` constrains the
    codex backend to JSON output but models sometimes still wrap in
    ```json … ``` fences, especially for arrays.
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
    if not isinstance(data, list):
        raise ValueError(
            f"expected JSON array, got {type(data).__name__}"
        )
    return data


def write_morning_items(items: list[dict], date_str: str) -> Path:
    """Atomic write to cache/morning-items.json.

    The .tmp file is rewritten in place with os.replace so
    morning-fleet-deliver.py (which reads this file at 12:00 UTC)
    never sees a partially-written JSON array.
    """
    path = CACHE_DIR / "morning-items.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, path)
    return path


def run() -> dict:
    articles, date_str = load_ranked_articles()
    prompt = build_prompt(articles)
    result = llm.infer(prompt, json_mode=True, timeout=LLM_TIMEOUT_S)
    if not result.ok:
        raise RuntimeError(f"LLM infer failed: {result.error}")
    items = parse_items_response(result.text or "")
    if not items:
        raise RuntimeError("LLM returned empty items list")
    path = write_morning_items(items, date_str)
    return {
        "status": "ok",
        "date": date_str,
        "items_count": len(items),
        "items_path": str(path),
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
