![Clawford](../assets/Clawford2.png)

# Lowly Worm — newsfeed 🐛📰

*Last updated: 2026-04-15 · Reading time: ~20 min · Difficulty: moderate*

**TL;DR**

- Lowly Worm is the filter between me and a morning of doom-scrolling. I still read the news — he just makes sure I'm not spending forty minutes before coffee foraging across six news sites looking for anything worth reading. Every morning at 5 AM PT he fetches WSJ, NYT, WaPo, Slate, and Google News, dedupes across outlets, ranks against a preference model that learns from my reactions, and delivers a personalized morning edition to Telegram with inline 👍/👎/📖 buttons on each item.
- **Preference learning is the hero of this chapter.** A coarse topic-weight model tunes itself from your thumbs-up/down signals, and a fine subtopic model gets proposed by an LLM judge on every thumbs-down. You don't have to know the subtopic taxonomy up front — the judge writes it for you as you react, and the digest gets sharper over time without any manual intervention. This is what makes a personalized feed actually useful instead of just "okay."
- Lowly Worm is **deliberately isolated from the shared brain.** He's the only agent in the fleet without read or write access to `~/Dropbox/openclaw-backup/`. His prompt-injection attack surface is large (he reads the open web); confining him to his own workspace keeps any injected instruction from reaching the rest of the fleet.
- He was the **pilot agent** for the Clawford-native migration — the first agent to migrate off the old LLM cron runtime onto the post-liberation stack (`agents.shared.llm.infer`, host crontab, the compose/deliver split). Every pattern he shipped in that migration became the template the other five agents inherited. The scar tissue is in the deployment walkthrough section.
- If you *also* want Lowly to watch a social channel (LinkedIn, X, Bluesky, etc.) for filtered notifications and consolidated DMs, that's an optional extension covered in [Ch 11](11-lowly-worm-social.md). **The core newsfeed stands on its own and is useful without any of that.** Deploy this chapter first; add 07-2b only if you care about the social half.

## Meet the agent

Lowly Worm is the smallest citizen of Busytown and — for reasons that never quite made sense — also the one you saw most often. The original Lowly rides around in a hollowed-out apple, shows up in every scene of every book, and seems to know a little about everything happening in town. Curious. Well-read. Endlessly involved in whatever was interesting at the moment. This Lowly got a narrower assignment: he is the filter between me and a morning of scrolling. I still read the news. What he does is fetch it from five or six different places, filter it against my preferences, and hand me a scannable digest before I've had coffee. He has editorial opinions about signal vs. noise. He notes when a story is getting disproportionate coverage. He admits it honestly when a query turns up nothing interesting. He does not ride in an apple car. Here's what's happening.

## Why you'd want one — and why you might not

Lowly Worm is the filter between me and a morning of doom-scrolling. I read the news; I just refuse to do the foraging. Without an agent, my first hour of the day used to involve five news tabs, and the algorithm on every one of them was optimizing for engagement rather than for what I actually needed to know. With Lowly, a single Telegram thread arrives at 5 AM PT, grouped by topic, with a one-sentence context gloss on each item and inline buttons to react. I can scan it in ninety seconds and read the three or four things that matter. He costs effectively nothing — every LLM call he makes rides on the flat-rate ChatGPT Plus subscription I'm already paying for, via `agents.shared.llm.infer()` — and the preference model sharpens over time. After a few weeks of thumbs-up/down signals the digest is noticeably more about what I read and less about what's loudly trending. For a reader who wants a daily briefing without the dopamine economy of a news site's landing page, he is the most obvious win in the fleet.

**Why you might not.** Three cases where even the core Lowly is a poor fit. **First:** if you're already disciplined about reading the news directly and *don't* want an algorithmic filter shaping what you see, you don't need a middleman — skip him. **Second:** if your threat model puts prompt injection high on the list, remember that Lowly deliberately reads untrusted content from the open web on a schedule. The isolation from the shared brain mitigates that, but doesn't eliminate it. **Third:** if what you actually want is the social-channel half (filtered LinkedIn notifications, DM consolidation) and you don't care about RSS newsfeeds at all, jump to [Ch 11](11-lowly-worm-social.md) — though even then the core Lowly deployed here is still the plumbing that the extension sits on top of.

## What success looks like

A curated, personalized daily news feed that lives in a single Telegram thread, costs effectively nothing beyond your existing LLM subscription, and gets sharper over time.

