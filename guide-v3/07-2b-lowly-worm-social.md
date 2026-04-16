![Clawford](../assets/Clawford2.png)

# Lowly Worm — social channel extension *(optional)*

*Last updated: 2026-04-15 · Reading time: ~25 min · Difficulty: hard*

> 🔦 **Optional extension.** This chapter is a layer on top of [Ch 07-2a — Lowly Worm (newsfeed)](07-2a-lowly-worm-newsfeed.md), which stands on its own and is useful without any of what follows. Read this one only if you want Lowly to also watch a specific social channel (LinkedIn, X, Bluesky, Mastodon, Discord, Slack…) and consolidate what's happening there into your morning edition. If you're not sure whether you need it, deploy 07-2a first, live with it for a couple of weeks, and come back here if you find yourself missing the social-channel half.

**TL;DR**

- The idea: your morning Telegram thread should also include **filtered notifications** (what is happening *to* you on your primary social channel) and **consolidated new messages** (DMs grouped by thread and summarized, so one back-and-forth becomes one item instead of forty). This is the shape of the problem, regardless of which platform you're scraping.
- For me, that channel is **LinkedIn**, which is where most of my professional contact runs. The rest of this chapter is LinkedIn-specific because that is the surface I have working. Your primary channel may be different — the pattern generalizes but the extraction layer is yours to build.
- LinkedIn is a dark art. The official Python library fails on datacenter IPs. Paid scraper APIs return public keyword matches instead of your actual feed. The DOM is deliberately obfuscated with hashed class names. The only reliable path is **Playwright + a persistent Chromium profile authenticated out-of-band.** Budget real time for this, and expect a few hours of tune-up every few months.
- Three surfaces to scrape (plus one optional fourth): the **home feed**, the **notifications stream** (filtered for relevance), and **DMs** (consolidated by thread with LLM-generated summaries via `agents.shared.llm.infer()`). Profile viewers are the optional fourth.
- **One incident you should read before you deploy this:** on 2026-04-14, an overly-trusting Playwright click path in the DM scraper sent two real messages to a real contact. The story, the three-layer defense that replaced the click path, and the regression test that pins it are in "The smart-reply chip incident" section below. This is the scar-tissue centerpiece of the whole chapter — read it before you ship, not after.
- The cost of the extension is **zero additional dollars** — the DM-summarization inference calls ride on your existing ChatGPT Plus subscription via `codex`. The real cost is maintenance time: Playwright stays fragile, aria-label selectors rot as LinkedIn rebuilds UI, and session profiles expire every few months.

## Meet the agent

