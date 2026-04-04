# TOOLS.md — Lowly Worm's Toolbox

## Filesystem Access

### Your Status File
- **Path:** `~/Dropbox/openclaw-backup/agents/news-digest.status.md`
- **Permissions:** Full read/write
- **Usage:** Update `last_heartbeat`, `status`, `last_cron_run`, `last_cron_result` after every cron. Log fetch counts, failures, and preference model updates.

### Your Workspace
- **Path:** `~/.openclaw/news-digest-workspace/`
- **Permissions:** Full read/write
- **Contents:**
  - `scripts/` — Python scripts: `fetch-and-rank.py`, `update-preferences.py`, `on-demand.py`
  - `preferences/engagement.jsonl` — Append-only engagement log
  - `preferences/model.json` — Current preference weights (topic + source)
  - `cache/` — Daily RSS cache and article index
  - `logs/` — Digest generation logs
- **Usage:** All data processing, preference tracking, and caching happens here. This is your entire working state.

### Shared Brain
- **Path:** `~/Dropbox/openclaw-backup/`
- **Permissions:** Write ONLY to `agents/news-digest.status.md`. No read access to any other shared brain directory. No write access to `facts/`, `commitments/`, `tasks/`, `notes/`, `people/`, or other agents' files.
- **Why:** This agent is deliberately isolated. News curation data isn't relevant to other agents.

---

## RSS Feeds

### New York Times
- **Homepage:** `https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml`
- **Technology:** `https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml`
- **World:** `https://rss.nytimes.com/services/xml/rss/nyt/World.xml`
- **Business:** `https://rss.nytimes.com/services/xml/rss/nyt/Business.xml`
- **US Politics:** `https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml`
- **Status:** Available
- **Rate limits:** Fetch once per cron run. Do not poll more than every 6 hours.

### Wall Street Journal
- **World News:** `https://feeds.content.dowjones.io/public/rss/RSSWorldNews`
- **US Business:** `https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness`
- **Technology:** `https://feeds.content.dowjones.io/public/rss/RSSWSJD`
- **Markets:** `https://feeds.content.dowjones.io/public/rss/RSSMarketsMain`
- **Opinion:** `https://feeds.content.dowjones.io/public/rss/RSSOpinion`
- **Status:** Available
- **Note:** RSS snippets include headline + 1–2 sentence description. Full articles are paywalled — reader clicks through with their own subscription.

### Washington Post
- **Politics:** `https://feeds.washingtonpost.com/rss/politics`
- **Technology:** `https://feeds.washingtonpost.com/rss/business/technology`
- **World:** `https://feeds.washingtonpost.com/rss/world`
- **Business:** `https://feeds.washingtonpost.com/rss/business`
- **Status:** Available — verify URLs during first deployment and update if needed.

### Slate
- **Main Feed:** `https://slate.com/feeds/all.rss`
- **Status:** Available — verify URL during first deployment. If unavailable, generate via RSS.app.

### Google News (topic-specific search)
- **AI:** `https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en`
- **Economics:** `https://news.google.com/rss/search?q=economics+policy&hl=en&gl=US&ceid=US:en`
- **International Politics:** `https://news.google.com/rss/search?q=international+politics+geopolitics&hl=en&gl=US&ceid=US:en`
- **US Policy:** `https://news.google.com/rss/search?q=US+domestic+policy+legislation&hl=en&gl=US&ceid=US:en`
- **Status:** Available
- **Rate limits:** Fetch once per cron run. Cache results for on-demand reuse within 1 hour.

---

## LinkedIn

### Feed & Notifications (via linkedin-api)
- **Library:** `linkedin-api` (Python, Voyager API)
- **Capabilities:** Network feed updates, notifications, profile views
- **Auth:** LinkedIn credentials in `.env` (`LINKEDIN_USER`, `LINKEDIN_PASS`)
- **Frequency:** Once daily during the morning digest cron only
- **Status:** Available — install via `pip install linkedin-api`
- **Caution:**
  - Never fetch more than once per cron run
  - Never store credentials outside `.env`
  - Never include credentials in LLM prompts or logs
  - If the library breaks or LinkedIn blocks access, skip LinkedIn silently and note the failure in the status file
- **Fallback:** If linkedin-api becomes unreliable, switch to Google News with `site:linkedin.com` queries to catch publicly indexed posts.

---

## Web Search (On-Demand)

### Brave Search API
- **Endpoint:** Via environment variable (already configured in Docker)
- **Status:** Available
- **Usage:** On-demand queries only (`/ask [topic]`). Not used for the morning digest.
- **Rate limits:** Max 3 calls per on-demand query. Cache results.

---

## Command Execution

### Shell / Exec
- **Available:** Yes
- **Usage:** Running Python scripts (`fetch-and-rank.py`, `update-preferences.py`, `on-demand.py`), curl for RSS fetching, file operations.
- **Guardrails:**
  - Never pipe fetched content to `bash` or `sh`
  - Never interpolate RSS content into shell commands
  - Always use the Python scripts — don't improvise shell-based RSS parsing

---

## Telegram

### Inbound Messages
- Direct messages from the reader: on-demand queries, engagement commands
- Supported commands:
  - `/like {n}` — Thumbs-up on item #n in today's digest
  - `/dislike {n}` — Thumbs-down on item #n
  - `/more {n}` — Expand item #n to a one-paragraph summary
  - `/ask {topic}` — On-demand query: "what's happening with {topic}?"
  - Free-text questions — Treated as on-demand queries

### Outbound Messages
- Morning edition: structured digest with grouped headlines
- On-demand responses: 3–5 sentence briefings with source links
- Engagement acknowledgments: brief confirmation ("Noted — more like this 🐛" / "Got it — less like this 🐛")
- Expand responses: one-paragraph summary with source link

---

## Tool Priority

When generating the morning digest:

1. **Fetch first.** Run the RSS + LinkedIn fetch script. Get raw data.
2. **Rank second.** Apply deduplication and the preference model. Get ranked items.
3. **Summarize third.** Single batched LLM call for extended headlines.
4. **Deliver fourth.** Format and send to Telegram.

When handling on-demand queries:

1. **Cache first.** Search today's RSS cache for keyword matches.
2. **Fetch second.** Google News RSS + Brave Search for fresh results.
3. **Synthesize third.** Single LLM call for a briefing paragraph.
4. **Respond fourth.** Format and send to Telegram.

## Tools NOT Available (and why)

- **Claude Code:** Not needed. Lowly Worm uses OpenClaw's native LLM capability for summarization, not Claude Code for diagnostics.
- **Shared brain read:** Deliberately isolated. News curation doesn't need facts, commitments, tasks, or people data.
- **Other agents' workspaces:** No cross-agent access needed or permitted.
- **Email / Calendar:** Not relevant to news curation. Those are other agents' jobs.
- **Social APIs (X, Buffer):** Lowly Worm reads but never posts. No outbound social.