Concretely, the morning edition is:

- **Ranked and deduplicated** against WSJ, NYT, WaPo, Slate, and Google News (plus whatever else you drop into the feed list in `fetch-and-rank.py`).
- **Grouped by topic category** — 🤖 AI & Tech, 💰 Economics, 🌍 World, 🏛️ US Policy, 📋 Also Noted, and so on. Emoji category headers make the thread scannable at a glance.
- **Capped at 15-20 items total.** The preference model decides what makes the cut, and below-the-fold items get dropped rather than padded.
- **Accompanied by inline 👍 / 👎 / 📖 buttons** on each item — tap 👍 or 👎 to train the preference model, tap 📖 to get a one-paragraph expansion of that specific article.

The morning edition is not trying to be comprehensive. It's trying to be the five-minute version of what you would have spent forty minutes finding on your own.

## Making your newsfeed actually useful — the preference learning story

A morning digest from a static topic list is *okay*. A morning digest from a list that learns what you care about, with no manual tuning, is *genuinely useful*. This section is about how the second thing works, and why it's the hero of the chapter rather than an afterthought.

The basic idea: **coarse preferences get hand-tuned; fine preferences get learned from your feedback via a cheap LLM judge.**

### The coarse model

The seed in `manifest.json` has a topic vocabulary — about ten broad categories (AI, economics, international politics, US domestic policy, tech, markets, science, business, regulation, startups), each with ~15 keyword terms. When `fetch-and-rank.py` pulls an article, it scores the article against the vocabulary and assigns a topic (or marks it `other`). The ranking formula then uses `topic_weight × source_weight × recency × diversity_bonus`.

Topic and source weights start at 1.0 and shift from there. Thumbs-up on an item: topic weight ×1.1, source weight ×1.05. Thumbs-down: topic ×0.85, source ×0.95. Clamp to `[0.1, 5.0]` to prevent runaway values. Over a few weeks of thumbing, the weights settle into a shape that matches what you actually read.

If you stopped here, you'd have a decent personalized feed — but it would only learn at the topic level. Thumbs-down on "AI safety" can't distinguish "AI safety from a researcher I trust" from "AI safety from a pundit I don't" — both are "AI," both get the same weight change. That's the limit of a coarse-only model, and it's why the fine model below is load-bearing rather than optional.

### The fine model, via an LLM judge

The interesting half is what happens on a thumbs-down. `update-preferences.py` runs nightly, reads the day's `engagement.jsonl`, and for every thumbs-down it invokes a small `agents.shared.llm.infer()` call:

> *Here's the article title, headline, and source. The reader marked this as irrelevant. In one sentence, why? Then output a subtopic tag from this list, or propose a new one.*

The judge's answer gets written to a secondary weight table — not at topic level, but at *subtopic* level. So "AI safety (researcher)" and "AI safety (pundit)" become separate keys with their own weights, and the next day's ranking uses both. Over time the subtopic weights get richer than the topic weights, and the digest gets noticeably sharper.

The magic is that **you don't have to know the subtopic taxonomy up front.** The judge proposes new tags as it sees new kinds of thumbs-downs, and the table grows organically. After a few weeks I had subtopic tags like "AI-safety-from-pundit", "markets-daily-noise", "celebrity-tech-gossip", and "thought-leader-post-without-substance" — each of which got filtered down over time. None of those were in my original vocabulary; the judge wrote them all.

**Cost: effectively zero on top of the existing LLM subscription.** `agents.shared.llm.infer()` is a thin wrapper over `codex`, which rides the ChatGPT Plus subscription I'm already paying for. No per-token billing, no per-call cost, just a small slice of a flat-rate subscription. The judge fires per thumbs-down event, there are usually 2-5 of them a day, and each call is a tiny structured-output request. If you rewrite `llm.py` to use a pay-per-token API key instead of a subscription, the math changes from "free" to "pennies per day" — see [Ch 06 — Infra setup](06-infra-setup.md) for how that swap works.

The preference model lives in `preferences/model.json` and is portable — reset it by deleting the file and `deploy.py` re-seeds from the manifest.

This coarse-hand-tuned + fine-LLM-judge-learned pattern generalizes to any feed you want to rank against a preference model. I'm documenting it in the main body of the chapter — not in an appendix — because it is the non-obvious part of what makes Lowly Worm feel genuinely valuable in practice. Without it, the morning edition is a generic RSS digest. With it, it's a personal newspaper that gets more personal every week.