This chapter is the other half of Lowly Worm — the Busytown character introduction is in [Ch 07-2a](07-2a-lowly-worm-newsfeed.md#meet-the-agent) and there is no separate character for the extension. What is different is the surface: where the core Lowly reads RSS feeds and ranks them against a preference model, the social extension reads a single specific professional channel — in my case LinkedIn — and folds its filtered output into the same morning Telegram thread as the newsfeed. Same voice, same agent, additional surface.

## Why you'd want this extension — and why you might not

The core Lowly from [Ch 07-2a](07-2a-lowly-worm-newsfeed.md) handles RSS newsfeeds well. What it doesn't give you is any signal on what's happening to you *on your social channel* — who commented on your last post, who's asking you something in DMs, whether anyone you want to hear from is active today. For someone whose professional network runs through LinkedIn (like me), that's a meaningful gap, and filling it was worth the scraping effort.

The social-extension agent lives inside the same morning Telegram thread as the newsfeed. A typical morning now looks like:

- 🤖 AI & Tech: four items from RSS
- 💰 Economics: three items from RSS
- 🌍 World: two items from RSS
- 🔗 LinkedIn: three filtered notifications + two DM threads summarized
- 🏛️ US Policy: two items from RSS
- 📋 Also noted: a few stragglers

The LinkedIn section sits in the same thread as the news, tagged by category, indistinguishable structurally from the RSS sections — the reader can scan one thread and get both the news *and* the social pulse in the same ninety seconds.

**Why you might not.** The LinkedIn machinery is real work and real maintenance. The DOM rots every few months. The auth profile expires periodically. The notifications filter needs occasional tuning as LinkedIn adds new noise categories. If any of the following apply, skip this chapter and stick with the core newsfeed from 07-2a:

- **Your primary social channel is not LinkedIn.** Nothing below translates directly — the extraction layer is LinkedIn-specific. Read this chapter as a *pattern* (feed + notifications + messages + per-thread summarization via a shared inference wrapper) and build your own on top.
- **You already check the social app directly during the day and don't need a morning summary.** Most of the value here is *consolidation*, not *speed*. If you already check the app habitually, the agent is mostly redundant.
- **You don't want to maintain a Playwright-based scraper long-term.** This is the single biggest ongoing tax of the whole Lowly project. Be honest about whether you'll actually do the tune-up versus let it rot. A dead LinkedIn scraper fails silently — "zero items returned, no error" — which is worse than no scraper at all.

## What success looks like

Three surfaces of your primary social channel, consolidated and delivered in the same morning thread as the newsfeed:

1. **The home feed.** A ranked, deduplicated selection of posts from accounts you follow. Same treatment as RSS items — one-sentence extended headline, topic tag, 👍/👎/📖 buttons.
2. **Filtered notifications.** What is happening *to you* on the platform, not just what is in the feed. Someone commented on your post. Someone reacted to a thing you shared last week. Someone from an unusual company viewed your profile. The raw stream is mostly noise (your-own-post engagement metrics, work-anniversary prompts, trending-content roundups) and most of it gets dropped before the LLM ever sees it. Typically 30 raw notifications shrinks to 2-5 worth surfacing.
3. **Consolidated DMs.** New messages grouped by thread and summarized across each thread's unread chunk — so a 40-message back-and-forth with one person becomes *one* item ("3 new messages from {person} re: {topic}") instead of forty. The per-thread summarization is the non-obvious part, and it is the single biggest quality improvement I have made to this extension since it shipped.
4. **(Optional) Profile viewers.** The "who viewed your profile" signal, filtered for relevance. Most profile views are uninteresting (recruiters you've already ignored, viewers from your own employer); the relevant subset is usually 0-2 per morning. Skippable if you don't care about inbound-attention signal.

## What makes this hard — LinkedIn is a dark art across four surfaces

Every obvious approach to LinkedIn fails for a specific reason:

- **The `linkedin-api` Python library** returns CHALLENGE responses from datacenter IPs within minutes of the first successful login, even behind a sticky residential proxy. Cookies invalidate mid-session. The library's author is playing a losing game against LinkedIn's anti-automation team and it shows.
- **Apify and similar scraping APIs** return *public* LinkedIn posts matching a keyword, not your personal feed. They can give you "all posts mentioning AI" but they can't give you "what the people you follow are saying today." Wrong API shape for a personalized digest.
- **RSS feeds for LinkedIn** were deprecated years ago. There is no RSS for a home feed.
- **Screen-scraping the mobile app API** requires reverse-engineering mutual TLS + signed requests. Full-time job, signatures rotate.

The one path that survives is **Playwright driving a persistent Chromium profile authenticated out-of-band** — and even that is held together with string and JavaScript.

### The auth primitive (shared by all four surfaces)

The auth story is the same for every LinkedIn surface:

1. **A one-time interactive auth step** (via `scripts/linkedin-auth.py`) opens a Chromium window through `chrome://inspect` remote debugging on a local machine. You log in like a human, solve any CAPTCHA, and the resulting Chromium profile directory gets saved to the agent's workspace. That profile directory *is* the credential; the agent never sees a password.
2. **A host-cron keep-alive** (`linkedin-keepalive`, every 6 hours) pokes the session just often enough that LinkedIn doesn't flag it idle. Without this, the session dies within a day or two and every scraper silently returns empty.
3. **Extraction uses `page.evaluate()` with stable `aria-label` selectors.** LinkedIn's class names are hashed (`_936a7c6b`) and rot in weeks. The JS extractors — one per surface, committed as `scripts/linkedin-*-extract.js` — target `aria-label` attributes like `"Open control menu for post by {Name}"` because those stay stable across LinkedIn's rebuilds. Without that trick, your selectors rot in weeks.

From there, each of the four surfaces layers its own work on top.

### Surface 1 — the home feed

`linkedin-scrape.py` launches a Playwright browser against the persistent profile, navigates to `linkedin.com/feed/`, waits for the post list to render, and calls `page.evaluate()` to load `linkedin-extract.js`. The JS walks the DOM and returns an array of `{author, text, link, reactions, timestamp, post_type}` items. Python filters duplicates against the previous N days of seen posts via `linkedin-seen.json` (hashes post text + author, keeps the last 500 entries), and the survivors become candidates for the morning edition's "LinkedIn" section.

This is the easy LinkedIn surface. The DOM is relatively consistent day to day, the aria-labels are well-named, the dedup story is straightforward. If you're only scraping the feed, you can probably keep this alive with quarterly touch-ups.

### Surface 2 — the notifications stream

`linkedin-notifs-extract.js` walks the `/notifications` page, which is a different DOM tree with its own obfuscated class names and its own aria-label conventions. The scraper returns `{type, actor, text, link, timestamp}` for each notification.

The harder part is filtering. LinkedIn's notifications stream is mostly noise — on an average morning I get a dozen notifications and maybe two are interesting. The ranking step drops the known-uninteresting categories (your-own-post engagement metrics, work-anniversary prompts, trending-content roundups, *"your network is talking about X"*) and keeps only:

- **Direct interactions with your content** — comments or reactions on *your* posts, inbound on things you wrote.
- **Inbound attention from someone new** — profile views from unusual companies, connection requests from people you don't already know.
- **Responses to threads you're already in** — replies to comments you left on someone else's post.

The relevant subset typically shrinks 30 raw notifications to 2-5 items worth surfacing. The filter is pure Python — no LLM needed — though the morning-edition composition in [Ch 07-2a](07-2a-lowly-worm-newsfeed.md) can still choose to promote or demote based on the preference model.

### Surface 3 — LinkedIn DMs (and the incident that shaped how they work)

The DM scraper is the hardest of the four surfaces, and it is the surface where a misclick can send a real message to a real contact. A 40-message back-and-forth with one person should arrive as **one** item in the morning edition, not forty. The current architecture is shaped entirely by one incident on 2026-04-14 — the scraper's previous implementation sent two real messages to a real contact on that morning's cron run. The next section tells that story in full. The architecture you see described here is the *fix*, not the original design.

The pieces:

1. **`linkedin-messages-extract.js`** walks the Messages panel and extracts each thread's metadata — thread ID, participant name, last-message timestamp, unread count, the full text of any unread messages, **and the per-thread URL** (`item.querySelector('a[href*="messaging"]').href`). The URL is the load-bearing field; the Python wrapper uses it to navigate directly to each thread without ever clicking inside the messaging UI.
2. **Grouping by thread happens in Python** (`linkedin-scrape.py`), and crucially **enrichment is direct navigation, not clicking**: for each thread with a real conversation URL, `page.goto(thread["url"])` opens it and `page.evaluate(...)` extracts the bubbles. There is no `page.click()` and no `page.query_selector(text=...)` anywhere in the messaging-enrichment loop. Threads whose extractor URL is the bare `/messaging/` fallback (date dividers, smart-reply chips, anything without its own anchor) skip enrichment entirely and ship with the preview only.
3. **A network-level send-block runs in `main()`.** `page.route()` handlers on `**/voyager/api/messaging/conversations/**` and friends abort `POST/PUT/PATCH/DELETE`. GETs (which is how the page READS the inbox) pass through. This is defense in depth: if a future regression ever reintroduces a stray click on a smart-reply chip, the resulting send request never leaves the browser, and a `[send-block]` line goes to stderr as a loud signal. Routes are scoped narrowly — global `**/*` interception slows page loads enough to break extraction (see the Pitfalls section below).
4. **Summarization uses `agents.shared.llm.infer()`**, not a separate LLM cron. A small structured-output prompt takes the thread's unread chunk and returns a one-sentence summary plus a topic tag. This runs inline with the morning-edition compose step.
5. **The summary feeds into `cache/morning-items.json`** as `source=linkedin`, `source_label="LinkedIn Message"`, with the summarized thread as the `extended_headline`.

The summarization-via-infer pattern landed in commit `6afa924` (*"restore LinkedIn thread summary via openclaw infer"*) and it is the single biggest quality improvement I have made to this extension since it shipped. Before it, a morning with three active DM conversations produced a wall of individual unread-message items that each earned their own scroll. After it, the same morning produces three one-line summaries I can triage in seconds.

The inference calls ride on the ChatGPT Plus subscription via `codex`, same pattern as the preference-learning LLM judge in [Ch 07-2a](07-2a-lowly-worm-newsfeed.md). **Zero additional dollars.**

### Surface 4 — profile viewers *(optional)*

`linkedin-viewers-extract.js` scrapes the "who viewed your profile" panel. This is a quiet channel: most views are uninteresting (recruiters you've already ignored, viewers from your own employer), and the relevant subset is usually 0-2 per morning. The scraper pulls the data, Python filters the obviously-uninteresting categories, and any survivors get folded into the notifications section as a single aggregated item — *"2 new profile views this morning: {Company A}, {Company B}."*

This is the most skippable of the four surfaces. If you don't care about profile-view signal, delete the scraper and the filter and you lose nothing material.

## The smart-reply chip incident (2026-04-14)

On the morning of 2026-04-14, Lowly Worm's morning-edition cron ran its LinkedIn DM scraper, and the scraper **sent two real messages to a real contact**. Actual text dispatched to an actual human's inbox. The messages were LinkedIn's own smart-reply chip text — *"Nope"* and *"Not at all"* — suggested replies that the LinkedIn messaging UI renders as clickable shortcuts. The scraper thought it was clicking on a thread in the inbox list. It was clicking on the chips.

This is the scar-tissue centerpiece of this chapter. Everything the DM architecture does now — the direct URL navigation, the bare-URL guard, the network-level send-block — exists because of that morning. If you skip the rest of this section, understand at least that the architecture above is not paranoid for the sake of paranoia. It is paranoid because a less-paranoid version of the same code already sent messages.

### What went wrong

LinkedIn renders both the inbox sidebar and the auto-opened most-recent conversation under `[role="main"]` simultaneously. Both surfaces use `<li>` elements. The conversation pane includes LinkedIn's smart-reply chips ("Reply to conversation with 'Nope'", "Reply to conversation with 'Not at all'", etc.) as clickable list items inside the same DOM region as the real inbox threads.

`linkedin-messages-extract.js`'s selector was `mainEl.querySelectorAll('li')`. That swept the smart-reply chips into `thread_summaries` as if they were inbox threads. Each "thread" in the returned list had a sender (the chip text looked plausible as a sender name), a timestamp (null — the chips don't have them, which *should* have been a red flag), and no URL (same).

The Python wrapper then iterated the result list and did what looked like reasonable Playwright code:

```python
thread_el = page.query_selector(f'text="{thread["sender"]}"')
thread_el.click(force=True)
```

Two things blew this up:

1. **Playwright's `text="..."` matcher is global.** It returns the first element anywhere on the page whose visible text matches, regardless of where the script *thought* it was navigating. The visible text matched the chip — which was sitting in the conversation pane, not in the inbox sidebar — and the matcher returned the chip.
2. **`force=True` dispatches the click even if the element isn't visible or interactable.** It went straight through to LinkedIn's send endpoint.

The cron sent *"Nope"* to the contact, the page re-rendered with a new smart-reply set, the scraper tried to click the next "thread," and it sent *"Not at all"*. A third click attempt failed — Playwright couldn't find the element after the DOM had shifted under it — which is the only reason the count stopped at two instead of three.

### The fix (three layers, all mandatory)

The architecture described in the Surface 3 section above is the result. Each layer is independently necessary; removing any one of them reintroduces a way for this class of bug to come back.

**Layer 1 — Direct URL navigation, not clicks.** `linkedin-messages-extract.js` already pulls a per-thread URL into each result item via `item.querySelector('a[href*="messaging"]')`. Use it. The Python wrapper now does `page.goto(thread["url"])` and `page.evaluate(...)` to extract bubbles, and has no `query_selector(text=...)` or `.click()` calls anywhere inside the messaging view. Direct navigation is strictly safer because it cannot land on the wrong element — you either go to the URL or you don't, and smart-reply chips have no URL.

**Layer 2 — Bare-URL guard.** When the JS extractor can't find a thread anchor (which is what happens for date dividers and smart-reply chips — they have no `<a href>` of their own), it falls back to the bare `https://www.linkedin.com/messaging/` URL. The Python wrapper treats that as "skip enrichment" and keeps the preview-only entry. The guard is a single line:

```python
if not url or url.rstrip("/").endswith("/messaging"):
    enriched.append(thread)  # keep preview, skip enrichment
    continue
```

Without this, false positives that slip through Layer 1 still reach the page-interaction loop.

**Layer 3 — Network-level send-block.** Even with both layers above, `page.route()` handlers on LinkedIn's voyager messaging endpoints — `**/voyager/api/messaging/conversations/**`, `**/voyager/api/voyagerMessagingDashMessengerMessages/**`, and related — abort `POST/PUT/PATCH/DELETE`. GETs pass through (that's how the page READS the inbox). Every abort prints a `[send-block]` line to stderr. This is the safety net for the day a future regression reintroduces a stray click or a new LinkedIn UI drops chips into an unexpected DOM location. The outbound HTTP request gets killed before it leaves the browser, and the stderr line is the loud signal that the safety net fired.

### The regression test

Commit `f81b9d7` landed the three-layer fix and added two tests that pin the contract:

- `test_scrape_messages_navigates_by_url_never_clicks` — asserts `page.query_selector` is never called with a `text=` selector inside the messaging enrichment loop, and that `page.goto` is called with each thread's URL.
- `test_scrape_messages_skips_enrichment_for_bare_messaging_url` — asserts the bare-URL guard fires on a fallback URL and skips enrichment without navigating.

Both live in `agents/news-digest/tests/test_linkedin_messages_extract.py`. They are the tests that *would have* caught the bug before it shipped, and they now live in CI specifically to prevent the same shape from coming back.

### The lesson

The lesson I took from this, beyond the specific fix: **any Playwright code path that sends user-generated content at runtime needs network-level defense in depth, not just correctness in the click handler.** Clicks can land on the wrong element for reasons that are not obvious in review. Selectors can match things your mental model of the page doesn't include. The happy-path correctness of a scraper is not a substitute for a network-level block on the specific mutation endpoints you never want to hit by accident. If a bug class's consequence is "a real message to a real person," the contract is "the HTTP request for that message does not leave the browser under any circumstance the code did not explicitly authorize." Layer 1 and Layer 2 together get you to "the scraper doesn't click on the wrong thing." Layer 3 is what makes the story "no second message went out even if it had." Pay for all three.

## Deployment additions

Step 3 of the [Ch 07-0](07-0-your-first-agent.md) arc — "Write or port the scripts" — is where this chapter lands on top of the existing Lowly Worm deploy. You should already have [Ch 07-2a](07-2a-lowly-worm-newsfeed.md) deployed and producing a morning edition before you start.

### LinkedIn first-time auth

Before you can scrape anything, you need an authenticated Chromium profile for LinkedIn. This is a one-time interactive step that you run on your laptop, not on the VPS:

1. Run `python3 agents/news-digest/scripts/linkedin-auth.py` locally (you need a real browser with a display).
2. A Chromium window opens at `linkedin.com/login`. Log in with your real account. Solve any CAPTCHA.
3. Once you're at the home feed, close the window. The script saves the Chromium profile directory.
4. Tar the resulting `linkedin-profile/` directory and `scp` it to the VPS at `~/.clawford/news-digest-workspace/linkedin-profile/`. Extract it there.

From then on, `linkedin-scrape.py` reuses that profile. When it eventually breaks — cookies do eventually expire, LinkedIn occasionally invalidates stale sessions en masse — you re-run the auth script locally and re-ship the profile. Expect to do this once every few months.

### The extra host cron

Adding the social-channel extension adds exactly one host cron to what [Ch 07-2a](07-2a-lowly-worm-newsfeed.md) already installs via `ops/scripts/install-host-cron.sh`:

| Host cron | Schedule (UTC) | What it does |
|---|---|---|
| `linkedin-keepalive` | `0 */6 * * *` | Runs `linkedin-keepalive.py`, which pokes the Playwright session just often enough to keep LinkedIn from flagging it idle. 300-second timeout because Playwright is slow to start and slow to navigate. |

The `linkedin-keepalive` entry gets added to `install-host-cron.sh` alongside the other CONTRACT_ENTRIES; re-running `install-host-cron.sh` picks it up automatically.

### Wiring into the morning edition

`fetch-and-rank.py` in the core Lowly pulls RSS. In the social-extension version, it also:

1. **Launches Playwright** with the persistent profile (reusing the keepalive-kept session).
2. **Scrapes the four surfaces** — feed, notifications, messages, optionally viewers.
3. **Runs the notifications filter** (pure Python, no LLM).
4. **Runs the messages summarization** (one `agents.shared.llm.infer()` call per thread with unread messages).
5. **Merges the social items into the ranked output** alongside RSS items, tagged with `source=linkedin` and an appropriate `source_label`.

The `morning-edition.py` compose step then selects items, writes extended headlines, groups by topic, and writes `morning-items.json` — same as the core Lowly, just with more items flowing through it.

### Smoke test

After the extension deploy, run the morning-edition cron manually and verify the LinkedIn items show up in `morning-items.json`:

```bash
ssh openclaw@<vps> "~/repo/ops/scripts/news-digest-morning-edition-host.sh"
ssh openclaw@<vps> "python3 -c \"import json; items = json.load(open('/home/openclaw/.clawford/news-digest-workspace/cache/morning-items.json')); print([i for i in items if i.get('source') == 'linkedin'])\""
```

You should see LinkedIn items with `source_label` values like `"LinkedIn"`, `"LinkedIn Notification"`, and `"LinkedIn Message"`. If none of them appear, the Playwright session is probably dead — run `linkedin-keepalive.py` by hand to poke it, and if that doesn't work, re-run `linkedin-auth.py` locally and re-ship the profile.

## Pitfalls you'll hit

> 🧨 **Pitfall.** Reaching for `linkedin-api`, Apify, or any paid LinkedIn scraping service instead of Playwright. **Why:** official libraries fail on datacenter IPs within minutes (CHALLENGE cookies + session invalidation), and keyword-search APIs return random public posts instead of your actual feed. I tried three before landing on Playwright; none of them gave me *my* LinkedIn feed, which is what the reader actually wants. **How to avoid:** go straight to Playwright + persistent Chromium profile. Budget a full evening for the first setup. The JS DOM extractors and `aria-label` selectors are the non-obvious parts — read `scripts/linkedin-extract.js` before you write anything.

> 🧨 **Pitfall.** Adding a `page.click()` call anywhere inside the DM enrichment loop, even for "just this one case." **Why:** the smart-reply chip incident (above) is what happens when `page.click()` + `page.query_selector(text=...)` meets LinkedIn's messaging DOM. The fix was to forbid those calls in the enrichment loop entirely and pin the contract with a regression test. Adding a click back in — even wrapped in a condition, even "just for this one unusual thread shape" — reintroduces the class of bug. **How to avoid:** the contract is direct URL navigation only. If you think you need a click, either the JS extractor needs to expose more metadata (do that instead) or your use case is outside the enrichment loop and should use a different code path. The `test_scrape_messages_navigates_by_url_never_clicks` regression test will fail loudly if anyone tries.

> 🧨 **Pitfall.** Playwright `page.route("**/*", handler)` cripples page loading. **Why:** every request matched by a route pattern round-trips through Python — even if the handler just calls `route.continue_()` immediately. With a global `**/*` glob, every CSS file, JS bundle, font, image, and XHR pays the IPC cost. On the LinkedIn messaging page (which fires hundreds of requests during initial load) this is enough to push the conversation list past the script's `time.sleep(5)` wait window, and the JS extractor returns `items_found=0`. The first draft of the send-block used `**/*` and broke message extraction on the first deploy — `[send-block]` correctly never fired, but messages also dropped to zero. **How to avoid:** scope route patterns narrowly to the URL families you actually want to intercept — `**/voyager/api/messaging/conversations/**` and friends — so most requests bypass the handler and go straight to the network at full speed. As a rule of thumb: if your `page.route()` glob would match a `.css` or `.js` URL, it's too broad.

> 🧨 **Pitfall.** One stuck LinkedIn thread scrape eats the entire cron budget. **Why:** any `page.click()` + `page.wait_for_selector()` combination inside a per-item loop can burn minutes on a single stuck item — a loading spinner that never resolves, a modal that steals focus, a network hang. If you retry on failure, you multiply the problem. **How to avoid:** this pitfall is mostly retired on the messaging surface because direct `page.goto()` navigation doesn't have the same retry behavior, but the lesson generalizes. Any `page.click()` call inside a per-item loop should have a hard 5-second cap, zero retries, and an outer try/except that accepts "one item missed" as strictly better than "the whole cron missed." Cron budgets are finite.

> 🧨 **Pitfall.** LinkedIn session silently expires without the keep-alive. **Why:** LinkedIn flags sessions as idle after a day or two with no activity and invalidates their cookies. The symptom is subtle: `linkedin-scrape.py` runs, Playwright navigates to `/feed`, the DOM loads, the extractor returns *zero items*, and the morning edition just… doesn't have a LinkedIn section that day. No error, no alert. **How to avoid:** the `linkedin-keepalive` host cron every 6 hours is non-optional — it exists specifically to prevent this. If you see a morning edition with zero LinkedIn items for two days in a row, run `linkedin-keepalive.py` by hand; if *that* doesn't work either, it's time to re-auth.

> 🧨 **Pitfall.** Aria-label rot after a LinkedIn UI rebuild. **Why:** the entire LinkedIn scraper story depends on `aria-label` attributes being stable across DOM rebuilds. That holds for months at a time and then LinkedIn pushes a UI update and one or two of the labels shift — `"Open control menu for post by {Name}"` becomes `"Post actions menu for {Name}"` or similar. Your scraper stops returning items for that specific interaction type. The symptom is the same "zero items, no error" shape as session expiration. **How to avoid:** when zero items returns *and* the keepalive works, open LinkedIn in your own browser, use DevTools to inspect the buttons you're trying to click, and check the current aria-labels against what your JS extractors expect. This happens once every few months and each fix is a one-line change in one of the `linkedin-*-extract.js` files.

## See also

- [Ch 07-2a — Lowly Worm: newsfeed](07-2a-lowly-worm-newsfeed.md) — the core chapter. This extension depends on it.
- [Ch 07-7 — Auth architectures](07-7-auth-architectures.md) — the cross-cutting reference for auth patterns. The Playwright + persistent Chromium profile + `chrome://inspect` pattern is one of the four strategies covered there.
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the LLM-vs-deterministic seam that the DM-summarization layer rides on, and the script contract.
- [`agents/news-digest/scripts/linkedin-extract.js`](../agents/news-digest/scripts/linkedin-extract.js) — the JS DOM extractor that uses `aria-label` selectors to survive LinkedIn's obfuscated class names. Read it before writing your own for a different platform.
- [`agents/news-digest/scripts/linkedin-auth.py`](../agents/news-digest/scripts/linkedin-auth.py) — the one-time interactive auth flow.
- [`agents/news-digest/scripts/linkedin-keepalive.py`](../agents/news-digest/scripts/linkedin-keepalive.py) — the keep-alive poker.
- [`agents/news-digest/tests/test_linkedin_messages_extract.py`](../agents/news-digest/tests/test_linkedin_messages_extract.py) — the regression tests that pin the three-layer DM safety contract.
