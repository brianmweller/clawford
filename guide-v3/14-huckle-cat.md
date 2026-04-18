# Huckle Cat 🐱🤝 — the connector agent

*Last updated: 2026-04-16 · Reading time: ~25 min · Difficulty: hard*

> **TL;DR.** Huckle Cat is the relationship agent — the one that inverts the usual shape of a Clawford agent. Instead of wrapping a single external API the way Mr Fixit wraps the fleet's own heartbeat or Hilda Hippo wraps two retailers, Huckle Cat is built **around the shared brain itself**. His input is seven disparate data sources (Gmail, Google Calendar, Google Contacts, Google Messages, WhatsApp, meeting transcripts, and Workflowy) and his output is a relationship intelligence layer: ~280 people files in the brain with names, emails, phones, circles, last-interaction timestamps, enriched context notes, and facts pulled from email signatures. He composes a morning relationship nudge at 5 AM PT (overdue / approaching / healthy), triages a shared notes inbox twice a day, and keeps `last_interaction` fresh via a daily re-mining pass. He was the last agent in the fleet to deploy, and he is the only one where the [mining pipeline](#the-mining-pipeline) runs **before** the first cron fires — by design.

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

**Stale `last_interaction` timestamps are the failure mode you will actually hit.** The mining pipeline runs once at deploy time and stamps every contact's `last_interaction` field with the most recent message found in the scan. If nothing updates those timestamps afterwards, the morning nudge will, within days, start flagging contacts the operator has interacted with since — which is the textbook way to lose operator trust in a relationship-nudge agent. The `daily-refresh` cron (landed 2026-04-14) exists specifically to fix this; see [§ The stale-dates bug and the daily-refresh fix](#the-stale-dates-bug-and-the-daily-refresh-fix).

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
| `whatsapp-mine.py` | WhatsApp session logs | Message participants; group chats split per non-operator author |
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

The pattern generalizes: if you have an important data source with no API, a hand-rolled DevTools snippet run from a persistent-profile browser gets you 80% of the way to "this data is now part of the brain." It is the same shape as Hilda Hippo's Camoufox-based auth flows, minus the MFA.

## The stale-dates bug and the daily-refresh fix

Huckle Cat deployed on 2026-04-12 with 279 seeded people files. Each people file had a `last_interaction` field stamped with the most recent interaction found during the one-time mining run. That was the operator state on day 1.

Two days later, the morning relationship nudge started flagging contacts the operator had definitely spoken to since April 12. The nudge for `\[person X\]` said "last interaction 6 days ago, overdue," and the operator had texted `\[person X\]` that morning. The nudge for `\[person Y\]` said "last interaction 9 days ago, approaching threshold," and the operator had a calendar event with `\[person Y\]` the previous afternoon.

The root cause was simple in retrospect: **the mining pipeline ran once, and the people files were frozen from that moment forward.** The morning relationship nudge was reading a snapshot from April 12 every day, and every day the snapshot was more wrong.

The 2026-04-14 fix introduced `daily-refresh.py`, a new host cron that runs at `0 10 UTC` (3:00 AM PT), re-mines a 14-day rolling Gmail + Google Calendar + Google Messages window, and updates the `last_interaction` field in the affected people files in place. The morning relationship nudge at `30 10 UTC` then reads the refreshed `last_interaction` values and produces an output that reflects the real state of things.

Three second-order details fell out:

1. **Meeting transcript attendee-count cap.** The transcript source can also update `last_interaction` — a 1:1 meeting transcript with person X is strong evidence the operator interacted with person X that day. But a 12-attendee all-hands meeting is not evidence of a real interaction with any individual attendee. The fix caps attendee-based refresh at 6 participants; meetings larger than that don't stamp `last_interaction` on anybody. This prevents the operator's "overdue" list from being silently cleared by their weekly all-hands.