## Deployment walkthrough

The general seven-step arc from [Ch 08](08-your-first-agent.md) applies. What follows is the Lowly-specific material.

### The pilot-agent context

Before the walkthrough: Lowly was the **pilot agent** for the whole Clawford liberation. When the time came to test whether the fleet could actually live without OpenClaw — without the in-container LLM cron runtime, without the 600-second cron ceiling, without the gateway-mediated exec layer — Lowly was the first agent to make the jump. He was chosen because he was the simplest: a single input (RSS + LinkedIn), a deterministic ranking pass, an LLM composition step, and a Telegram delivery. Every failure mode the migration hit, hit him first.

Two of those failure modes became load-bearing patterns for the rest of the fleet:

1. **The 600-second LLM cron timeout** that killed the first migration attempt. Pre-liberation, `morning-edition` was a single LLM cron that called `fetch-and-rank.py`, then composed extended headlines for 15-20 items, then wrote two output files, all inside one session. When the feed was slow or LinkedIn was misbehaving, the whole pipeline blew through the 600-second budget and got killed mid-compose. The symptom was "no morning digest today, but also no error message — just a gap." The fix was the compose/deliver split (described below) plus a host-cron wrapper instead of an in-container LLM cron.
2. **The sys.path shim for shared library imports.** The migration landed the first three modules under `agents/shared/` (`telegram.py`, `google_oauth.py`, `llm.py`) before the deploy tool had caught up with the sync-to-workspace step. Lowly's scripts needed to import them immediately and there was no way to do that without a shim. Commit `3e4d6e3` on 2026-04-14 added a `sys.path` shim that finds the first ancestor directory containing `agents/shared/` and prepends it; every subsequent agent inherited the same shim. If you're writing a new agent script today, that's the shim you're using, and it exists because Lowly needed it on 2026-04-14.

Neither of these is visible in the chapter as it reads today, but both shaped what the deployment walkthrough actually does. The compose/deliver split is now "the way crons work." The shim is now "how shared modules get imported." Both were Lowly's scar tissue first.

### The bot and the token

Create Lowly's Telegram bot via @BotFather and save the token in `~/clawford/.env` as `NEWSDIGEST_BOT_TOKEN=...`. Unlike Mr Fixit (whose token is the fleet's default `TELEGRAM_BOT_TOKEN`), Lowly has his own named bot token because he only sends — he doesn't need to receive free-text messages, and the `callback_query` updates from inline button taps are handled separately by the engagement poller.

### The cron surface

Lowly's crons are a mix of two that call the LLM and two that don't:

| Cron | Schedule (UTC) | Kind | What it does |
|---|---|---|---|
| `news-digest-morning-edition` | `30 10 * * *` | LLM | Runs `fetch-and-rank.py`, which fetches all configured RSS feeds and ranks everything against `preferences/model.json`. Then calls `morning-edition.py`, which uses `agents.shared.llm.infer()` to write a one-sentence extended headline for each of the top 15-20 items, groups them by topic category, and writes `cache/morning-items.json` (structured) + `cache/morning-brief-ready.txt` (plain-text fallback). **Does not deliver** — the fleet delivery cron picks up the file. |
| `news-digest-preference-update` | `0 23 * * *` | LLM | Runs `update-preferences.py`, which reads the day's engagement signals from `preferences/engagement.jsonl`, invokes `agents.shared.llm.infer()` once per thumbs-down for the LLM judge, and updates both the topic-level and subtopic-level weights in `preferences/model.json`. Silent on no-op days. |
| `news-digest-engagement-poll` | `*/5 * * * *` | Pure Python | Runs `engagement-poller.py`, which reads the agent's session transcript files, extracts `/like N` / `/dislike N` / `/more N` signals (plus inline-keyboard `callback_query` events), and appends them to `preferences/engagement.jsonl`. No LLM involvement. |
| `linkedin-keepalive` | `0 */6 * * *` | Pure Python | Runs `linkedin-keepalive.py`, which launches a headless Playwright browser against the persistent LinkedIn profile every six hours to keep the session warm. Only relevant if you deploy [Ch 11](11-lowly-worm-social.md); leave it unregistered otherwise. |

Plus the fleet-wide `morning-fleet-deliver-host.sh` from [Ch 09 — Mr Fixit](09-mr-fixit.md), which at `0 12 * * *` reads `cache/morning-items.json`, chunks it into per-item Telegram messages with inline 👍/👎/📖 buttons, and posts them via `NEWSDIGEST_BOT_TOKEN`.

