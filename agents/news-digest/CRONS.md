# CRONS.md — Lowly Worm Cron Schedule

All times in UTC.
Agent: news-digest
Workspace: .openclaw/news-digest-workspace/

---

## Morning Edition — Daily at 12:00 UTC (8 AM ET)

**Schedule:** `0 12 * * *`
**Command:** Generate and deliver the morning news digest.

1. Run `python3 ~/.openclaw/news-digest-workspace/scripts/fetch-and-rank.py`
   - Fetches all RSS feeds (NYT, WSJ, WaPo, Slate, Google News)
   - Fetches LinkedIn feed updates and notifications via linkedin-api
   - Deduplicates by URL and title similarity
   - Extracts topics via keyword matching against the preference model vocabulary
   - Ranks by: recency × topic_relevance × source_weight × diversity_bonus
   - Outputs ranked JSON to `cache/ranked-YYYY-MM-DD.json`
2. Read the ranked output. Select the top 15–20 items.
3. Generate extended headlines in a single batched LLM call: for each item, produce the original headline plus one sentence of context.
4. Group items by topic: 🤖 AI & Tech, 💰 Economics, 🌍 World, 🏛️ US Policy, 🔗 LinkedIn, 📋 Also Noted.
5. Format as a Telegram message using the template below and deliver.

**Morning edition format:**

```
🐛📰 Morning Edition — {date}

🤖 AI & TECH
━━━━━━━━━━━━━━━
1. {Headline}
   {Context sentence} — {Source}, {recency}

2. {Headline}
   {Context sentence} — {Source}, {recency}

💰 ECONOMICS
━━━━━━━━━━━━━━━
3. {Headline}
   {Context sentence} — {Source}, {recency}

🌍 WORLD
━━━━━━━━━━━━━━━
...

🏛️ US POLICY
━━━━━━━━━━━━━━━
...

🔗 LINKEDIN
━━━━━━━━━━━━━━━
{N}. {Post summary or notification}

📋 ALSO NOTED
━━━━━━━━━━━━━━━
• {Title} — 1 line — {Source}

🐛 {count} items · {source_count} sources · Reply: /like 1, /more 3, /ask [topic]
```

**On success:** Update status file with heartbeat, item count, source count, fetch duration.
**On failure (feed errors):**
1. Log which feeds failed and why
2. Deliver the digest with available sources — never skip delivery because one feed is down
3. Note failed sources at the bottom of the digest: "⚠️ {source} was unreachable today"

**Telegram output:** Always.

---

## Preference Update — Daily at 23:00 UTC

**Schedule:** `0 23 * * *`
**Command:** Process today's engagement signals and update the preference model.

1. Run `python3 ~/.openclaw/news-digest-workspace/scripts/update-preferences.py`
   - Reads new entries from `preferences/engagement.jsonl`
   - For each engagement event:
     - `thumbs_up` → topic weights × 1.1, source weight × 1.05
     - `thumbs_down` → topic weights × 0.85, source weight × 0.95
     - `expand` → topic weights × 1.05
   - Clamps topic weights to [0.1, 3.0] and source weights to [0.5, 2.0]
   - Writes updated weights to `preferences/model.json`
2. Log update summary to status file: how many events processed, which topics changed most.

**On success:** Update status file silently.
**On failure:**
1. Log the error
2. Alert human via Telegram: "⚠️ Preference update failed: {error}. Model unchanged."

**Telegram output:** Silent on success. Alert on failure only.

---

## Heartbeat — Every 30 Minutes

**Schedule:** `*/30 * * * *`
**Command:** Update your own status file with current heartbeat timestamp.

Write current UTC time to `last_heartbeat` in `~/Dropbox/openclaw-backup/agents/news-digest.status.md`. Produce NO output if successful.

**On success:** Silent.
**On failure:** Alert human via Telegram: "❌ news-digest heartbeat write failed."

**Telegram output:** Silent on success. Alert on failure only.

---

## Summary Table

| Cron | Frequency | Telegram | Auto-action |
|------|-----------|----------|-------------|
| Morning edition | Daily 12:00 UTC | Always | Fetch, rank, summarize, deliver |
| Preference update | Daily 23:00 UTC | On failure only | Update preference weights |
| Heartbeat | Every 30 min | On failure only | Write heartbeat to status file |