2. **Cron ordering is load-bearing.** `gmessages-mine` runs at `0 */2 UTC` (every 2 hours, writes `cache/mined-gmessages.json`). `daily-refresh` runs at `0 10 UTC` (reads `mined-gmessages.json`, updates people files). `morning-relationship-nudge` runs at `30 10 UTC` (reads updated people files, composes brief, writes `cache/morning-brief-ready.txt` for fleet-deliver at `0 12 UTC`). The 30-minute gap between refresh and nudge is slack for the refresh cron — if you make them adjacent, a slow refresh run produces a nudge against stale data. Keep the slack.

3. **Cache freshness is not verified.** `morning-relationship-nudge` does *not* currently check whether `cache/mined-gmessages.json` is fresh before composing the brief. If `gmessages-mine` fails silently for a week, the nudge will quietly start using 7-day-old Google Messages data. This is a known gap — see [Pitfalls](#pitfalls).

## Current state

As of 2026-04-15, Huckle Cat runs four host crons off `~/.clawford/connector-workspace/`.

**Host cron surface.** Registered via `ops/scripts/install-host-cron.sh`:

| Cron | Schedule (UTC) | What it does |
|------|----------------|--------------|
| `gmessages-mine` | `0 */2 * * *` | Camoufox + DevTools JS snippet, scrapes Google Messages Web, writes `cache/mined-gmessages.json` |
| `daily-refresh` | `0 10 * * *` | Re-mines 14-day Gmail + GCal + GMessages window; updates `last_interaction` in people files; writes `cache/upcoming-meetings.json` |
| `morning-relationship-nudge` | `30 10 * * *` | Calls `people-scan.py`, groups by overdue / approaching / healthy, writes `cache/morning-brief-ready.txt` for fleet-deliver at `0 12 UTC` |
| `notes-triage-alert` | `0 8,20 * * *` | Reads `notes/inbox.md`, LLM-classifies new entries into `{fact, commitment, task, shopping, unclear}`, sends twice-daily Telegram digest with inline `/confirm N` + `/dismiss N` |

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
  mined-gmessages.json    # gmessages-mine output, read by daily-refresh
  upcoming-meetings.json  # daily-refresh output, read by nudge
  pending-triage.json     # notes-triage pending state
scripts/
  mine/                   # the one-time mining pipeline (7 miners + aggregator + enricher)
  gmessages-auth.py       # Camoufox QR pairing
  gmessages-mine.py       # ongoing scraper
  daily-refresh.py        # 14d rolling refresh
  people-scan.py          # deterministic scan called by nudge
  morning-relationship-nudge.py  # the orchestrator
  notes-triage-alert.py   # the triage orchestrator
```

**Brain state** lives outside the workspace, in the shared brain on Dropbox — this is the point of Huckle Cat. After the initial mining run, the operator's shared brain contains `~280` people files under `people/` and `~240` durable facts under `facts/`, all enriched from the mining pipeline.

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

> 🧨 **Pitfall.** LLM enrichment cost scaling linearly with contact count. **Why:** the enrichment pass runs one LLM call per candidate, regardless of whether the candidate is high-tier or marketing. On a first deploy of 280 people, that is `\~$3`. On a second-pass mining run of 600 people (because the gmail history grew or because a new source was added), it is `\~$6`. There is no safeguard against runaway spending — if a bug in the aggregator produces 5000 candidates, the enrichment pass will cheerfully run 5000 LLM calls. **How to avoid:** the aggregator has a candidate-count guard: if the final candidate list exceeds 1000, the pipeline hard-stops with `"Candidate count {N} exceeds safety ceiling — investigate before proceeding"` and the operator has to explicitly override with `--i-know-what-i-am-doing`. Do not disable the guard lightly.

## See also

- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the deploy path and safeguard story
- [Ch 08 — Your first agent](08-your-first-agent.md) — the general deploy walkthrough
- [Ch 12 — Mistress Mouse 🐭📅](12-mistress-mouse.md) — canonical Google OAuth pattern
- [Ch 13 — Sergeant Murphy 🐷🔍](13-sergeant-murphy.md) — the cache-is-not-a-delivery-queue rule that all of Huckle Cat's orchestrators follow
- [Ch 17 — Auth architectures](17-auth-architectures.md) — the cross-agent auth reference (pending)