All four Lowly-specific crons run from the host crontab, declared in `ops/scripts/install-host-cron.sh`. None of them go through a gateway container or a platform-cron dispatcher. The "LLM" column in the table means the cron calls `agents.shared.llm.infer()` internally — it does not mean the cron runs *inside* an LLM session.

### The compose/deliver split

The shape of the morning edition is worth understanding as a general pattern: **the LLM composes, the host delivers.** `news-digest-morning-edition` calls an LLM internally during the compose step, but the cron itself is a pure Python process. It fetches, ranks, selects, composes extended headlines via `llm.infer()`, and writes a structured items file. A separate host cron (`morning-fleet-deliver`) reads that file and posts the messages. They are two separate processes, they communicate through a file on disk, and they can fail independently. If `morning-edition` times out mid-compose, `morning-items.json` is either complete from a previous day (reader sees yesterday's digest — annoying but not broken) or missing (reader sees no digest and Mr Fixit's morning-status alerts catch the gap). If the host delivery cron crashes, the items file is still on disk and you can re-deliver manually. Neither side has to know the other side's concerns.

This split is also why `morning-edition` fires at `30 10 * * *` and `morning-fleet-deliver` fires at `0 12 * * *` — 90 minutes of slack. Lowly's ranking script regularly takes 3-8 minutes, and the first iteration had the two crons only 10 minutes apart, which meant a slow ranking run occasionally missed the delivery window. Ninety minutes is wildly generous, but it's free — nothing else is waiting on the file.

Every subsequent morning-brief agent in the fleet — Mistress Mouse's `morning-briefing`, Sergeant Murphy's `morning-meeting-brief`, Hilda's `delivery-digest`, Huckle Cat's `morning-relationship-nudge`, Mr Fixit's `morning-status` — uses this exact pattern. Compose into `cache/morning-brief-ready.txt`, let `morning-fleet-deliver` pick it up at 12:00 UTC. Lowly was the first to ship it.

### Smoke test

After `python3 agents/shared/deploy.py news-digest --yes-updates` and `install-host-cron.sh`, run the morning-edition cron manually to verify the whole compose chain works:

```bash
ssh openclaw@<vps> "~/repo/ops/scripts/news-digest-morning-edition-host.sh"
ssh openclaw@<vps> "cat ~/.clawford/news-digest-workspace/cache/morning-items.json | python3 -m json.tool | head -40"
```

You should see a JSON array of 15-20 items, each with `num`, `category`, `extended_headline`, `title`, `url`, `source_label`, and `topics`. If `morning-items.json` doesn't exist or is empty, the compose chain either failed or the LLM call timed out — check `~/.clawford/logs/news-digest-morning-edition-host.log` for the exit code and any traceback.

For the preference model, fire `update-preferences.py` manually and confirm it runs without error:

```bash
ssh openclaw@<vps> "/usr/bin/python3 ~/.clawford/news-digest-workspace/scripts/update-preferences.py --dry-run"
```

On a fresh deploy there's no engagement history yet, so the script should report "0 events processed, no weight updates." That's the healthy no-op shape.

## Pitfalls you'll hit

> 🧨 **Pitfall.** `/like 1` with a space is not tappable on Telegram. **Why:** Telegram auto-detects slash commands and renders `/like` as a blue clickable link with `1` as a separate argument. When the user taps the link, Telegram sends *only* `/like` to the bot — the argument is stripped. Per-item feedback breaks silently. I hit this at 5 AM on 2026-04-13 — tapped `/like` on an item, Lowly replied "I got the like, but I need the item number," and I stared at it for a full minute before realizing the number was never sent. **How to avoid:** use **inline keyboard buttons with `callback_data`** (the preferred UX — `{"text": "👍 like", "callback_data": "like:2"}`), or fall back to underscore commands (`/like_1`) when you can't attach a `reply_markup`. Both are parsed by `engagement-poller.py`. Never ship `/like N` with a space — it *looks* clickable and isn't.

