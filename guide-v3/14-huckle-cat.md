# Huckle Cat 🐱🤝 — the connector agent

*Last updated: 2026-04-22 · Reading time: ~60 min · Difficulty: hard*

> **TL;DR.** Huckle Cat is the relationship agent — the one that inverts the usual shape of a Clawford agent. Instead of wrapping a single external API the way Mr Fixit wraps the fleet's own heartbeat or Mistress Mouse wraps Google Calendar, Huckle Cat is built **around the shared brain itself**. His input is six disparate data sources (Gmail, Google Calendar, Google Contacts, Google Messages, meeting transcripts, and Workflowy) and his output is a relationship intelligence layer: ~280 people files in the brain with names, emails, phones, circles, last-interaction timestamps, enriched context notes, and facts pulled from email signatures. He composes a morning relationship nudge at 5 AM PT (overdue / approaching / healthy), triages a shared notes inbox twice a day, and keeps `last_interaction` fresh via a daily re-mining pass. A second parallel brain under `self/` — four layers synthesised from the operator's career archives — feeds a [cold-recruiter drafting path](#the-professional-brain) that detects unknown ATS senders, scores a composite fit across domain / level / function dimensions, and drafts replies that read like the operator wrote them. He was the last agent in the fleet to deploy, and he is the only one where the [mining pipeline](#the-mining-pipeline) runs **before** the first cron fires — by design.

## Meet the agent

Huckle Cat is the Richard Scarry character — an earnest cat kid who knows everybody in Busytown and remembers what they're up to. In a Clawford fleet, his job is three things:

1. **Relationship memory.** Maintain a people directory in the shared brain with every human the operator actually interacts with, keyed by email or phone, enriched with circle (family / work / friend / acquaintance), relationship type, context notes, recent interactions, and durable facts. The directory is the input to every other relationship-aware behavior in the fleet.

2. **Morning relationship nudge.** At 5 AM PT, compose a Telegram brief grouped into *overdue* (people the operator should reach out to), *approaching* (people coming up on their expected cadence), and *healthy* (people in good standing, mostly suppressed). The brief reads like a relationship-manager end-of-quarter review, except it fires every morning.

3. **Notes triage.** Read the shared `notes/inbox.md` (a free-form append-only inbox that any agent — or the operator — can write to), classify each new note with an LLM into `{fact, commitment, task, shopping, unclear}`, and surface a twice-daily Telegram digest with inline `/confirm N` and `/dismiss N` commands to route confirmed items into the right place in the brain.

The cleanest way to think about Huckle Cat is: **he is the only agent that reads every other agent's output and writes to the brain accordingly.** Mr Fixit is the only agent that watches the fleet. Huckle Cat is the only agent that watches the *operator's relationships* across every surface the fleet touches.

## Why you'd want one

- You have two or more years of relationship data scattered across Gmail, your calendar, your contacts app, a messaging app, and meeting transcripts, and no single app combines them.
- You want a daily 5 AM PT brief that says "you haven't messaged X in three weeks and you usually message X every two" — not a CRM, just a relationship cadence nudge based on actual interaction history.
- You want a shared brain that other agents can read from — a commitment written by [Sergeant Murphy](13-sergeant-murphy.md) can pick up the attendee's relationship context from Huckle Cat's people files, rather than re-computing it.
- You want an inbox-triage pattern for the operator's own notes and for agent-generated observations, with confirmation-before-write semantics that mirror Sergeant Murphy's commitment flow.

## Why you might skip this one

- You don't have a shared-brain architecture in the first place. Huckle Cat's output is useless without a place to write the people directory that other agents will actually read.
- You're not willing to run the mining pipeline and do the human review pass before the first cron fires. Huckle Cat deployed with an empty brain is a relationship-nudge agent with nothing to nudge about — he is essentially a cold-start problem until the mining pipeline lands the initial seed.
- Your relationship data is entirely inside a single tool (Slack, or a work Gmail, or a CRM). Huckle Cat's value is in the *fanout* across seven sources; reduce to one source and a simple script will do the job without needing an agent at all.
- You don't want LLM classification on your notes inbox. The notes-triage side of Huckle Cat is optional and can be disabled, but the morning nudge still depends on the mining pipeline's LLM enrichment pass, which is the only LLM call in the agent by default.

## What makes this agent hard

Three things, in order of operational weight.

**Brain-first deployment is not how the other agents work.** Every other agent in the fleet is a fetch-and-process loop: fire the cron, hit an external API, stage some output, send a Telegram message. Huckle Cat's first cron fires against a *pre-seeded brain* — the mining pipeline has already run end-to-end before the first host cron is even registered. If you deploy Huckle Cat the way you deploy Mr Fixit (drop in the scripts, register the crons, walk away), the first morning nudge composes a brief against an empty people directory and produces an empty or near-empty output for a week. The mining pipeline is not optional scaffolding. It is step 0.

**Seven-source fanout is a data-quality problem, not an engineering problem.** Each of the seven miners produces output of different shapes, different fidelity, different levels of trust. Gmail gives you rich history but also gives you every newsletter you ever subscribed to. Google Contacts gives you high-signal saved contacts and also 1,900 auto-saved "other contacts" from stray CCs. Meeting transcripts give you participants but have to be matched back to calendar attendees via fuzzy name+title logic. Workflowy gives you ~3,000 meeting nodes but many of them reference people by first name only. The aggregator's job is to dedupe, score, merge aliases, and produce a coherent per-person record — and the only way to verify that it worked is a human review pass before finalization. That review pass is a real chunk of operator time on first deploy, and it cannot be skipped.

**Stale `last_interaction` timestamps are the failure mode you will actually hit.** The mining pipeline runs once at deploy time and stamps every contact's `last_interaction` field with the most recent message found in the scan. If nothing updates those timestamps afterwards, the morning nudge will, within days, start flagging contacts the operator has interacted with since — which is the textbook way to lose operator trust in a relationship-nudge agent. Two crons keep the dates fresh: `daily-refresh` (landed 2026-04-14) re-mines a rolling Gmail-inbound / Google Calendar / Google Messages window once a day, and `gmail-sent-mine` (landed 2026-04-21) walks the Sent folder every two hours to catch outbound email — the dominant channel for colleagues where the Messages scraper sees nothing. See [§ The stale-dates bug and the refresh crons](#the-stale-dates-bug-and-the-refresh-crons).

## The mining pipeline

The mining pipeline is Huckle Cat's step 0 — the thing that runs before the first cron fires, on the operator's local machine, with a human review pass in the middle, to produce the seed people directory that the rest of the agent depends on. It has three phases: **mine**, **aggregate + enrich**, and **review + finalize**.

### Step 1 — Mine

Seven miners, each scoped to one data source, each producing a flat JSON output under `cache/` (gitignored):

| Miner | Source | What it extracts |
|-------|--------|------------------|
| `gmail-mine.py` | Gmail (2yr history) | Sender/recipient/timestamp per message; signature block per sender (structured facts: title, company, phone, LinkedIn) |
| `gcal-mine.py` | Google Calendar (all events) | Attendees + dates |
| `contacts-mine.py` | Google Contacts | Both `people.connections.list` (2500+ saved) and `otherContacts.list` (1900+ auto-saved) |
| `transcripts-mine.py` | MCP transcription provider | Session metadata + participant lists; cross-references by first-name + title against calendar attendees |
| `gmessages-mine.py` | Google Messages (DevTools) | DOM-scraped conversation list + metadata — see [§ The Google Messages + DevTools story](#the-google-messages-devtools-story) |
| `workflowy-read.py` | Workflowy (API export) | ~3000 people extracted from ~16K meeting nodes in the contact cache |

The miners run in parallel where possible. Each has a sampling cap (e.g., gmail-mine caps at N messages per sender to keep the cost bounded) and writes its output to `cache/mined-{source}.json`.

### Step 2 — Aggregate + LLM-enrich

The aggregator (`contact-aggregator.py`, ~1200 lines) does the hard part:

- **Email-based deduplication** across sources — the same person might appear in Gmail, Contacts, and calendar attendees with slightly different name spellings, and all three records need to collapse to one.
- **Fuzzy name matching for transcript-only names** — meeting transcripts identify participants by first name (and sometimes title), and matching those back to calendar attendees requires a tolerant matcher that handles "Alex K." = "Alexandra Khan" when the calendar event has both of them.
- **Importance scoring** — sent messages are weighted heavier than received messages, newsletters are down-weighted, Google auto-saved contacts start from a lower base score than explicitly saved ones.
- **Auto-circle assignment** — domain heuristics (family-domain email → family circle, work-domain → work, known-friend-domain → friend, everything else → acquaintance) combined with frequency and recency.
- **Alias merge + whitespace normalization + name-subset dedup** — "J Smith" folds into "John Smith" if the email addresses match, "Alex" folds into "Alex Khan" if the phone numbers match, and so on.

Then the LLM enrichment pass (`llm-enrich.py`) runs one call per person, with `sqrt`-scaled message sampling so that a top-tier relationship gets a rich context window and a low-tier acquaintance gets a minimal one. The output is relationship_type, tone, context_notes, and key_topics per person. The enrichment is I/O-bound and parallelizable; the default runs 10 workers. Cost is roughly 1 cent per person on a small model, so ~`$2-4` for a 280-person seed is what the first run actually costs in practice.

### Step 3 — Review + finalize

This is the phase that is easy to skip and the one you absolutely must not skip. `contact-aggregator.py` does not write directly to the people directory. Instead, it writes a **tiered review markdown** — a table of every candidate contact grouped by score tier, with the inferred circle, relationship type, and rationale for each. The operator reads that review file in an editor, deletes marketing contacts, corrects obvious mis-classifications, merges duplicates the aggregator missed, adjusts circles, and then re-runs the aggregator with `--finalize` to commit the seeds to `~/Dropbox/clawford-brain/people/{email-slug}.md`.

The review pass is the only thing standing between the seven-source fanout and a people directory that the operator can actually trust. It is also the only manual step in Huckle Cat's entire deployment. Budget 30–60 minutes for it the first time.

### Why the pipeline runs on the local machine, not on the VPS

Three reasons:

1. **LLM cost visibility.** A local run makes the cost of the enrichment pass show up in the local LLM bill, where the operator can see it in real time. A VPS run would bury it in the monthly usage rollup.
2. **Review-file ergonomics.** The review markdown is meant to be read and edited in a proper editor. Editing a 280-row markdown table over `ssh` + `vim` is possible but unpleasant; editing it locally in VS Code is what the operator actually does.
3. **Iteration speed.** The first deploy involves 3–5 runs of the aggregator with small config tweaks (circle thresholds, acquisition cutoffs, family-map overrides). Running that loop locally is ~10x faster than running it over ssh.

The output — the finalized `people/` and `facts/` directories — lives on Dropbox and syncs to the VPS, so the VPS crons pick up the seeded brain without ever running the mining pipeline themselves.

## The Google Messages + DevTools story

Google Messages Web has no API, no bulk export, and no official scraping path. But the operator's iPhone-to-Android messaging history is sitting there in the browser, and it's one of the seven sources that matters most for relationship-cadence tracking because SMS is often where the closest relationships actually live.

The solution (a four-commit series on 2026-04-14) is a **self-contained JavaScript snippet** that walks every conversation thread in the Google Messages Web DOM, extracts message metadata (sender, recipient, timestamp), and copies the result to the clipboard as JSON. No extension installation, no permissions dialog, no persistent access token — just paste the snippet into Chrome DevTools, run it, and the clipboard contains the full conversation list.

The wrapper scripts around this:

- `gmessages-auth.py` — opens a Camoufox browser against `messages.google.com/web`, walks the operator through pairing (phone displays a QR code, laptop takes a screenshot, operator scans). Session persists in a Camoufox profile directory.
- `gmessages-mine.py` — runs the DevTools snippet against the authenticated session, scroll-paginates through the conversation list (not capped by DOM height — scroll triggers lazy-load), handles both absolute timestamps (`"Mar 15, 2:35 PM"`) and relative timestamps (`"Today 2:35 PM"`, `"Yesterday 4:20 PM"`), and 2-digit-year disambiguation context-aware rather than naive.
- A **graceful selector fallback** so that if Google changes the HTML structure, the miner degrades to a "this source is broken, surface that in the heartbeat" state instead of crashing the whole aggregator run.

The pattern generalizes: if you have an important data source with no API, a hand-rolled DevTools snippet run from a persistent-profile browser gets you 80% of the way to "this data is now part of the brain." It is the same shape as a full Tier 3 auth flow, minus the anti-bot armor and the MFA automation.

## The stale-dates bug and the refresh crons

Huckle Cat deployed on 2026-04-12 with 279 seeded people files. Each people file had a `last_interaction` field stamped with the most recent interaction found during the one-time mining run. That was the operator state on day 1.

Two days later, the morning relationship nudge started flagging contacts the operator had definitely spoken to since April 12. The nudge for `\[person X\]` said "last interaction 6 days ago, overdue," and the operator had texted `\[person X\]` that morning. The nudge for `\[person Y\]` said "last interaction 9 days ago, approaching threshold," and the operator had a calendar event with `\[person Y\]` the previous afternoon.

The root cause was simple in retrospect: **the mining pipeline ran once, and the people files were frozen from that moment forward.** The morning relationship nudge was reading a snapshot from April 12 every day, and every day the snapshot was more wrong.

The 2026-04-14 fix introduced `daily-refresh.py`, a host cron at `0 10 UTC` (3:00 AM PT) that re-mines a 14-day rolling window from Gmail (inbound), Google Calendar, and Google Messages, and updates the `last_interaction` field in the affected people files in place. The morning nudge at `30 10 UTC` then reads the refreshed values and composes against the real state of things.

A week later the agent tripped on a narrower version of the same class of bug. The operator had emailed two colleagues the day before and Huckle flagged both as overdue the next morning. `daily-refresh` re-mines Gmail *inbound* — replies from those colleagues would have updated the dates — but nobody had replied yet. The operator's outbound send wasn't a signal the system was scanning at all. For relationships where email is the dominant channel (colleagues, extended family who don't text), a full week of outbound conversation could produce zero `last_interaction` updates.

The 2026-04-21 follow-up fix introduced `gmail-sent-mine.py`, a second host cron at `15 */2 UTC` (every two hours, offset from `gmessages-mine`) that walks the operator's Sent folder, maps each recipient email to a people slug via the same `build_email_to_slug_map` helper the other miners use, and stamps `last_interaction` with the send date. Max-merged — a fresher existing date (from Krisp, from `gmessages-mine`, from a `/checkin`) is never rewound by an older sent message. The cursor lives at `cache/gmail-sent-mine-cursor.json`; the bootstrap window is 90 days. The per-run stats land at `cache/mined-gmail-sent.json` with an `unmatched_samples` field that makes it easy to see which Sent-folder recipients don't have a people file yet.

A parallel fix in the same commit wired `handle_nudge_action("done")` to stamp `last_interaction = today` when the operator presses ✅ done on a morning-nudge entry. Before, the button only wrote to `snoozes.json`, which hides the contact for thirty days but leaves the cadence clock stuck. After, pressing ✅ done is an explicit "I contacted them today" signal, max-merged against the existing date. The operator gets the quick manual path, the miner gets the passive path, and together they close the gap that the inbound-only `daily-refresh` left open.

Four second-order details fell out:

1. **Meeting transcript attendee-count cap.** The transcript source can also update `last_interaction` — a 1:1 meeting transcript with person X is strong evidence the operator interacted with person X that day. But a 12-attendee all-hands meeting is not evidence of a real interaction with any individual attendee. The fix caps attendee-based refresh at 6 participants; meetings larger than that don't stamp `last_interaction` on anybody. This prevents the operator's "overdue" list from being silently cleared by their weekly all-hands.

2. **Cron ordering is load-bearing.** `gmessages-mine` runs at `0 */2 UTC` (writes `cache/mined-gmessages.json`). `gmail-sent-mine` runs at `15 */2 UTC` (writes direct to people files with max-merge; the 15-min offset keeps the two Google OAuth flows from firing simultaneously). `daily-refresh` runs at `0 10 UTC` (reads `mined-gmessages.json`, updates people files). `morning-relationship-nudge` runs at `30 10 UTC` (reads updated people files, composes brief, writes `cache/morning-brief-ready.txt` for fleet-deliver at `0 12 UTC`). The 30-minute gap between refresh and nudge is slack for the refresh cron — if you make them adjacent, a slow refresh run produces a nudge against stale data. Keep the slack.

3. **Cache freshness is not verified.** `morning-relationship-nudge` does *not* currently check whether `cache/mined-gmessages.json` is fresh before composing the brief. If `gmessages-mine` fails silently for a week, the nudge will quietly start using 7-day-old Google Messages data. This is a known gap — see [Pitfalls](#pitfalls).

4. **Max-merge is the contract.** Every signal that writes `last_interaction` — `daily-refresh`, `gmail-sent-mine`, the ✅ done button, `/checkin` — goes through `daily_refresh.update_last_interaction`, which reads the existing date, keeps whichever is newer, and atomic-writes the result. This means the crons can run in any order, re-run after partial failures, and produce the same final state. The cursor advance in `gmail-sent-mine` is the only piece where ordering matters, and it only persists on a successful run.

## Current state

As of 2026-04-21, Huckle Cat runs eleven host crons off `~/.clawford/connector-workspace/`.

**Host cron surface.** Registered via `ops/scripts/install-host-cron.sh`:

| Cron | Schedule (UTC) | What it does |
|------|----------------|--------------|
| `gmessages-mine` | `0 */2 * * *` | Camoufox + DevTools JS snippet, scrapes Google Messages Web, writes `cache/mined-gmessages.json` |
| `gmail-sent-mine` | `15 */2 * * *` | Walks the Gmail Sent folder (cursor-based), maps To/Cc recipients to people slugs, max-merges `last_interaction = message_date` directly onto people files. Covers the outbound-email signal `daily-refresh` misses. 15-min offset from `gmessages-mine` keeps the two Google OAuth flows from stacking |
| `daily-refresh` | `0 10 * * *` | Re-mines 14-day Gmail-inbound + GCal + GMessages window; updates `last_interaction` in people files; writes `cache/upcoming-meetings.json` |
| `morning-relationship-nudge` | `30 10 * * *` | Calls `people-scan.py`, groups by overdue / approaching / healthy, writes `cache/morning-brief-ready.txt` for fleet-deliver at `0 12 UTC` |
| `notes-triage-alert` | `0 8,20 * * *` | Reads `notes/inbox.md`, LLM-classifies new entries into `{fact, commitment, task, shopping, unclear}`, sends twice-daily Telegram digest with inline `/confirm N` + `/dismiss N` |
| `birthday-miner` | `0 6 * * 0` | Weekly: scans Google Calendar for recurring birthday events, resolves event titles to person slugs, upserts identity facts so `/people [name]` surfaces a birthday line |
| `inbox-triage` | `*/30 * * * *` | Scans recent inbound threads, filters service senders + self, queues known-sender threads for drafting in `cache/triage-queue.json` |
| `auto-compose` | `5,35 * * * *` | Drains the triage queue — one draft (or FYI) per thread, idempotent via processed-log, caps at 5 threads per run |
| `gmail-watch-renew` | `0 7 * * *` | Daily re-call of `users.watch()` to keep the Pub/Sub push path alive (the real-time triage listener is a sibling story) |
| `gmail-facts-mine` | `0 4,10,16,22 * * *` | Mines durable facts from the rolling inbound + sent window; writes upserts into `brain/facts/YYYY-MM.md` |
| `workflowy-facts-mine` | `30 4,10,16,22 * * *` | Pulls the Workflowy export, extracts facts from nodes that mention a known-person full-name, upserts into `brain/facts/` |

**Workspace layout** under `~/.clawford/connector-workspace/`:

```
SOUL.md                   # immutable
IDENTITY.md               # immutable
TOOLS.md, AGENTS.md       # durable identity
USER.md                   # operator profile (gitignored)
HEARTBEAT.md              # status file
MEMORY.md                 # persistent notes
token.json                # Google OAuth (gitignored)
credentials.json          # Google OAuth client (gitignored)
cache/
  mined-gmessages.json       # gmessages-mine output, read by daily-refresh
  mined-gmail-sent.json      # gmail-sent-mine per-run summary (stats + unmatched-sample)
  gmail-sent-mine-cursor.json # gmail-sent-mine cursor (last_internalDate + last_run_at)
  upcoming-meetings.json     # daily-refresh output, read by nudge
  pending-triage.json        # notes-triage pending state
scripts/
  mine/                      # the one-time mining pipeline (7 miners + aggregator + enricher)
  gmessages-auth.py          # Camoufox QR pairing
  gmessages-mine.py          # ongoing Messages scraper
  gmail-sent-mine.py         # ongoing Sent-folder miner (stamps last_interaction direct)
  gmail_sent_mine_lib.py     # cursor I/O + recipient extraction (pure helpers, unit-tested)
  daily-refresh.py           # 14d rolling refresh (owns update_last_interaction)
  people-scan.py             # deterministic scan called by nudge
  morning-relationship-nudge.py  # the orchestrator
  notes-triage-alert.py      # the triage orchestrator
```

**Brain state** lives outside the workspace, in the shared brain on Dropbox — this is the point of Huckle Cat. After the initial mining run plus the correspondence-layer fact import (see below), the operator's shared brain carries `~400` people files under `people/` and `~600` durable facts under `facts/`, every fact tagged with an `audience_scope` that gates which kinds of recipient will see it surface in a draft.

## The conversational surface

The morning nudge and notes-triage are the **outbound** behaviors — crons that fire on a schedule and deliver to Telegram. Huckle Cat's **inbound** surface is the tools manifest in `agents/connector/tools.py` that the inbox daemon loads whenever I message the bot. The daemon is covered end-to-end in [Ch 18 — The inbox](18-the-inbox.md); this section enumerates only what Huckle Cat exposes.

Ten read tools and six producer tools (plus three confirm executors the LLM never sees). The usable commands, as I type them on Telegram:

| Command | What it does | Backing |
|---------|--------------|---------|
| `/people <name>` | Full record for one person — circle, tone, last_interaction, visible facts (birthday, health, relationship context) | `get_person` → `brain.get_person` with first-name fallback |
| `/commitments` | Unified open-commitment view across every agent (Murphy's, Mouse's, Huckle's own) with overdue / approaching flags | `get_commitments` → shells `scripts/commitment-scan.py` |
| `/nudge` | Force the morning relationship scan on-demand (without overwriting the 5 AM PT delivery cache) | `force_nudge` → shells `scripts/people-scan.py` |
| `/triage` | Force notes-inbox triage on-demand | `force_triage` → shells `scripts/notes-triage.py` |
| `/draft <name> [text]` | Compose a check-in (no arg) or reply (with inbound text) using the recipient's facts, tone, and voice profile | `draft_reply` → shells `scripts/draft-compose.py` |
| `/note <text>` | Quick-add to `notes/inbox.md` with propose/confirm buttons | `propose_add_note` → `brain.append_inbox_note` on confirm |
| `/add <name> <circle>` | Create a new person file with frontmatter and duplicate-slug guard | `propose_add_person` → `brain.create_person_file` on confirm |
| `/checkin <name>` | Record that I talked to someone (resets their overdue timer) | `mark_checkin` (direct mutator, no confirm gate) |
| `/dismiss N` | Skip the N-th item in a pending-triage batch | `dismiss_triage_n` |
| free-text | "who haven't I talked to in the family circle?" → LLM combines `get_morning_nudge` + `get_checkin_log` + `get_config_summary` into an answer | Any combination of read tools |

The asymmetry between `mark_checkin` (direct mutator, callable by the LLM without a confirm button) and `propose_add_note` / `propose_add_person` (staged behind a confirm button) is deliberate. A check-in is a reversible low-stakes signal — if the LLM misreads "just talked to Priya" it's a stale `last_interaction` date, which the next `daily-refresh` will correct. A note or a new person file writes to the shared brain's canonical surface; I want my thumb on the send button for those.

The tools resolve shared state through `agents/shared/brain.py`, which grew five helpers in the 2026-04 slash-command-wiring pass to back this surface: `get_person` (with first-name fallback for ambiguous lookups), `list_persons`, `append_inbox_note` (atomic append with ISO timestamp + triaged flag), `create_person_file` (duplicate-slug guard), and `upsert_fact` (idempotent on `(source_agent, subject, idempotency_key)` — used by the birthday miner described below). The helpers are in [Ch 16 — The shared brain](16-shared-brain.md); the tool-manifest pattern is in [Ch 18 — The inbox](18-the-inbox.md).

## Passive ingestion: birthdays without a command

The first cut of the conversational surface had a `/birthday [name]` command. I pruned it during the 2026-04-19 review pass — not because birthdays don't matter but because the command was the wrong shape of answer to the question. If I have to *ask* for someone's birthday, the fact isn't really in the shared brain yet; it's in my head. The pattern I wanted was the reverse: birthdays land as identity facts on the person file, and `/people [name]` surfaces them alongside everything else I might want to know about a contact.

That meant building an ingestion path. `scripts/birthday-miner.py` runs weekly (`0 6 * * 0` UTC) and does three things:

1. **Fetch** — query Google Calendar for events matching a `birthday|b-?day|🎂` regex in the window `-30 days … +365 days` (one recurrence per yearly event, which is all the miner needs).
2. **Resolve** — for each event, extract the owner's name from the title via a regex pipeline: strip to-do prefixes (`Get / Bring / Buy / Order / Remember <X> birthday card`) and generic greetings (`Happy birthday!`), strip relational titles (`Aunt Marcia` → `Marcia`, `Mama Yao` → `Yao`), then either (a) match the `aliases` map in `birthday-aliases.json` (operator-forced), (b) fall back to `brain.get_person` with the first-name-unique heuristic, or (c) skip silently.
3. **Upsert** — call `facts.upsert_fact(subject, category="identity", content=f"Birthday: {date}", idempotency_key="birthday")`. The idempotency key means re-runs are cheap and non-destructive.

The resolver's heuristics are deliberately loose. A typical calendar has eighteen events matching the regex, of which maybe four are parse-clean (`Priya's Birthday`, `Jeanette's birthday`), six are to-do cards (`Get Dad birthday card`), three are relational titles (`Aunt Marcia's birthday`, `Grandma's birthday!`), one or two are joke entries (`OliMom's fake birthday!`), and the rest are ambiguous (`Emily's birthday` when there are three Emilys in people/). The miner gets the parse-clean and relational cases right automatically. For everything else, there's a companion file at `~/.clawford/connector-workspace/birthday-aliases.json`:

```json
{
  "aliases": {
    "Mom": "priya-rivera",
    "Emily": "emily-bruemmer",
    "Grandma": "marcia-rivera"
  },
  "manual_birthdays": {
    "marcia-rivera": "1954-07-27",
    "david-rivera": "1958-12-17"
  }
}
```

The `aliases` map is operator-forced — it short-circuits the heuristic. `manual_birthdays` takes the other path: operator-supplied slug + ISO date, written as a fact directly, bypassing the calendar entirely. Useful for birthdays I know but don't have on any calendar, and useful for birthdays where the calendar shows the next recurrence date (`2026-07-27`) rather than the person's actual birth year (`1954-07-27`). The manual pass runs **first** inside `process_events`, so operator-supplied dates win any collision with calendar-derived ones. I got this ordering wrong on the first pass — calendar pass ran first, wrote `2026-07-27` under `idempotency_key="birthday"`, and then the manual entry with `1954-07-27` was deduped out. The test that now pins the ordering (`test_manual_entry_wins_over_calendar_collision`) exists because of that specific failure.

The broader pattern this instantiates: **passive ingestion over conversational lookup.** Anything that can be derived from the calendar, the email archive, or the message history should flow into the brain as a fact, so the conversational surface stays focused on what I actually want to do (draft a reply, check who's overdue, stage a note). A `/birthday` command would have been another hand-crafted LLM path for something that's really just a fact on a person file. The miner is fifty lines of pure-function orchestration plus a GCal fetch wrapper — half of it is regex hygiene — and it makes the `/people` command carry more for free.

## The correspondence layer

The one-sentence ask from the operator: *start drafting my email replies.* The thirty-five-commit answer is the correspondence layer — a pipeline that reads a Gmail thread, classifies what the inbound message is actually asking for, picks a voice, composes a reply, and stages it as a Gmail draft (never sent). It runs on the same `~/.clawford/connector-workspace/` surface as the nudges but exercises a different slice of the shared brain: `people/`, `facts/`, `voice/`, `commitments/`. The bulk landed on 2026-04-19 and 2026-04-20; a second pass on 2026-04-21 tightened six rough edges reported on a real draft to a former colleague (see [Compose hardening](#compose-hardening)).

### Per-circle voice profiles

A draft to a family member cannot sound like a draft to a VC. The first real problem the correspondence layer had to solve was what "voice" even means when the operator writes to five different kinds of recipient. The answer: `scripts/voice-profile-build.py` reads the last two years of sent mail, groups the operator's messages by the recipient's circle, and extracts a per-circle style fingerprint — opening phrase patterns, closing phrase patterns, typical sentence length, emoji density, signature block.

Five circle profiles came out clean on the first pass: `family-inner`, `family-extended`, `friends-close`, `professional-inner`, `professional-outer`. A sixth — `holiday-card` — was attempted and abandoned. The operator writes to the `holiday-card` circle maybe eight times a year, and eight sent messages is not enough signal to model a voice against; the exclusive-circle sampling guard in `voice-profile-build.py` hard-stops at a minimum-sample floor and the `holiday-card` profile never got built. The drafts for that circle fall back to `family-extended` voice, which is close enough. A sixth guardrail became a sixth rule: if there aren't enough sent messages for a voice, don't pretend there are.

Per-person overrides layer on top. A person file can carry a `voice_overrides` block that points at a specific prior thread ("write to this person the way you wrote on 2026-03-02") or forces a particular register. Useful for the handful of people whose relationship doesn't fit cleanly into a circle — the college friend who works in the operator's industry, the former boss who's now a family friend. The override is read at compose time and mixed into the circle fingerprint before the LLM call.

### The four-step composition loop

The first cut of `draft-compose.py` was a one-shot prompt: *here's the thread, here's the recipient's people file, write a polite reply.* The output was correct, responsive, and useless — it answered every question but proposed nothing, surfaced no operator-specific context, and read like a competent intern drafting on someone else's behalf. Which, technically, it was.

The rewrite broke compose into an explicit four-step loop, each step one LLM call:

1. **Intent.** What is the inbound message actually asking for? The answer is one of a short enum: *needs reply (scheduling)*, *needs reply (decision)*, *needs reply (acknowledgement)*, *FYI only*, *no action*. The classification lives in `inbound_act_lib.py` and was peeled off into its own module precisely so it could be unit-tested without running a compose.
2. **State and gap.** What does the brain already know about the recipient and this thread, and what's missing? Pull their facts filtered by `audience_scope`, pull recent commitments involving them, pull the last two outbound messages. Write a one-paragraph summary of *what I already know* and *what this inbound is adding to it*.
3. **Leverage.** What concrete assets exist for the response? A pending commitment the recipient cares about. A calendar slot the operator could offer. A prior thread that explains the context the inbound missed. The compose model is explicitly told that if it can't identify at least one concrete asset, the reply is probably just an acknowledgement — which is a valid outcome.
4. **Strategy.** Given the intent, the state/gap, and the leverage, what's the shape of the reply? Decide the register (circle voice + per-person overrides), pick the concrete asset to lead with, sketch the key sentences. Only then compose prose.

Four LLM calls per draft isn't free, but the steps are short — the whole chain takes under thirty seconds and costs a few cents. The quality delta vs. the one-shot prompt is the difference between "I received your email" and "I can grab thirty minutes on Thursday afternoon — does 2:00 PT or 3:30 PT work better?"

### The emotional transaction

Three named beats forced the shape of the recipient-modeling step.

A *friend checking in after a rough quarter* wrote the operator a long, personal update. The first draft was correct and professional; it missed the thing the friend actually wanted, which was to feel heard. The fix: every reply now carries an explicit "what emotional outcome does the sender want" field in the strategy step. Gift-givers want appreciation. Advice-givers want usefulness. Catch-up-after-a-gap messages want presence and reciprocity. The LLM is instructed to identify this outcome before writing the reply and to make sure the reply delivers on it.

*The operator's father*, after a long thread about a grandkid learning to read, got a reply that answered every question in the inbound. It read correctly and it was wrong — the thing the operator's father wanted was the operator to engage with the grandkid-reading image, not to volley back a clean answer. Now the voice profile for `family-inner` has an explicit instruction to linger on the emotional content of the inbound before pivoting to logistics. One line in the prompt; noticeable effect on the drafts.

*An external contact requesting a pre-meeting slot* got a reply that said "happy to jump on a call if you're around this week." That is not a proposal. It is a vague gesture in the direction of a proposal, and it puts the scheduling work back on the recipient. The fix went into the prompt as a hard rule.

### The concrete-proposals rule

For scheduling replies, propose at least one specific day plus at least one specific time. Never "happy to if you're around," never "let me know what works for you." If the inbound proposed a window, either accept a specific slot inside that window or counter with a specific slot outside it. If the inbound proposed nothing, pull the operator's availability via `agents/shared/availability.py` (the free-slot calculator reads the calendar index and returns the next N business-hour slots), and propose the top two or three.

The rule is load-bearing enough that it's encoded in the strategy prompt and re-verified at the end of compose:

> ⚠️ **Warning.** A scheduling reply without a specific day+time is not a reply — it's a non-response that costs the recipient another round. If the brain has zero calendar signal for this person (no prior meetings, no availability access), offer three daypart windows ("Tue morning, Thu afternoon, or Fri before lunch") rather than falling back to open-ended language.

The open-ended language is what the model defaults to if the rule isn't in the prompt. It takes one correction to fix and it never stays fixed if the rule drops out — every compose-prompt iteration that tries to trim tokens by removing the specificity hint gets caught the next time the operator reviews a draft and sees the words "happy to" appear.

### Reply-needed triage

Not every inbound thread needs a drafted reply. A newsletter doesn't. A calendar invitation doesn't. A forwarded FYI from a colleague often doesn't. The auto-compose cron runs the four-step loop on every queued thread, but the step-1 intent classifier can emit `reply_needed: false`, at which point the pipeline short-circuits.

When `reply_needed` is false, the outcome is a Telegram message rather than a Gmail draft: `"🐱🤝 FYI from <sender>: <one-line summary>"`. The thread is logged in the processed-log (so the same FYI doesn't re-fire on the next half-hour) and no draft is created.

The triage decision is not a pre-filter — it's an output of the same composition pipeline, made after the model has read the thread and the recipient's context. Pre-filtering inbound on heuristics (sender domain, subject keywords) was the first cut and it got newsletters right and real messages wrong. Letting the model decide *after* reading the content is slower and more accurate; it also lets the model handle ambiguous cases ("this reads like an FYI but the sender is a family member, so surface it to the operator even if no reply is needed").

> 🔦 **Tip.** The Telegram FYI ping is the cheapest way to answer the question "did anything important happen while I was in meetings?" without opening Gmail. The operator's 1:1 with a colleague just shifted by an hour — Huckle has already pinged the summary, no draft needed, no Gmail context-switch.

### Audience scope — the thing that makes drafts read like me

Facts in the brain are not equally sharable. The fact that a family member is in chemotherapy must not appear in a draft to a colleague. The fact that the operator is fundraising must not appear in a draft to a family member who doesn't know yet. Before the correspondence layer, the brain had no concept of audience — facts were simply facts, and the only gate on what appeared in a draft was whichever facts happened to land in the prompt's context window.

The fix: every fact in `brain/facts/YYYY-MM.md` now carries an `audience_scope` field — a list of up to three tags drawn from `{professional, personal, family, friends, academic, financial, legal, genealogy, internal, public}`. A fact tagged `["family"]` surfaces in drafts to family-circle recipients only. A fact tagged `["professional"]` surfaces in drafts to professional-circle recipients only. A fact tagged `["personal", "family"]` is visible to both. Absence of a scope means "visible to all," which is the conservative default for facts imported before the tagging system existed.

The implementation is two-phase. Phase one was a one-time retroactive pass — `scripts/facts-scope-augment.py` batch-classified 257 Huckle-native facts via a single LLM call that returned a JSON map from fact-id to scope tags, and rewrote the fact files in place with the new scope lines. Phase two — the durable pattern — is scope-at-write-time. Every miner that lands a new fact emits an `audience_scope` on the fact dict before `upsert_fact()` writes it. The scope-augment script still exists as a belt-and-suspenders re-tagger for any fact that slips through unscoped, but the intent is that it stays idle.

The interop payoff: a parallel cognitive-exoskeleton project carries the same audience-scope concept on 7,090 facts about 288 subjects. A one-time importer reads that project's SQLite and writes the facts through `upsert_fact()` with the original `audience_scope` preserved, adding 352 pre-scoped facts to the Huckle brain on day one. The tags interoperate without translation — same vocabulary, same semantics — which means the scope-at-write-time rule is now shared across two independent projects.

### Compose hardening

An actual draft to a former colleague on 2026-04-21 — the first non-trivial output the operator reviewed in detail — surfaced six rough edges that shipped in a same-day fix pass. Worth naming each one explicitly; they're the kind of issues that drop out of the test suite but jump off the page when a human reads the output.

- **Sign-off normalization.** The composition schema told the LLM *"no signature block"* while the voice-anchor rule downstream told it to match the history's closing literally. The LLM usually resolved the contradiction by dropping the canonical form ("Best,\nBrian" for the professional-outer circle) and emitting just "the operator" or nothing. The fix: strip the "no signature block" instruction, have `parse_compose_result()` in `compose_lib.py` normalize any trailing name-only or close-variant into the voice profile's canonical `profile_signoff`. A bare "the operator" becomes "Best,\nBrian"; an already-canonical draft is left alone.

- **Dehardwrapping.** LLMs default to wrapping emitted prose at ~68 characters, and `EmailMessage.set_content()` in the MIME builder preserves those line breaks as CRLF. Gmail then rendered them as visible mid-paragraph breaks — a draft that looked like it was pasted from a terminal. The fix: a paragraph-aware unwrap in `parse_compose_result()` collapses hard-wrapped lines within each paragraph (blank lines still separate paragraphs). Bulleted and numbered lists are detected and preserved.

- **Greeting vs. reply-opener disambiguation.** The voice profile carried both a `typical_greeting` ("Hi {name},") and a common pattern ("Opens replies with 'Thanks, {name}.'"). The LLM stacked both — "Hi Jamie,\n\nThanks, Jamie." — two name uses in two lines, unmistakably generated. The fix: the prompt now surfaces thread position and explicitly names the two forms as alternatives. Originating / reopening → greeting. Replying / following_up → reply-opener.

- **Scheduling wired end-to-end.** `draft-compose.py` had accepted `--scheduling-rules` and `--search-window` for weeks, but the cron path (`auto-compose.py`) never passed them. Availability was always empty; the LLM freehanded times against working hours it didn't know. The fix: `auto-compose` now reads `~/.clawford/connector-workspace/scheduling.rules.json`, computes a 14-day window starting tomorrow 9am, and threads both through to `draft-compose`. OPEN SLOTS populate against the real working-hours + blackout + buffer rules.

- **Calendar busy blocks.** The Gmail token already carried `calendar.readonly` scope. Nothing used it. A new `agents/shared/gcal_freebusy.py` helper calls `freebusy().query()` over the search window, merges overlapping intervals across any queried calendars, and passes the result as `--busy-blocks` to `draft-compose`. Proposed times no longer collide with existing meetings. Degrades open on any API failure — freehand scheduling is the fallback, not a crash.

- **Recipient timezone.** Every person record can now carry an optional `timezone:` field (standard IANA name). When it's set and differs from the operator's, OPEN SLOTS render dual-tz: `Tue Apr 22 11:00–12:00 PDT (14:00–15:00 EDT their time)`. The prompt's RECIPIENT block also names the tz so the LLM knows what "after 4 pm" means for them. Absent: single-tz render, same as before.

### Mentioned-but-not-addressed people

A real-world case from the same 2026-04-21 thread: the colleague mentioned her two young children by name in an update. The miner extracted the claim correctly, but the claim's subject ended up attached to the colleague's slug because that's who the email addressed; the children had no email addresses and therefore no slug in the candidate set. Retrieving "facts that mention child A" at draft time was impossible — the fact lived on the mother's file with no cross-reference.

The fix: an optional `mention_slugs` list on every fact, plus the beginnings of a kinship graph in person records.

- `person.md` files can carry an optional `parent_slug:` field. That establishes the parent-child link without requiring the child to have contact details.
- `build_candidate_slugs()` in `gmail_facts_mine_lib.py` returns two sets now: **primary** (the addressed people, same as before — these are the only valid `subject_slug` values) and **mention** (children of primary candidates via a reverse parent-to-children map, built once at miner startup). The LLM gets both lists in the prompt with an explicit rule: mention candidates may be cited in `mention_slugs` but MAY NOT be used as `subject_slug`.
- `load_facts_for_subject()` unions by subject with by mention. Looking up "Arthur" returns both facts whose subject is `arthur-fitzgerald` (if any are mined directly) and facts whose subject is someone else but whose `mention_slugs` includes him.
- `brain_index.py` gains a `by_mention` map alongside `by_subject`, so the fast path is symmetric.

Single parent only in v1. Depth-1 traversal (no grandchildren via the same mechanism). Step-parent / multi-parent is a future extension via a `parent_slugs` plural field — not yet needed.

### The mining epic — brain learns on its own

Up until 2026-04-20, Huckle's brain was a frozen snapshot. The one-time mining pipeline from step 0 produced the initial seed; the parallel-project fact import added 352 pre-scoped facts; a retroactive scope pass tagged the Huckle-native 257. Total: 596 facts, none of them updating. If the operator told a colleague over email "I'm raising a Series B next quarter," that fact never landed in the brain, and a draft to a different colleague three days later had no way to reference it.

The fix: three daily miners that extract durable facts from three sources and write them through `upsert_fact()` with audience-scope tagging done at write time. All three share a single helper, `agents/shared/fact_extraction.py`, that handles the LLM call, parses the response, and applies a handful of hard filters.

| Miner | Source | Cron (UTC) | Subject inference |
|---|---|---|---|
| `gmail-facts-mine.py` | Gmail inbox + sent, rolling window from cursor | `0 4,10,16,22` | `From` / `To` / `Cc` emails mapped against `people/*.md`'s `email:` field |
| `krisp-facts-mine.py` | Meeting-transcript debriefs (meetings-coach-side) | `15 4,10,16,22` | Attendee emails on the pending debrief; all-hands (>6 attendees) dropped |
| `workflowy-facts-mine.py` | Workflowy tree via `/nodes-export` | `30 4,10,16,22` | Whole-word full-name mentions against `people/*.md`'s `full_name:` field |

The transcript miner lives in the `meetings-coach-workspace/` rather than the `connector-workspace/`. That's deliberate — the pending-debrief JSONs are already in the meetings-coach workspace, and reading them from the connector would require a cross-workspace file access that doesn't survive process-level isolation. Placing the miner in its own agent's workspace keeps the read in-bounds.

Shared design rules, all enforced in `fact_extraction.py`:

- **Self-filter.** Facts with `subject_slug == sam-smith` (or any of the known self-name variants) are dropped. The brain tracks others.
- **Unknown-slug filter.** If the LLM returns a subject slug not in the candidate set (which is derived from message attendees / node mentions), the fact is dropped. The model sometimes invents subjects to satisfy the JSON schema; silently dropping them keeps the brain clean.
- **Confidence floor.** Facts with `confidence < 0.3` are dropped silently. Facts in `[0.3, 0.6)` are still written to `brain/facts/` — the Flux-style pattern — but also flagged for review (see next section).
- **Scope required.** A fact with no `audience_scope` (or an entirely-invalid scope list) is dropped. The rule is that miners don't produce untagged facts.

The three miners fire every six hours — 04/10/16/22 UTC, which is 20/02/08/14 Pacific. The 10 UTC slot (2 AM PT) is the pre-brief one: any fact mined in that slot lands in the brain before the `30 10 UTC` brief-gen composes against it, keeping the Fleet 5 AM PT rule intact. The other three slots are about responsiveness through the workday — a fact extracted from a morning Gmail reply is available in the brain by early afternoon, not the next morning. Each miner's cursor advances across runs so the same message isn't re-extracted; re-observations of an already-known fact feed the reinforcement loop below instead of silently no-op'ing.

### Reinforcement on re-observation

The first cut of `upsert_fact()` returned `status: skipped` when it saw a duplicate idempotency key — a fact it had already written with the same `(source_agent, subject, key)` triple. That's correct for idempotency ("don't double-write the same entry") but it throws away a real signal: *this fact just got observed again.* A fact seen three times across three different emails is stronger evidence than the same fact seen once.

The current behavior, landed 2026-04-20, is Flux-style reinforcement. On idempotency collision, the fact's `confidence` bumps by 0.05 (capped at 0.95 so reinforcement asymptotes below 1.0 — "I've seen this a lot" stays semantically distinct from "this is a verified truth") and its `last_reinforced_at` field updates to the incoming timestamp. The miner envelope gains a `facts_reinforced` counter alongside `facts_minted`. Recency-sorted brain readers — "what's been observed about this person lately?" — get a real answer instead of the `recorded_at` timestamp frozen on first-write.

The dedup invariant still holds. No matter how many times the same email gets reprocessed by a re-run, there's still exactly one fact block on disk for that idempotency key. Reinforcement rewrites that block in place; it never appends.

### Within-batch and cross-run semantic dedupe

Idempotency-key reinforcement only catches exact-hash collisions: same message, same subject, same content string. A real corpus throws two harder failure modes at the miner.

**Within a single extraction.** The LLM sometimes emits the same claim twice in different wording inside one response — *"Jamie has started exploring new job opportunities outside of LinkedIn"* and *"Jamie started exploring opportunities outside LinkedIn."* Distinct content strings, distinct idempotency keys, both land on disk as separate facts. The fix (`_dedupe_within_batch()` in `fact_extraction.py`): group the batch by subject, compute Jaccard similarity on a stopword-stripped token set, collapse pairs at ≥ 0.6 similarity. Higher confidence survives; on tie the lexicographically-first idempotency key wins (determinism). Textual re-statements collapse; genuinely different claims pass through.

**Across mining runs.** Today's miner writes *"Eliott is Jamie's son, born approx 2021-06"*; next week's miner on a different email writes *"Arthur, Jamie's son, is approaching 2.5 years old"* (and then re-derives the birthdate to 2023-10). Those are two distinct facts about two distinct children — but a semantic-similar claim from a third email would get minted as a third entry instead of reinforcing one of the existing two. Jaccard on bag-of-words misses this because the surface vocabulary is so different.

The cross-run layer uses local embeddings — `fastembed` with `BAAI/bge-small-en-v1.5`, ONNX under the hood, CPU-only, 384-dim vectors, no API keys. `_dedupe_against_existing()` routes each new fact against the existing brain for the same subject via a structured short-circuit first:

- Different `fact_type` → definitely not dupes, skip the embedding.
- Same `fact_type` with a known-key conflict (different company, different year, different title, different `(domain, item)`) → skip the embedding.
- Same `fact_type` with a matching key (same company, same year, etc.) → reinforce without embedding.
- Otherwise → embed both contents, compute cosine.

Cosine thresholds: `≥ 0.88` → reinforce in place (call `reinforce_fact_by_id()`, same confidence bump and cap as the idempotency path). `0.75 – 0.88` → enqueue on the pending-review queue for operator decision via the morning brief. `< 0.75` → NEW, proceed to upsert.

The `embed()` wrapper degrades open: any import or runtime failure returns None, and the dedupe helper falls through to "NEW" instead of crashing the miner. On the 2026-04-21 deploy, the encoder produced cosine 0.973 for *"Jane prefers tea over coffee"* vs *"Jane likes tea rather than coffee"* and 0.415 for an unrelated sentence — the thresholds sort correctly without hand-tuning.

### Low-confidence flagging

The mining pipeline writes everything at `confidence ≥ 0.3`, which is deliberately lower than the threshold the composer uses. A fact at `confidence = 0.5` might read plausibly on the person file — *she's possibly moving to Austin in June* — and would be overconfident to surface in a draft. Dropping it entirely loses information that might get reinforced on the next mining pass.

The compromise mirrors the parallel project's pattern: facts at `[0.3, 0.6)` land in `brain/facts/YYYY-MM.md` with their real confidence value AND get a one-line pointer appended to `brain/facts/_pending_review.md` — an audit trail the operator never actually has to read — plus a matching entry on the tap-to-resolve queue covered in the next section. The review pointer captures the fact id, confidence, source (`gmail:<msg-id>`, `krisp:<event-id>`, `workflowy:<node-id>`), the LLM's stated reason, and the content. Writes to the review file are atomic and idempotent on fact id; re-running the miner over the same window doesn't duplicate review entries.

The composer-side gate is wired up: `load_facts_for_subject(..., min_confidence=0.6)` is the signature every compose path uses, and facts below the threshold never land in the recipient context. Facts that survive review get their confidence bumped to 0.95 (operator-promoted, via the brain-maintenance digest below); facts that don't get written to `_rejected.md` so subsequent mining passes skip the same claim by signature (subject + content hash).

### Brain maintenance in the morning brief

`_pending_review.md` used to be the decision surface. In practice it was a file nobody read: the operator isn't going to open a Dropbox markdown every few days to triage fourteen fact candidates, and the queue grew unboundedly. The 2026-04-21 pass replaced it with a **"Brain maintenance" section appended to the morning brief**, surfacing pending items as tap-to-resolve Telegram messages.

The shape:

- **0 items** → section suppressed. No noise on quiet days.
- **1–5 items** → one message per item with an inline keyboard `[✅ Yes] [❌ No] [⏭ Skip]`. Yes promotes the fact to the canonical month file with confidence bumped to 0.95 and an `operator_confirmed_at` stamp. No removes it from the markdown trail and writes a signature to `_rejected.md`. Skip mutes the item for seven days.
- **6+ items** → collapses to a single summary message *"12 items pending review — tap to expand"* with a `[📋 Expand]` button that re-delivers the full list.
- **≥ 3 items** → a bulk footer appears: `[☑️ Approve remaining]` (accept everything above), `[🔕 Silence today]` (mute all visible until tomorrow morning).

Sources feeding the queue:

| Source | When | Prompt |
|---|---|---|
| `miner` | low-confidence fact extraction (`[0.3, 0.6)`) | *"Is this a durable fact worth remembering?"* |
| `dedupe` | cross-run cosine match in the 0.75–0.88 band (ambiguous near-dup) | *"Possible duplicate of an existing fact — merge (reinforce) or keep separate?"* |

The queue itself is an append-only JSONL at `cache/pending-review-queue.jsonl` with an idempotency check on `id`; the digest renderer filters out entries whose `muted_until` is in the future. Atomic rewrites (tmp + replace) handle the mute and remove cases so a crash mid-update can't corrupt the file.

The host cron that ships the section fires at `2 12 * * *` (UTC — 5:02 AM PT, immediately after the fleet morning-brief delivery at `0 12`). The Telegram callback handlers live in `agents/shared/dispatcher.py` behind a `facts:*` prefix; the actual promote/reject/skip logic is in `agents/connector/scripts/facts_callback_lib.py` so it stays testable without a live Telegram session.

### Decay-weighted retrieval

Static confidence gets a fact wrong in both directions over time. A fact the miner minted at 0.8 two years ago is treated the same as one minted yesterday; a fact that got reinforced across six observations bleeds confidence because the extraction model averaged its own cautious 0.6. The correspondence layer was compensating for both by over-trusting stored values.

The fix is query-time, not write-time: `load_facts_for_subject()` applies `0.5^(age_days / half_life)` at retrieval and surfaces the result as `effective_confidence` on each returned fact. Per-category half-lives live in `agents/shared/decay.py`: identity / relationship / birthdate don't decay (None sentinel); role and employer get 365 days; preference gets 180; event and health 90; logistics / rumour / guess 30; default 180. `last_reinforced_at` takes precedence over `recorded_at` in the age calculation so a fact re-observed this week doesn't get penalized for the months since the original write.

The default `min_confidence=0.6` is now effective, not stored. A 180-day-old event at raw 0.8 lands at effective 0.2 and drops from compose context automatically; an 8-year-old identity fact at raw 0.9 stays at 0.9 and never falls off. Tuning is per-category — one-line edits to `HALF_LIFE_DAYS` — rather than per-fact, because individual facts shouldn't have to declare their own curves and the classifier that emitted the `category` field is the right level for "how fast does this kind of claim age out."

### Theory of mind — known_by

The recurring draft-composer failure was sending a recipient back the fact they had just told the operator. Jane would write the operator about her career exploration; the miner would extract *"Jane has started exploring new roles"* with its emails-are-participants-by-default provenance; a week later a reply to Jane would cite the extracted fact as if it were news to her. Every re-observation made the fact more confident; none of them tracked that Jane herself was the source.

Every fact now carries an optional `known_by` list — slugs believed to have been told the fact. Gmail miner populates it from the primary candidate set (From / To / Cc minus the operator). Workflowy miner leaves it empty (the operator's private notes, no participants). Other sources fill it with whatever "was there when it was observed" means in their world. At retrieval time, `build_recipient_context()` tags each shareable fact with `recipient_knows: bool` based on whether the current recipient's slug is in `known_by`; `compose_lib.py` renders a `[RECIPIENT KNOWS]` marker in the prompt's WHAT YOU MAY REFERENCE block, and the prompt carries a rule telling the LLM not to present tagged facts as new information.

The forward path covers new facts, but the backfill is where the bulk of the value lives. Two source types have recoverable participant lineage: Flux-imported facts whose `source_detail` is a bare integer (Flux's `Message.id`) can be resolved by opening Flux's SQLite and pulling `sender_address` + `recipient_address` for that row; Gmail-miner-era facts (`source_detail` starts with `gmail:`) can be re-fetched via the Gmail API for the message's From/To/Cc headers. A one-time pass (`agents/shared/scripts/backfill-known-by.py`, dry-run default, `--commit` with tarball backup) resolved 361 of 619 facts on the 2026-04-21 brain: 352 Flux-sourced, 9 Gmail-sourced. The 258 unresolved entries fall into three buckets where no participant backref exists — `LLM-extracted from interaction data` (the old scope-augment retroactive pass), `birthday-miner/...` entries (no participant concept), and krisp debrief titles (resolver deferred until attendee-list mining lands on the meetings-coach side). Those stay empty and default to "new to every recipient," which is the conservative fallback.

Of the 352 Flux resolutions, 299 landed as `known_by = [subject]` — 1-on-1 threads where the fact's subject is the only non-the operator participant. That's a narrower win than the multi-person case, but still the right signal: drafts back TO the subject carry the tag and the composer stops re-telling them. The remaining 53 multi-person resolutions — CC'd threads, group emails, spouses on the same message — are where the feature earns its keep. Miner runs from 2026-04-21 forward stamp `known_by` natively; the backfill script is a one-shot, idempotent on already-tagged blocks so re-runs are safe no-ops.

### Person cards as a surfacing layer

High-confidence facts (≥ 0.7) also append a one-line observation to the subject's `brain/people/<slug>.md` under a `## Recent observations` section — append-only, trimmed to ten entries. The card is not a second source of truth; the fact file stays authoritative. The appended line is a surfacing mechanism so a glance at the card tells the operator what's new about Sarah, without having to scan the monthly fact files. Miners skip the append silently when no card exists yet — the fact itself still writes — because a single mined observation shouldn't be enough to conjure a new subject into the brain. The durable-storage pattern lives in [Ch 16 — The shared brain](16-shared-brain.md); this paragraph is the miner's use of it.

### Real-time triage (deferred to the next chapter)

A twin pipeline, landing in a sibling session, replaces the half-hour polling of `inbox-triage` with a Gmail Pub/Sub push path: Google sends a notification when a thread changes, a persistent listener on the VPS reads the notification, fires `inbox-triage --thread-id <id>`, and then `auto-compose` picks the thread up on the next cycle with seconds of latency instead of minutes. That work rides on a separate auth grant (Pub/Sub scope), a systemd daemon (`clawford-huckle-push.service`), and a daily `gmail-watch-renew` cron to keep the Gmail-side watch alive past its seven-day expiry.

The polling path above stays in place as belt-and-suspenders. The push path is faster for responsive inbounds ("can you call in ten minutes?") but can miss events under Pub/Sub edge cases and systemd restarts; the poll loop guarantees eventual delivery. The full architecture lives in [Ch 18 — The inbox](18-the-inbox.md).

### The 9 PM silence

The correspondence layer was a month old when the operator sent Huckle Cat a one-line question at 9:07 PM PT and got no reply. Two check-marks on the Telegram side. No typing indicator, no error bubble, no eventual response — just silence. The question itself was trivial: *"Did you draft any emails today?"*

The conversation JSONL at `~/.clawford/inbox/connector.jsonl` told the wrong story. An assistant turn was recorded at the right timestamp, saying *"No — I don't send or draft emails unless you ask me to help with a specific person."* Every signal short of actually looking at the operator's phone suggested the reply had gone through. The clue was one line in `~/.clawford/logs/inbox.log`:

```
telegram send DENIED by reviewer for agent='connector':
  The agent's role is to send relationship nudges/notes triage,
  not to conduct a direct conversation reply like this.
```

The outbound reviewer had denied the message. Dispatcher's persist-before-send ordering meant the JSONL recorded an assistant turn that had never actually left the VPS. Two bugs stacked: the reviewer's role summary for connector forbade *"auto-replies"* too broadly, and the reply itself — even if it had reached Telegram — was a confident lie. Auto-compose had been drafting Gmail drafts for weeks. The agent answered from its static role prompt because it had no tool to look at its own cron logs.

The fix landed as a fleet-wide edit on 2026-04-21: the reviewer's role summaries now affirmatively license conversational Q&A about the agent's own state; every agent's `tools.py` exposes `get_recent_runs`; and `_build_system_prompt` in the dispatcher carries a shared preamble teaching every agent to call its state-inspection tools before answering meta-questions. The full architecture lives in [Ch 18 — The inbox](18-the-inbox.md#conversational-grounding-and-the-outbound-reviewer). Huckle Cat was the agent that triggered the discovery; the lesson generalizes to the whole fleet.

The meta-lesson is worth naming because it will recur: **an agent that doesn't know what it did today will confabulate, and the outbound reviewer will silently block the lie.** The two failure modes are linked — a better classifier wouldn't have helped if the underlying reply was still wrong, and a better reply wouldn't have helped if the reviewer kept denying it. Fix both or fix neither.

### The bubblewrap beat

The three fact miners run under process-level isolation — the first brain-writing crons to adopt the P1.2 bubblewrap profile documented in [Ch 19 — Security and hardening](19-security-and-hardening.md). The file-based opt-in pattern: a line per cron log-name in `~/.clawford/bwrap-allowlist.txt`, read by the host-cron wrapper, triggers the `CLAWFORD_ISOLATION_MODE=bwrap` handoff before the script runs.

The initial rollout surfaced a real gap in the profile. The pre-widening default bound `brain/agents/<agent_id>/` RW and left everything else under `brain/` read-only. The miners write to `brain/facts/`. The `daily-refresh` cron writes to `brain/people/`. The meetings-coach debrief path writes to `brain/commitments/`. Under the old profile, every one of those writes hit EROFS silently, and the `daily-refresh` host log had been carrying exactly that error for two days before the miners flagged it.

The right fix was to widen the profile, not to keep the miners exempt. The 2026-04-20 pass added three things to the bwrap default: an RW whitelist for `brain/{facts,people,commitments,queues}/` (the shared write targets; every other path under `brain/` stays RO), an RO bind for `~/.codex/` (so the Codex token loader resolves inside the namespace), and a tmpfs at `/dev/shm` (so SysV shared memory works and browser-driven crons can join the allowlist too). The three miners went on the allowlist in the same commit, and within a day every non-Fixit cron in the fleet followed. The profile widening is covered end-to-end in Ch 19's Defense Layer 7.

> 🔦 **Tip.** When a new brain-writing cron joins the fleet, check whether its write target is already in the RW whitelist. If it isn't, widen the profile first — don't carve out a per-cron exception. The whitelist is explicit by design so it stays a short, auditable list.

## The professional brain

Every narrative in this chapter so far is about the people in the operator's *personal* life — family, friends, known colleagues, the people in the operator's Google Contacts. The correspondence layer, the audience-scope tags, the per-circle voice profiles — they all assume the recipient already has a people file. They have no answer for a different failure: a cold inbound from a recruiter the brain has never seen, to an operator whose career matters more to them than any specific draft ever will.

Before 2026-04-21 the triage code's blanket skip for unknown senders caught those inbounds and dropped them silently. The operator was catching them by hand. The fix is a second brain — structurally parallel to the people-and-facts brain, scoped to the operator themselves — plus three surgical edits to the existing triage + compose path. The result is that a cold inbound from a retained-search recruiter routes into the same four-step composition loop as any other thread, reads a fit assessment off the operator's professional profile, and stages a draft that reads like the operator wrote it.

This gets its own top-level section rather than another subsection of the correspondence layer because the data layer is large and separable. The operator-side brain — the `self/` subtree on Dropbox — is architecturally analogous to the people-and-facts brain that the rest of this chapter builds on, with the same shape (facts, archives, a directory on Dropbox) but a single subject: the operator. The architectural slot for it lives in [Ch 16 — The shared brain](16-shared-brain.md#the-self-layer-a-brain-about-the-operator); this section is how Huckle builds and consumes it.

### The four layers

The self-brain lives at `~/Dropbox/clawford-backup/self/` and has four layers, each generated by its own script and each consumable on its own:

| Layer | Input | Output |
|-------|-------|--------|
| **Stage 1 — classifier** | `Personal/Bio/`, `Personal/Job Search/`, `Archive/<employer>/`, Workflowy export | `self/archive-index.json` — per-file `{role, class, signal_score, summary, chronological_date}` |
| **Stage 3 — per-role archives** | Signal-bearing records from the index, grouped by role or search round | `self/archives/<role>.md` — scope, OKRs owned, tenets authored, major accomplishments, feedback themes |
| **Stage 4 — structured facts** | `self/archives/*.md`, re-parsed into typed schemas | `self/facts/{employer,major_accomplishment,target_company,tenet_authored,strength_theme}.json` |
| **Stage 1.2 — narrative profile** | Structured facts + priority raw docs (LinkedIn PDF, career bios) | `self/profile.md` — track record, excited by, great at, leadership style, level & scope bar, target-role shape |

The numbering is historical; Stage 1.2 landed after Stage 1 was already named, and renaming would have churned the test suite. The pipeline order is 1 → 3 → 4 → 1.2.

Each layer is regenerable from the layer below it. The archive index is deterministic against the filesystem. The per-role archives re-synthesise from the index. The structured facts re-extract from the archives. The profile re-renders from the facts. Hand-edit any of them and the downstream re-generation respects the edit. Raw archive files at `~/Dropbox/Archive/<employer>/` are never copied into the brain — the classifier summaries are 30 words apiece, and the profile speaks only to scope, decisions, and outcomes. Proprietary strategy stays on disk at its original path; the brain carries what generalises across the operator's arc.

### Chronology is load-bearing

The operator's career spans roughly a decade across several roles at different employers, interleaved with distinct rounds of job search. Reasoning across that arc without respecting the chronology is how the self-brain learns the wrong lessons — classifying a 2020 strategic doc as current thinking, promoting a 2022 target company that no longer matters.

Two hand-editable files under `self/` carry the chronology:

- `role-timeline.md` — one row per employment span with `start_date`, `end_date`, and a short role description. Rows cover the whole arc, including between-job periods.
- `search-timeline.md` — one row per round of job-seeking with similar spans. Treating job search as its own class of "role" matters because the artefacts a search produces (decision frameworks, company-by-company notes, target-list spreadsheets) share a shape across rounds and deserve to be synthesised together, not fragmented across whichever employer happened to be current at the time the search concluded.

The Stage 3 synthesiser routes every signal-bearing record to the matching role or search round by the classifier's `chronological_date` field, which falls back through a small hierarchy: explicit dates in the document text → filename date patterns → `mtime`. A class-based override routes anything tagged `job_search_material` to the search-round bucket regardless of date — a decision-framework doc authored during employment at one company but about looking at a different company belongs to the search round, not the employer archive.

### Recruiter detection

The existing triage path classifies every inbound thread into one of five statuses before the compose loop decides whether to draft a reply. The relevant one here is `skipped_unknown_sender` — the catch-all that drops any sender whose email isn't in `email_to_slug`. That status was silent data loss for recruiters; they are by definition unknown senders the first time they reach out.

The fix is a heuristic detector that runs *before* the `unknown_sender` skip and, on a hit, reroutes the thread to a new `queued_cold_recruiter` status that feeds the compose loop. Three signal shapes contribute:

1. **Domain suffix match.** 22 ATS and retained-search domains compiled into a set. Ends-with match, so `mail.greenhouse-mail.io` and `hire.lever.co` both trigger. This is the strongest signal; a message from an ATS domain is essentially definitionally a recruiter outreach.
2. **Subject + snippet phrase match.** A bag of lexical patterns that repeat across recruiter outreach: *"reaching out regarding"*, *"your background"*, *"came across your profile"*, *"opportunity,"* plus level vocabulary — *Director*, *Head of*, *VP*. Not sufficient alone, but a hit plus an ambiguous domain (LinkedIn InMail) promotes to recruiter.
3. **Negative-phrase veto.** Phrases that look like recruiter outreach but aren't — *"your order"*, *"your subscription"*, *"payment failed"*. One hit here vetoes the detection.

The detector returns a `(bool, confidence, signals)` tuple; the reason field persists on the queue entry so the Telegram FYI can explain *why* a message was flagged. There's a narrow carve-out in the service-account filter: `is_likely_service_account` catches a lot of genuine recruiter traffic (noreply addresses with ATS suffixes), so a positive recruiter-domain match skips the service-account drop. Getting that exemption wrong was the thing that silently dropped the first real recruiter inbound the pipeline ever saw.

### The FIT CHECK — composite, not a target-list gate

When the compose loop runs on a cold recruiter thread with `--cold-inbound`, it injects a SELF CONTEXT block into the prompt carrying the profile narrative, the level-and-scope bar, the operator's employer history (most recent first, with end dates), and the active + recently-concluded search pipeline from the search-status layer. Then a Step 0.5 — the FIT CHECK — runs before the usual four-step loop.

The first cut of the FIT CHECK was a binary gate on `target_company`: on the list → engage, off the list → decline. That was the wrong shape for two reasons. Real A-list opportunities surface outside the curated list — there's no way to know ahead of time that a frontier lab is about to pitch a specific role. And some target-list companies turn out to be the wrong fit once the role details land, regardless of brand.

The current FIT CHECK scores three dimensions independently:

- **Domain fit.** Does the company's business resonate with the operator's background? Marketplaces, two-sided platforms, content platforms, and consumer/community businesses score strong. Adjacent enterprise surfaces where causal methods transfer score moderate. Industries with no prior exposure score weak.
- **Level fit.** Does the role pass the operator's level & scope bar? Owning a major lever of company success (pricing, supply, ranking, monetisation, growth) or sitting C-suite-adjacent scores strong. Senior Director reporting to EVP with unclear scope scores moderate. Director several layers from exec scores weak.
- **Function fit.** Does the role draw on the operator's strengths? Applied ML + causal inference + experimentation scores strong. Pure ML engineering or research leadership scores moderate. Research-only, pure eng management, or sales scores weak.

`target_company` is a *supporting* signal that elevates the composite tier by one step when it hits — not the primary gate. The composite tier (A, B, C, not_a_target, unclear) drives the strategy selection: A and B *take the call*, C politely declines, unclear asks one clarifying question. The LLM emits `fit_assessment: {tier, rationale, domain_fit, level_fit, function_fit, target_company_match}` alongside the draft text, and the Telegram FYI displays the tier with a coloured indicator so the operator can calibrate at a glance.

### The level & scope bar

Level fit weights heavier than the other two dimensions because a role that misses the operator's level bar is a non-starter regardless of how strong domain and function look. The bar is stated explicitly in `profile.md` and lifted into the compose prompt as a dedicated section:

- The role owns a major lever of company success. Pricing, supply, ranking, monetisation, growth — things where the scope map is the company's actual economics, not a subsidiary function.
- OR the role is directly C-suite-adjacent. Reports to CEO / CTO / CDO / EVP, or is a Head-of-function at a company whose function organisation flows through a Head.

Anything else is below the bar. A *Director of Data Science* title three layers from the exec team, with ownership of a narrow measurement function, is structurally disqualifying — not because the work isn't real but because the scope doesn't match the operator's track record of owning company-level levers. The bar is deliberately harsher than domain and function; those score on a strong / moderate / weak scale, while level short-circuits to weak whenever the inbound doesn't surface evidence for one of the two conditions above.

Naming the bar in the prompt was the single highest-value edit of the whole professional-brain pass. Before it, the LLM hedged on level fit; after, it would still write the respectful decline but it stopped trying to talk itself into engagement with roles whose scope obviously didn't match.

### Recruiter voice profile

The five per-circle voice profiles from the correspondence layer cover family, friends, and professional peers. None cover the register the operator uses when writing *to recruiters* — distinct from professional-outer, more compact, less chatty, signature-forward. A draft composed against the professional-outer voice reads like a peer catch-up; a draft composed against the recruiter voice reads like an executive response.

The fix is a new mode on the voice-profile builder: `--recruiter-mode` queries Gmail for sent messages to the 22 recruiter-platform domains from the detector's allowlist and extracts a separate voice fingerprint scoped to that sample. The output lands at `cache/voice-profiles/recruiter.json` alongside the per-circle files, and compose loads it instead of any circle profile when `--cold-inbound` is passed.

The sample is necessarily smaller than the circle samples — the operator has sent fewer cold-recruiter replies than professional-outer emails over two years — but it clears the minimum-sample floor, and distinctive closing patterns (*"Thanks for thinking of me."*) pin down the register reliably.

### The search-status layer

A cold inbound doesn't arrive in a vacuum. The operator often has several searches in late-stage progress already, and a new inbound that would be an eager *take the call* in a quiet period is a polite *let me circle back in two weeks* when the pipeline is crowded. The draft has to know which mode the operator is in, or it will sound mismatched.

`search-status-build.py` mines Gmail and Workflowy over a 90-day rolling window and synthesises the active pipeline via a single LLM call:

- **Gmail evidence.** Threads from recruiter-platform domains (same set the detector uses) get parsed for interview-stage signals: phone screen scheduled, onsite invitation, offer extended, passed, ghosted.
- **Workflowy evidence.** Meeting nodes whose titles match interview-prep patterns (*"Interview prep — <company>"*, *"<company> onsite"*) get parsed for stage inference by surrounding dates.
- **Synthesis.** The LLM reconciles the two sources per company and emits a stage taxonomy entry: `initial-outreach`, `engaged`, `recruiter-screen`, `hiring-manager`, `technical-interview`, `onsite-panel`, `final-round`, `offer`, `accepted`, `declined`, `passed`, `ghosted`.

Output lands at `self/search-status.md` (human-readable) and `self/facts/active_search_stages.json` (programmatic). The compose-side consumer is the SELF CONTEXT block: when the operator is in late-stage engagement with two or more companies, the prompt injects a mandatory *"ACTIVE PIPELINE — keep briefings tight; avoid proposing long intros when urgent pipeline conflicts"* directive that bubbles up as a one-line urgency note in the draft itself. On the first pass the directive was phrased as *"use sparingly"* — which the LLM interpreted as *"don't use at all"*; tightening it to *"MUST include brief urgency line when late-stage count ≥ 2"* fixed that in one edit.

### The calibration draft

A real inbound from a retained-search recruiter on 2026-04-21 became the calibration test for the whole professional-brain stack. The recruiter pitched a senior role at a major consumer platform, with economic-modeling-for-community-flywheel scope and a named reporting line to an EVP. Four iterations of draft review surfaced four distinct gaps — each one a different layer of the architecture failing in a different way.

- **Iteration 1 — the target-list gate.** The first draft tiered the opportunity as B and politely declined on the grounds that the company wasn't on the explicit target list. That was wrong: the role was strong on domain (marketplace dynamics), moderate on level (senior reporting to EVP), and strong on function (economic modeling, causal inference). A target-list miss shouldn't have gated engagement against a two-strong / one-moderate profile. The fix was the composite FIT CHECK — three dimensions with target_company as a supporting elevator, not a gate.

- **Iteration 2 — "directionally interesting."** The second draft engaged politely and asked four scope questions (reporting line, headcount, ownership, mandate) to "learn more." That was the wrong shape for a cold recruiter reply. The recruiter had done the work to research the operator and pitch a specific role; a wall of clarifying questions pushed the work back onto them. The fix was a load-bearing rule in the strategy section: **the call is the screen.** Scope questions belong on the intro call, not in the email. A 3–5 sentence reply acknowledging one or two specific elements of the pitch plus two or three concrete availability windows was the target shape.

- **Iteration 3 — lying about current employment.** The third draft opened with *"I lead <function> at <current employer>."* The operator wasn't at that employer anymore; the profile synthesis was reading the most recent employer record as "current" based on row ordering rather than the end date. The fix was an explicit end-date comparison at compose time: if `today > end_date`, the SELF CONTEXT block renders as *"post-employment / active executive search"* rather than *"currently employed at <X>."* Getting that flag right means the draft can engage with the recruiter from the correct register — someone actively listening to opportunities, not someone humble-bragging about a current role.

- **Iteration 4 — credentials-first opener.** The fourth draft led with the operator's resume even though the recruiter had LinkedIn-sourced the outreach. *"My background is in economic modeling and causal inference"* is redundant; the recruiter said as much in their opening line. The fix was an OPENER STRATEGY directive: the opener engages with *their* pitch, not the operator's credentials. What specific element of their note resonated? Name one or two, conversationally. Credentials come later as confirmation of fit, if at all. A final anti-redundancy rule — no *"Senior Director role"* restatement when the recruiter already used the title — was the last regression.

The shipped draft is not a monument to a perfect pipeline. It's a monument to four specific edits, each made against a specific failure mode, each surfacing a different layer of the architecture. The four-layer self-brain is what the operator's professional context looks like. The FIT CHECK is how the pipeline judges a role against that context. The recipient-respect first principle is how the draft reads from the other side of the wire. The calibration inbound is where all three of those stopped being theoretical and started producing something the operator would actually sign their name to.

### Keep vs. dismiss — cold recruiter lifecycle

The `_make_cold_recruiter_stub_person` helper that feeds the draft is ephemeral — built at prompt time, never written to the people directory. That's fine for the first reply, but the recruiter usually comes back: a week later they want to schedule the call, a week after that they're sending a hiring-manager intro. If the sender never crosses the threshold from "unknown" to "known," every thread re-enters the cold-recruiter pipeline, the drafts keep spinning fresh stubs, and any downstream agent that wants to find this contact (the meetings agent prepping the recruiter screen, say) comes up empty because `find_person_by_email` returns `None`.

The fix is a Telegram-button flow on the cold-recruiter FYI. Auto-compose attaches an inline keyboard with **✅ Keep** and **🚫 Not a fit** under every A/B/C/unclear-tier cold-recruiter draft it stages. Tapping ✅ fires a `recruiter:keep:<thread_id>` callback that reads the queue entry, derives name + slug from the `From` header, and calls `brain.create_person_file(name, "professional-outer", email=…, relationship_type="recruiter", recruiter_signal_domain=…, source_thread_id=…)`. The operator's person directory gains a new entry with the correct circles, and every future inbound from that sender routes as `queued` (known) instead of `queued_cold_recruiter`.

Tapping 🚫 fires `recruiter:reject:<thread_id>` and records the sender on `cache/rejected-recruiters.jsonl`. The inbox triage path now loads that list and short-circuits any future match to a new `skipped_rejected_recruiter` status — preventing the same sender from re-queueing the cold-recruiter pipeline indefinitely. For `not_a_target` tier drafts the buttons are suppressed entirely; the operator can still rescue via a manual `keep_recruiter(descriptor)` tool, but the happy path is dismiss.

Verb choice matters. The earlier cut of this tool called itself "promote" — bureaucratic and mismatched against "not a fit." Re-reading the Telegram prompt out loud settled it: *"Keep Jane Ashby"* vs. *"Not a fit"* is a real operator decision shape. *"Promote Jane Ashby"* sounds like corporate HR. Tools that live in operator voice need to read in operator voice, not schema voice.

The callback plumbing reuses the `facts:*` dispatcher pattern — a thin prefix in `agents/shared/dispatcher.py` hands off to `handle_recruiter_callback` in a connector-side library. Pure functions, testable without a live Telegram session. The rule the code enforces is: idempotency on the thread_id (tapping twice doesn't double-create), and the kept-recruiters log is append-only so re-runs of the callback produce the same final state.

### Email is schedule-plus-filter, not demonstrate-plus-evaluate

A subtler pass on the same calibration thread exposed a deeper mismatch between what the prompt was asking the LLM to do and what an email reply *should* carry. The first version of the cold-inbound schema asked for a `fit_signal` field alongside `compelling_angle` — "one specific piece of evidence tied to the role's demands." The intent was to give the recruiter advocate-ready evidence to forward to the hiring manager. The resulting drafts included phrases like *"which lines up well with work I have done leading economic modeling for marketplace dynamics at scale."* Technically accurate. Structurally wrong.

The recruiter is reaching out to the operator — not the other way around. She has already read the LinkedIn profile, made her fit hypothesis, and is pitching *him* on the role. A reply that pitches fit back inverts the power dynamic. It reads as auditioning when the operator should be the one being courted. It also pre-commits the operator to a framing before the role details have landed, narrowing scope pre-screen.

The structural clarification: an email reply and a recruiter call do different work, even though they're part of the same relationship.

| | Email | Call |
|---|---|---|
| Job | Schedule + surface honest filter | Demonstrate fit + evaluate match |
| Must-carry | Engagement + compelling_angle + availability | Full pitch narrative + evaluation questions |
| Power stance | Being courted (don't audition) | Both parties present; fit gets demonstrated live |

The revised schema drops `fit_signal` entirely. `compelling_angle` stays — because that's *filter* information the recruiter can actually use ("here's what the operator is responding to; if my pitch doesn't match that, I should reframe"). It also stays honest about context the recruiter can't see: if the operator is late-stage with two other companies, the angle names what specifically about *this* shape pulls him anyway, so the recruiter can calibrate urgency. No boilerplate enthusiasm. No "excited about the opportunity."

The LLM's own `recipient_model` reasoning moved in the right direction once the prompt carried the power-dynamic framing explicitly. The model now articulates the distinction unprompted: *"A short, specific, respectful note will feel engaged; a fit-pitch would feel redundant and slightly awkward because she is the one recruiting him."* That's the durable fix — the reasoning step encodes the stance upstream of the output, so drafts stop drifting into pitch register even when the voice profile's "warm-but-restrained" patterns tempt the LLM toward over-explaining.

The three-beat email contract that came out of this is now encoded as a MUST-CARRY section in the compose prompt: **engage with one specific element of their pitch + compelling_angle as honest filter signal + concrete availability**. Four paragraphs would be wrong. Four sentences are right.

### Operator hints — the iterative regenerator

Auto-compose writes a draft every thirty minutes. It can't take feedback. When the operator reads the draft and wants "the same reply but warmer" or "the same reply but mention that I already accepted," the cron model has no answer.

The fix is a Telegram-invoked `/reply` tool that takes an optional `hint` string, threaded from the tool call through `auto-compose.py --operator-hint ...` → `draft-compose.py --operator-hint ...` → `compose_lib.build_compose_prompt(operator_hint=...)`. The hint lands at the top of the compose prompt as a labeled block that explicitly frames itself as *overriding* voice anchors, history defaults, and any conflicting brain facts:

```
OPERATOR HINT (load-bearing — applies to THIS draft only,
overrides conflicting defaults from voice / history / brain):
  <hint text>

If the hint adds factual context (dates, decisions, status
updates), treat it as more recent than anything in the brain and
weave it into the draft accordingly. If the hint is stylistic
(shorter, warmer, less formal), apply it to the final draft_text
AND to the reasoning that produces it — don't just append a
cosmetic pass at the end.
```

Two kinds of hint the pattern covers:

- **Stylistic** — *"make it warmer"*, *"two sentences shorter"*, *"drop the scope questions"*. These steer voice and structure. Important: the prompt instructs the LLM to apply them to the *reasoning*, not just the output — a "make it shorter" pass that trims the final prose without rethinking what to say leaves the reply less coherent than one that regenerates the strategy with shortness as a constraint.

- **Factual** — *"mention that I already accepted the offer"*, *"flag that I'm traveling next week"*, *"the hiring manager followed up separately, reference that"*. These inject information the brain doesn't have yet. The prompt treats factual hints as *more recent than anything in the brain* — authoritative, not a mere suggestion.

The hint is the only reason `/reply` exists as an operator tool at all. Without it, the tool duplicates auto-compose's work: the autonomous cron already drafts every thread every half hour, and the Telegram FYI already surfaces the draft. A manual "/reply" without a hint is just "don't wait thirty minutes" — marginal value. The hint pattern closes the one gap the cron can't: the operator's real-time context that the autonomous pipeline doesn't have.

### Fuzzy reference resolution — operators don't speak thread_ids

The first cut of `/reply` took a Gmail thread_id as its argument. Immediate operator rejection: *"We are never going to have the thread ID handy."* Thread IDs are sixteen-character hex strings — nobody recognises them in running conversation. The operator says *"the recruiter email"*, *"that Tuesday thread"*, or *"the family admin message"*.

The resolver bridges that gap. `_resolve_thread_descriptor(descriptor)` scans the auto-compose log + triage queue + the operator's people directory and matches against whatever shape the operator supplied:

- **Explicit thread_id** (hex, 14–22 chars) — pass through directly. Edge case for programmatic callers.
- **Full email** (`person@example.com`) — match against queue `from_email`.
- **Person name** (first name or full) — resolve via `brain.get_person` with first-name fallback; then match the person's slug against log `slug` field and the person's email against queue `from_email`.
- **Substring on sender / subject** — case-insensitive search across `from_email`, `from_header`, and `subject` on every known thread.

Every match carries a `match_reason` string that enumerates which signals fired: a typical four-signal hit looks like `"slug=<slug> + person_email=<addr> + email_substring + header_substring"`. On ambiguity (multiple candidates), the resolver returns `status: ambiguous` with the candidate list — the Telegram LLM then asks which one the operator meant, rendering the candidate subjects inline. On not-found, the resolver returns recent threads to offer as disambiguation options.

The underlying data sources are the log (persistent, all processed threads) and the triage queue (transient, rolling window). The log has subject + slug; the queue has from_email + from_header. Union-by-thread_id across both gives a single merged candidate pool with the richest metadata either source had.

The fuzzy-resolver pattern generalises to any operator tool that needs to point at a specific thing-in-context. Phase 2b extends it to `/promote` — the cold-recruiter promotion flow now accepts the same descriptor shapes as `/reply`, scoped to un-acted queue state via an `only_queue_statuses` kwarg on the resolver. The status filter is essential: already-promoted, rejected, or known-sender threads must not match `/promote` even if they'd hit the substring heuristic, because the promotion callback needs `from_email` + `from_header` from a live `queued_cold_recruiter` queue entry to create the people file. Without the filter, a fuzzy match could silently misroute to an incompatible state. The next operator tool that needs to point at a thing-in-context should follow the same pattern: scope the resolver to only the state that's actually actionable for that flow.

## Deployment walkthrough

This is the overlay on [Ch 08 — Your first agent](08-your-first-agent.md). The unusual bit for Huckle Cat is that step 0 — the mining pipeline — comes *before* any cron is registered, and step 0 is a lot of the total work.

**Pre-step: reuse the Google OAuth setup.** If [Mistress Mouse](12-mistress-mouse.md) or [Sergeant Murphy](13-sergeant-murphy.md) is already deployed, reuse the same Google Cloud project and credentials. Add the `contacts.readonly` and `gmail.readonly` scopes to the existing consent screen, re-run `google-auth-setup.py` locally, SCP the new `token.json` to the VPS. You do not need a fresh Google Cloud project.

**Pre-step: Google Messages pairing.** Run `python3 agents/connector/scripts/gmessages-auth.py` on the operator's local machine (not the VPS). A Camoufox browser opens, the pairing page takes a screenshot, the operator scans the QR code with their phone, and the session persists in `~/.clawford/connector-workspace/gmessages-profile/`. SCP the entire profile directory to the VPS when the pairing is done.

**Pre-step: Workflowy bearer token.** If not already set up from Sergeant Murphy's deployment, generate a Workflowy bearer token and paste it into the agent's `.env` file.

**Step 0: Run the mining pipeline locally.** This is the big one.

1. `python3 agents/connector/scripts/mine/run-all.py` — runs all seven miners sequentially (parallelism is inside each miner). Writes seven `cache/mined-*.json` files. Expect this to take 20–40 minutes depending on the gmail history length. Budget more for the first run.
2. `python3 agents/connector/scripts/mine/contact-aggregator.py` — aggregates the seven source files into a unified candidate list, does the deduplication and scoring, writes `cache/candidates.json` and a tiered `cache/review.md`.
3. `python3 agents/connector/scripts/mine/llm-enrich.py` — runs LLM enrichment at 10-worker parallelism, produces `cache/enriched.json`. Expect this to take 5–10 minutes and cost a few dollars on a small model.
4. **Review pass.** Open `cache/review.md` in a proper editor. Read every candidate. Delete marketing contacts, correct obvious mis-classifications, adjust circles, merge duplicates the aggregator missed. Budget 30–60 minutes for this step the first time. Do not skip it.
5. `python3 agents/connector/scripts/mine/contact-aggregator.py --finalize` — writes the final `people/` and `facts/` files to the shared brain on Dropbox. Dropbox syncs the files up to the VPS automatically.

**Step 3: The regular agent-deploy steps.** Scripts, manifest, SOUL/IDENTITY files — same as every other agent.

**Step 5: Register the host crons.** Add the four Huckle Cat entries to the `CONTRACT_ENTRIES` block in `ops/scripts/install-host-cron.sh`. Run `install-host-cron.sh` on the VPS.

**Step 6: Deploy.** `python3 agents/shared/deploy.py connector` on the VPS. Verify with `python3 agents/connector/scripts/people-scan.py --dry-run` — it should print a JSON summary with non-zero counts (if it returns zero, the brain seeding from step 0 didn't reach the VPS, and the Dropbox sync is the place to look).

**Step 7: Wait for `30 10 UTC` the next day.** The first morning-relationship-nudge cron fires, composes a brief, and writes `cache/morning-brief-ready.txt`. Fleet-deliver at `0 12 UTC` picks it up and sends to Telegram. If the brief looks sparse, it is not that the agent is broken — it is that the `daily-refresh` cron hasn't had a chance to update `last_interaction` on a second day yet. Give it 48 hours.

## Pitfalls

> 🧨 **Pitfall.** Deploying Huckle Cat without running the mining pipeline first. **Why:** the agent reads a pre-seeded people directory in the shared brain and the mining pipeline is what creates that directory. Without it, the first morning-relationship-nudge composes against an empty (or near-empty, if Google Contacts auto-imported something) people directory and produces a useless brief. **How to avoid:** step 0 of the deployment walkthrough is the mining pipeline. It is not optional, it is not "wire it up later," and it is not a thing to skip under time pressure. Budget 60–90 minutes for the mining run + review pass + finalize sequence on first deploy. The rest of the agent is a 30-minute deploy.

> 🧨 **Pitfall.** Skipping the review pass between `contact-aggregator.py` and `--finalize`. **Why:** the aggregator is good but not perfect. It will auto-classify marketing contacts as "acquaintances," miss some duplicates, get circles wrong for cross-domain relationships, and occasionally promote a low-signal contact into a high-tier bucket because of a quirk in the scoring. Every one of those errors lands in the shared brain forever unless the review pass catches it. **How to avoid:** open `cache/review.md` in an editor after every aggregator run and before every `--finalize`, and actually read it. The first deploy is 30–60 minutes of genuine review work. Later re-runs are faster but still non-zero.

> 🧨 **Pitfall.** Running the mining pipeline on the VPS instead of the local machine. **Why:** three things go wrong. The LLM enrichment cost becomes invisible (buried in the monthly bill instead of showing up in real-time local logs). The review-markdown editing becomes painful (it is a 300-row table; editing it over `ssh + vim` is miserable). And iteration is slow — the first deploy typically involves 3–5 aggregator runs with config tweaks, and those tweaks are 10x faster against a local filesystem. **How to avoid:** the mining pipeline is a local-only operation. The shared brain syncs the output to the VPS via Dropbox; the pipeline scripts do not need to run on the VPS at all.

> 🧨 **Pitfall.** Forgetting that `daily-refresh` is load-bearing. **Why:** without `daily-refresh`, every people file's `last_interaction` field is frozen at the timestamp of the one-time mining run, and the morning-relationship-nudge will start flagging contacts the operator has interacted with since. This failure is silent and cumulative — the brief looks plausible on day 1 and gets progressively more wrong every day. **How to avoid:** `daily-refresh` is a host cron at `0 10 UTC`. Verify it is registered in `install-host-cron.sh` alongside the other three crons. Verify the ordering: `daily-refresh` at `0 10`, `morning-relationship-nudge` at `30 10`, with 30 minutes of slack between them.

> 🧨 **Pitfall.** The `daily-refresh` attendee-count cap getting raised. **Why:** the cap exists to prevent all-hands meetings from silently updating `last_interaction` on everyone in the room. If the operator has a weekly 15-person team meeting, and the cap is off, every attendee in that meeting gets their `last_interaction` stamped to today every week, and the "overdue" list becomes structurally empty for the whole team. The relationship-cadence signal the whole agent exists to produce is the first casualty. **How to avoid:** the cap is 6 attendees, hardcoded in `daily-refresh.py`. If you tune it, tune it down (for paranoid households), not up. Any meeting with more than 6 people is assumed to not be a 1:1 interaction.

> 🧨 **Pitfall.** `morning-relationship-nudge` composing against a stale `mined-gmessages.json`. **Why:** `gmessages-mine` runs every 2 hours via a Camoufox browser that can fail in a dozen ways — session expired, Google HTML change, Camoufox profile corrupted, DevTools snippet rejected. If it fails silently, the `mined-gmessages.json` cache file gets stale, and `daily-refresh` + `morning-relationship-nudge` both quietly use data from last Tuesday. As of 2026-04-15, the nudge does not verify cache freshness before composing. **How to avoid:** the heartbeat probe for `connector` includes a check for `mined-gmessages.json` mtime; if it is older than 4 hours, the heartbeat flips to `fail` and the operator gets alerted via the fleet-health cron. If you see that alert, the fix is to re-run `gmessages-auth.py` locally (to refresh the session) and SCP the new profile up. The root-cause fix — making the nudge itself check cache freshness — is listed in the chapter as "known gap" and is a future improvement.

> 🧨 **Pitfall.** Treating the shared-brain `people/` directory as append-only. **Why:** the mining pipeline and the ongoing crons both modify files in `people/`, and if any of those modifications are not atomic, concurrent writes between the local laptop (the operator editing a people file by hand) and the VPS crons (the daily-refresh cron updating `last_interaction`) can lose data silently. **How to avoid:** all writes to people files go through `agents.shared.brain.update_people_file`, which does a read-modify-atomic-write with a Dropbox-aware lock file. Do not edit people files by hand on the VPS. Editing on the local laptop is fine because the VPS crons are idempotent against the local edits; the lock file prevents the race.

> 🧨 **Pitfall.** Shipping cold-recruiter drafting without the detector library on the VPS. **Why:** `agents/<agent>/manifest.json` is PII-bearing and gitignored; the tracked `.example` template and the live manifest drift over time as different sessions add scripts. A cold recruiter inbound on the first live test routed to `queued_cold_recruiter` on the local laptop but hit `ImportError` on the VPS because the detector library wasn't in the copy that had been deployed. **How to avoid:** every time a new script lands in an agent's manifest, edit BOTH `agents/<agent>/manifest.json` (live, gitignored) AND `agents/<agent>/manifest.json.example` (tracked template) in the same commit. Before SCP-ing the live manifest to the VPS, diff against the VPS copy to catch drift from parallel sessions — SCP without the diff clobbers entries that landed out-of-order on the other side.

> 🧨 **Pitfall.** LLM enrichment cost scaling linearly with contact count. **Why:** the enrichment pass runs one LLM call per candidate, regardless of whether the candidate is high-tier or marketing. On a first deploy of 280 people, that is `\~$3`. On a second-pass mining run of 600 people (because the gmail history grew or because a new source was added), it is `\~$6`. There is no safeguard against runaway spending — if a bug in the aggregator produces 5000 candidates, the enrichment pass will cheerfully run 5000 LLM calls. **How to avoid:** the aggregator has a candidate-count guard: if the final candidate list exceeds 1000, the pipeline hard-stops with `"Candidate count {N} exceeds safety ceiling — investigate before proceeding"` and the operator has to explicitly override with `--i-know-what-i-am-doing`. Do not disable the guard lightly.

## See also

- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the deploy path and safeguard story
- [Ch 08 — Your first agent](08-your-first-agent.md) — the general deploy walkthrough
- [Ch 12 — Mistress Mouse 🐭📅](12-mistress-mouse.md) — canonical Google OAuth pattern
- [Ch 13 — Sergeant Murphy 🐷🔍](13-sergeant-murphy.md) — the cache-is-not-a-delivery-queue rule that all of Huckle Cat's orchestrators follow
- [Ch 16 — The shared brain](16-shared-brain.md) — the `self/` layer's architectural slot and the two-subtree brain split
- [Ch 17 — Auth architectures](17-auth-architectures.md) — the cross-agent auth reference (pending)