> 🧨 **Pitfall.** Asking the LLM to log engagement signals to a file. **Why:** LLM agents cannot reliably write files in response to short reactive messages like `/like 3`. I tried four approaches — SOUL instructions, exec scripts, polling, hooks — and all four failed for different reasons. Sometimes the write raced with the cron output, sometimes the agent forgot, sometimes the exec layer prompted for approval on a one-character filename. **How to avoid:** bypass the LLM entirely. `engagement-poller.py` runs as a host cron every 5 minutes, reads the agent's session transcript files, and extracts engagement signals deterministically via regex. The LLM never sees this code path. This is a reusable pattern for any reactive feedback loop an LLM can't reliably handle; see [Ch 07 — Intro to agents](07-intro-to-agents.md) for why the seam belongs here.

> 🧨 **Pitfall.** Same article reposted across five outlets; the digest ships five copies. **Why:** exact-URL deduplication doesn't catch "OpenAI announces X" as reported by WSJ and NYT and Reuters with slightly different canonical URLs. I shipped a morning edition with the same story three times before I noticed. **How to avoid:** dedupe by URL *and* by Jaccard similarity on normalized title trigrams, threshold around 0.6. Plus cross-day dedup via `cache/sent-history.json` (article IDs + normalized titles + timestamps, auto-prune after 3 days) so you don't ship the same story two mornings in a row because it's still trending. `fetch-and-rank.py` handles both.

> 🧨 **Pitfall.** The preference model goes runaway in one direction because nothing in a starved topic ever shows up again. **Why:** a streak of thumbs-downs on one topic drives its weight toward zero, and then the ranking formula produces a score low enough that nothing in that topic ever lands above the cut line, so nothing in that topic ever gets thumbs-up'd either. The topic is effectively dead and won't recover on its own. **How to avoid:** `update-preferences.py` clamps all weights to `[0.1, 5.0]` — that floor matters. If you rewrite the weight-update math, preserve the clamp. A separate safeguard: the diversity bonus in the ranking formula forces at least one item from each major topic into the morning edition regardless of weight, so a starved topic still gets a weekly audition. If you disable that, a bad run of thumbs-downs can quietly kill a category forever.

> 🧨 **Pitfall.** `morning-edition` and `morning-fleet-deliver` scheduled too close together. **Why:** Lowly's ranking script regularly takes 3-8 minutes, sometimes longer if LinkedIn is slow or a feed redirects through Google News's RSS decoder. If the compose cron fires at 11:55 and the deliver cron fires at 12:00, a slow run misses the delivery window and the digest either ships partial or not at all. **How to avoid:** leave the default 90-minute slack (`30 10 * * *` compose / `0 12 * * *` deliver). If you need to move either one, keep the gap at 60+ minutes. The file sits on disk in between; nothing else is waiting on it.

> 🧨 **Pitfall.** Google News RSS URLs don't resolve from a datacenter IP. **Why:** Google News RSS feeds (`news.google.com/rss/articles/CBMi...`) wrap article URLs in a base64/protobuf redirect that won't decode via plain HTTP from a VPS. The canonical URL comes back as a Google News redirect, not the underlying article, and the dedup logic can't tell two items with different redirect tokens apart. **How to avoid:** use the `googlenewsdecoder` PyPI package to decode the wrapped URL before hashing it for dedup. `fetch-and-rank.py` already calls it; the pitfall is for anyone extending the feed list to new Google News queries without knowing the decoder is load-bearing.

## See also

- [Ch 11 — Lowly Worm: social channel extension](11-lowly-worm-social.md) — the optional extension that adds LinkedIn (or any other social channel) notifications and DM consolidation on top of the core newsfeed. Read only if you care about the social half.
- [Ch 08 — Your first agent](08-your-first-agent.md) — the general seven-step deploy arc Lowly inherits from.
- [Ch 09 — Mr Fixit](09-mr-fixit.md) — the host-cron install pattern, the fleet-health story, and the `morning-fleet-deliver-host.sh` cron that handles Lowly's delivery.
- [Ch 06 — Infra setup](06-infra-setup.md) — the deploy tool, the Telegram gateway, the shared library `llm.py` module the judge call rides on, and the `install-host-cron.sh` installer.
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the LLM-vs-deterministic seam that Lowly's compose/deliver split exemplifies, and the script contract that `engagement-poller.py` follows.
- [`agents/news-digest/manifest.json.example`](../agents/news-digest/manifest.json.example) — the full preference model seed (topic vocabulary with ~10 categories × ~15 keywords each) plus cron and script declarations.
- [`agents/shared/llm.py`](../agents/shared/llm.py) — the LLM backend wrapper Lowly's judge uses. Swap it out at this one file if you want a different provider.
