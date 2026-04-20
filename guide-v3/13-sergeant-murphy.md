# Sergeant Murphy 🐷🔍 — the meetings-coach agent

*Last updated: 2026-04-18 · Reading time: ~20 min · Difficulty: hard*

> **TL;DR.** Sergeant Murphy is the meetings agent — not a calendar agent, a meetings agent. He composes a morning meeting brief at 5 AM PT with factual context for every meeting on the day (no invented talking points), fires pre-meeting alerts 15–45 minutes ahead with the real agenda, scans meeting transcripts from an MCP-speaking transcription provider after each meeting to stage action items + decisions for confirmation, tracks the commitments that actually get confirmed, and runs a coaching analysis against a configured set of communication growth areas. He is the **second** Google-OAuth agent in the fleet (after [Mistress Mouse](12-mistress-mouse.md)) and he sits on the other side of [§ the routing boundary](12-mistress-mouse.md#the-routing-boundary-with-sergeant-murphy): Workflowy-presence events are his, non-Workflowy events are Mistress Mouse's. Read [§ The 5x resend incident](#the-5x-resend-incident) before deploying the post-meeting scan. It is the reason the rest of the fleet treats "cache files are not a delivery queue" as a named design rule.

## Meet the agent

Sergeant Murphy is the Richard Scarry pig detective — an officer who shows up with the facts already in hand. In a Clawford fleet, his job is the four-part meeting loop that every operator with a full work calendar wants to automate but almost nobody does well:

1. **Pre-meeting preparation.** Assemble a factual brief from Google Calendar + attendee context + the operator's Workflowy agenda before the meeting starts. No invented talking points, no AI-generated "what you should say." The brief is the stuff the operator would look up by hand in the ten minutes before a call, delivered ahead of time so the operator doesn't have to.

2. **Post-meeting debrief.** After the meeting, pull the transcript from the MCP-speaking transcription provider, extract action items and decisions, stage them as a pending debrief that the operator confirms via Telegram. Until confirmation, nothing lands in the permanent commitment log.

3. **Commitment tracking.** Once confirmed, commitments land in `commitments/active.md` in the shared brain, tagged with the source meeting. A daily cron at 9 AM PT surfaces overdue commitments and commitments approaching their `by_when` date.

4. **Meeting coaching.** For meetings flagged as coaching targets, run a deterministic metrics pass (talk ratio, turn length, fillers, longest turn) plus an LLM utterance-analysis pass against a configured growth-areas list, append the result to a coaching history, and surface a coaching message alongside the debrief.

The frame is deliberately narrow: Sergeant Murphy does **not** write your meeting notes for you. He gives you the facts, watches the transcript, stages what he thinks needs tracking, and waits for you to say yes.

## Why you'd want one

- An operator with 6+ real meetings a week who is tired of preparing by hand and who wants a system of record for commitments made in those meetings.
- Pre-meeting briefs that include real agenda items (from Workflowy) and real attendee history (from past meetings with the same person), not hallucinated talking points.
- A post-meeting debrief that is idempotent — you can confirm it once, twice, or never, and the state never gets weird — and that writes commitments into a place you can grep three weeks later.
- Coaching that is transcript-grounded with specific timestamps, not generic feedback. If the coaching says "you interrupted at 14:32 during a decision point," you can replay that exact moment.

## Why you might skip this one

- You already have a human assistant who does pre-meeting prep and you like it that way. Sergeant Murphy is a substitute for the prep workflow, not an addition to it.
- You don't use Workflowy (or a similar outline tool) as your work-planning surface. The pre-meeting agenda pull is Workflowy-shaped; porting it to Notion/Obsidian/Bear is possible but it's a real rewrite.
- Your meeting transcription provider doesn't speak MCP. The MCP-over-OAuth integration is the only auth shape wired up in this agent; other transcription providers would need a fresh adapter at the `transcript-scan.py` seam. See [§ The MCP transcription integration](#the-mcp-transcription-integration) for the shape of the work.
- You don't want the LLM touching meeting transcripts at all. The coaching feature is optional and can be disabled, but the post-meeting debrief's action-item extraction is load-bearing for the commitment-tracker side, and that path does read the transcript with an LLM. If that's a nonstarter, deploy a different agent.

## What makes this agent hard

Four things.

**Post-meeting idempotency is a hard problem, not an easy one.** A cron that fires every 30 minutes and that re-reads a staging directory every run will, by default, re-send the same output every 30 minutes until something invalidates the staging state. For a scrape cron that's annoying. For a cron that composes an LLM-written debrief and sends it to a human over Telegram, that's a 5x resend incident. The design rule that came out of the incident — "cache files are never a delivery queue" — is the rule that makes the post-meeting scan safe, and the whole section of this chapter on [§ The 5x resend incident](#the-5x-resend-incident) is about why that rule exists.

**MCP transcription integration is the first MCP consumer in the fleet.** Everything else in a default Clawford fleet talks to vendor APIs over plain HTTP with API keys or OAuth tokens. The transcription provider used here speaks the Model Context Protocol, which is its own surface area: an SDK, a different auth pattern (OAuth 2.1 with the same local-then-SCP refresh flow as Google OAuth), and a different retry story on 401s. See [§ The MCP transcription integration](#the-mcp-transcription-integration).

**Pre-meeting brief composition is deterministic on purpose, and the cron that composes it has to stay that way.** The original deploy (2026-04-08) carried a hard constraint: *factual schedule only, no generated talking points.* That constraint is easy to honor on day 1 and easy to erode on day 30, when it feels like the agent could "just summarize" the attendee's recent activity or "just generate" a quick opener. Don't. The trust contract with the operator is that the brief is reporting, not composition. Every talking point in the brief is a talking point the operator declared in Workflowy, not one the LLM made up.

**The routing boundary with Mistress Mouse has to be checked every run, in both directions.** [Mistress Mouse](12-mistress-mouse.md) owns non-meeting calendar events (doctor appointments, kid activities, social plans). Sergeant Murphy owns meetings. The asymmetric tell — Workflowy presence — means every run of every script in both agents has to look up Workflowy for each event before acting. If the Workflowy session has expired on either side, both agents can over-reach in opposite directions. The [§ routing boundary section in Ch 12](12-mistress-mouse.md#the-routing-boundary-with-sergeant-murphy) has the bright-line rule; the [Ch 12 routing pitfall](12-mistress-mouse.md#pitfalls) is about the other side of the failure mode.

## The 5x resend incident

Every agent in this guide has a load-bearing war story. Mr Fixit has his probation episode. Huckle Cat has the stale-dates bug. Sergeant Murphy has **the 5x resend incident**, and the design rule that came out of it is the single rule that makes cron-based staging-and-confirmation flows work anywhere else in the fleet.

### The silent prior

The bug actually first fired on **2026-04-08**, the day Sergeant Murphy deployed. A post-meeting debrief for an early-morning meeting was staged as `cache/pending-debrief-{event-id}.json`, the operator did not immediately reply with `/confirm`, and the post-meeting-scan cron fired again 30 minutes later, composed a fresh debrief (different wording each time, because LLM composition is non-deterministic), fired again, and again — five times in total over roughly 2.5 hours. The operator didn't notice. The duplicates landed as five separate entries in `coaching-history.json` with five slightly different assessments of the same meeting, and the incident was discovered only during the post-mortem of the next event six days later.

### The loud incident

On **2026-04-14**, the same bug fired on a post-meeting debrief for a mid-morning meeting and this time the operator was online, saw the first debrief at `15:00`, saw the second at `15:30`, saw the third at `16:00`, and pulled the cron by hand before the sixth. The Telegram channel had five debriefs for one meeting, five different coaching messages with five different sets of "moments to reflect on," and five separate appends to `coaching-history.json` — each with an earnest and slightly different LLM reading of a meeting that only happened once.

### The root cause

The post-meeting-scan cron's prompt said, in effect: *for each `cache/pending-debrief-*.json` file in the staging directory, check whether its `event_id` already appears in `commitments/active.md`; if not, compose the debrief and send it.* That sounds reasonable. It is not.

The failure mode is that `commitments/active.md` is only written **after** the operator confirms via `/confirm`. Until the operator confirms, the pending file persists, the grep check always returns "not found," and the cron composes-and-sends again on every tick. The staging directory is not a queue of things to send — it is a *workbench*, where the agent stages candidate commitments for human review. Treating it as a delivery queue means every unconfirmed meeting becomes a resend loop until the operator either confirms or manually deletes the pending file.

The second-order failure mode is that `coaching-history.json` is append-only, so every re-run of the scan re-appended a coaching entry. The commitment side of the agent had some accidental protection (the `/confirm` flow requires a human in the loop, so the worst that happens is the debrief looks noisy), but the coaching side silently accumulated duplicate assessments without any gate at all.

### The fix

The 2026-04-14 fix ("meetings-coach: fix post-meeting-scan re-sending same debrief") rewrote the cron's behavior against a completely different source of truth. The new rule is:

**Delivery is gated only on the current run's `transcript-scan.py` output.** If `transcript-scan.py` returns `processed: [event_id_A, event_id_B]`, then this run sends debriefs for events A and B — and nothing else. Nothing that happens in the cache directory can cause a send. The script-level `processed-transcripts.json` dedup file is the single source of truth for what has already been handled, and `transcript-scan.py` never re-emits an event id it has already recorded in that file.

Three invariants fell out:

1. **Cache files are a workbench, not a queue.** The `cache/pending-debrief-*.json` files still exist, and they still get written by `transcript-scan.py`, and they still get consumed by the `/confirm` flow — but the post-meeting-scan cron **never reads them to decide what to send.** A cleanup pass deletes files whose event id has landed in `commitments/active.md`, but that cleanup pass is explicitly scoped to cleanup only.

2. **Append-only state files need a grep-before-append gate.** `coaching-history.json` grew a check: before appending an entry for `event_id: X`, grep the file for any existing entry with that same event id; if found, skip. The invariant is enforced in Python, not in the cron prompt.

3. **Idempotency is load-bearing and gets a regression test.** `test_post_meeting_scan_idempotency.py` landed in the same commit. It asserts against the real manifest that running the post-meeting-scan cron twice in a row produces a second run with `sent: 0`. A regression test here is cheap and the failure it guards against is very expensive in operator-trust terms.

### The design rule

The incident codified a memory rule that now applies to every cron-based staging-and-confirm flow in the fleet, not just Sergeant Murphy: **delivery decisions are made from the current run's script output, never from iterating a cache directory.** Staging files are the human confirmation surface. They are not a delivery queue. Any cron that treats a staging file as a delivery queue will resend until a human intervenes, and the resend loop will fire at the operator with an LLM-composed message that rewords itself each tick — which is the worst possible failure mode because it looks like genuine activity.

Every other agent in the fleet now has a script-output contract at the cron boundary for the same reason. Lowly Worm's newsfeed cron gates delivery on `cache/morning-brief-ready.txt`. Mistress Mouse gates on the same file. The morning fleet deliver gates on `cache/morning-brief-ready.txt`. They are all variations on the same rule: a marker file written exactly once per composition run is the thing that authorizes delivery, and an open staging directory of pending items is not.

## The MCP transcription integration

Sergeant Murphy's post-meeting scan talks to a meeting-transcription provider over the Model Context Protocol. This is the only MCP consumer in a default Clawford fleet, and the integration has a few quirks worth calling out before deployment.

**Auth pattern matches Google OAuth.** The transcription provider uses OAuth 2.1 for consent. The same local-then-SCP pattern from [§ The Google OAuth pattern in Ch 12](12-mistress-mouse.md#the-google-oauth-pattern) applies: run `transcript-auth-manual.py` locally, consent flow pops a browser, token lands in `cache/transcript-token.json`, SCP to the VPS workspace. Refresh tokens auto-refresh under normal operation.

**401s require manual re-auth.** Unlike Google's refresh tokens, which effectively never expire, transcription-vendor OAuth tokens have been observed to get invalidated on the order of months — a timeline that is long enough to forget about and short enough to be surprising. The soul-level boundary on Sergeant Murphy is explicit: **never authenticate automatically.** If the token fails to refresh, the agent alerts the operator and waits. Re-auth is a manual process where the operator runs the auth script on the local laptop, consents, and SCPs the new token.

**Alerts are rate-limited to 90 minutes.** The first version of the 401 handler fired a Telegram alert on every run of every cron that touched the provider — so a token expiration at 3 AM PT meant 6–8 alerts before the operator woke up. The 2026-04-12 fix ("meetings-coach: truth-tell transcript auth + rate-limit 401 alerts") added a 90-minute alert dedup: the first 401 of a new session writes `cache/transcript-last-alert.json`, and subsequent 401s within 90 minutes check that file and silently skip the alert. The dedup resets on successful re-auth.

**The MCP SDK uses `get_multiple_documents`, not `get_document`.** The 2026-04-08 initial deploy fixed a subtle API mismatch: `transcript-scan.py` was built against a single-document fetch, but the vendor's MCP server only exposes a batch-fetch tool. The script now always calls `get_multiple_documents`, even for a one-event fetch. If you're porting the integration to a different transcription vendor, check which MCP tools their server actually exposes; don't assume `get_document` is there.

## The routing boundary with Mistress Mouse

The full explanation of the routing rule lives in [§ The routing boundary section of Ch 12](12-mistress-mouse.md#the-routing-boundary-with-sergeant-murphy). The short version (after the 2026-04-18 revision): **a calendar event is Sergeant Murphy's iff it has a videoconference link OR the operator has linked it in Workflowy.** Otherwise it's Mistress Mouse's.

Both agents read the same classification — a shared brain calendar index at `~/Dropbox/clawford-backup/status/calendar-index.json`, rebuilt once per morning tick by Mouse's `calendar-index-build.py` — rather than each running their own classifier inline. Sergeant Murphy's orchestrators (morning-meeting-brief, pre-meeting-alert, post-meeting-scan) still fall back to a local `has_videoconference_link` check for events the index hasn't seen yet.

The failure mode to watch for is the Workflowy session expiring — see the [routing pitfall in Ch 12](12-mistress-mouse.md#pitfalls). A dead Workflowy session means the `workflowy-links.json` cache stops updating, and any meeting whose only signal was the Workflowy tag will misroute. Video-link meetings still get classified correctly in that degraded state; only in-person meetings the operator tagged for coaching slip through to Mistress Mouse. The fix is to refresh the Workflowy session, not to patch either agent's routing code.

## Current state

As of 2026-04-15, Sergeant Murphy runs five host crons off `~/.clawford/meetings-coach-workspace/`.

**Host cron surface.** Registered via `ops/scripts/install-host-cron.sh`:

| Cron | Schedule (UTC) | What it does |
|------|----------------|--------------|
| `morning-meeting-brief` | `50 11 * * *` | 6-step pre-meeting brief chain: fetch calendar, bootstrap missing attendees, sync Workflowy nodes, prep per-meeting context, format brief, write `cache/morning-brief-ready.txt` for fleet-deliver at `0 12 UTC` |
| `pre-meeting-alert` | `*/30 * * * *` | Filter meetings starting in 15–45 min, fire factual Telegram alerts with real Workflowy agenda (dedup via `sent-alerts.json`, pruned every 48h) |
| `post-meeting-scan` | `15,45 * * * *` | Fetch transcripts from MCP vendor, extract action items + decisions, stage pending debrief, run coaching metrics, append coaching history — **gates delivery on `transcript-scan.py` output only** (see [§ 5x resend incident](#the-5x-resend-incident)) |
| `commitment-follow-up` | `0 16 * * *` | Read `commitments/active.md` filtered by `source_agent: meetings-coach`, flag overdue + approaching items, silent when nothing actionable |
| `heartbeat` | `*/30 * * * *` | Health + auth probe; writes `HEARTBEAT.md`; sole writer of the status file |

**Supporting I/O scripts** (deterministic Python, subprocess-safe, called by orchestrators):

- `gcal-fetch.py` — multi-calendar reader with `is_real_meeting` flag and conference-link extraction
- `meeting-prep.py` — per-meeting context assembly (attendees + facts + open commitments)
- `workflowy-sync.py` — Workflowy node creation + search; owns the Workflowy session
- `transcript-scan.py` — fetches from MCP vendor, extracts action items + decisions, maintains `processed-transcripts.json` as the single source of truth for dedup
- `transcript-metrics.py` — deterministic metrics from speaker-attributed transcripts (talk ratio, turn length, fillers, longest turn)
- `commitment-tracker.py` — reads `commitments/active.md`, filters by agent + deadline, formats output
- `person-bootstrap.py` — creates attendee files in the shared brain for new people
- `timed-deliver.py` — holds composition until the fleet delivery window and atomically publishes to the ready marker
- `gcal-auth.py` — Google OAuth setup + refresh
- `transcript-auth-manual.py` — transcription vendor OAuth setup

**Workspace layout** under `~/.clawford/meetings-coach-workspace/`:

```
SOUL.md                    # immutable (chattr +i)
IDENTITY.md                # immutable
TOOLS.md                   # durable identity
AGENTS.md                  # durable identity
USER.md                    # operator profile (gitignored, template in repo)
HEARTBEAT.md               # sole-writer status file
MEMORY.md                  # persistent notes
meeting-config.json        # calendar IDs, timezone, coaching growth areas
token.json                 # Google OAuth (gitignored)
credentials.json           # Google OAuth client (gitignored)
cache/
  transcript-token.json    # transcription vendor OAuth (gitignored)
  processed-transcripts.json  # dedup source of truth (post-incident)
  coaching-history.json    # grep-before-append (post-incident)
  pending-debrief-*.json   # workbench, NOT a delivery queue
  sent-alerts.json         # pre-meeting alert dedup
scripts/                   # all Python listed above
```

## Deployment walkthrough

This is the overlay on [Ch 08 — Your first agent](08-your-first-agent.md). Read Ch 08 first, then the items below.

**Pre-step: Google Cloud project setup.** Same as [Ch 12](12-mistress-mouse.md#deployment-walkthrough). If [Mistress Mouse](12-mistress-mouse.md) is already deployed, reuse the same Google Cloud project and credentials; no need to create a second project. Add the Google Calendar API and Gmail API. Verify the operator is in the OAuth consent screen test-users list.

**Pre-step: transcription vendor account + MCP server access.** Sign up for the transcription provider, enable MCP server access in the vendor's developer settings, note the consent URL and OAuth client ID. The pattern is vendor-specific but the shape is OAuth 2.1 consent + a refresh token that lives in `cache/transcript-token.json`.

**Pre-step: Workflowy bearer token.** Sergeant Murphy's Workflowy integration uses a bearer token stored in the agent's `.env` file. Generate the token from the Workflowy developer settings, paste it into `~/.clawford/meetings-coach-workspace/.env` as `WORKFLOWY_TOKEN=...`, and verify with `python3 agents/meetings-coach/scripts/workflowy-sync.py --ping`.

**Step 3a: Run Google OAuth locally.** `python3 agents/meetings-coach/scripts/gcal-auth.py`. Browser pops, consent flow runs, `token.json` lands. Verify locally with `gcal-fetch.py --days 1`, then SCP to `~/.clawford/meetings-coach-workspace/token.json` on the VPS.

**Step 3b: Run transcription vendor OAuth locally.** `python3 agents/meetings-coach/scripts/transcript-auth-manual.py`. Same local-then-SCP pattern. Token lands in `cache/transcript-token.json`.

**Step 3c: Configure `meeting-config.json`.** The template ships at `agents/meetings-coach/meeting-config.template.json` with placeholder calendar IDs, growth-area names, and meeting filters. Copy to the workspace, fill in real values, verify with `meeting-prep.py --ping`.

**Step 5: Register the host crons.** Add five entries to the `CONTRACT_ENTRIES` block in `ops/scripts/install-host-cron.sh` under the `meetings-coach` section. Each entry calls `python3 ~/repo/agents/meetings-coach/scripts/{cron-name}.py`. Run `install-host-cron.sh` on the VPS — it drift-detects and rewrites the crontab idempotently.

**Step 6: Deploy.** `python3 agents/shared/deploy.py meetings-coach` on the VPS. Verify with `python3 agents/meetings-coach/scripts/post-meeting-scan.py --dry-run`.

**Step 7: Wait for the `*/30` tick.** The first pre-meeting-alert cron fires and prints its result to the log. Check `~/.clawford/meetings-coach-workspace/HEARTBEAT.md` and the Telegram channel. If everything is green, the next morning-meeting-brief at `50 11 UTC` produces the first real composed brief.

**Step 8: Before enabling coaching.** The coaching side of the post-meeting scan is optional and is off by default. To enable it, set `coaching.enabled: true` in `meeting-config.json` and verify the growth-areas list matches the operator's intent. Run the coaching path in dry-run mode against a single meeting first (`post-meeting-scan.py --dry-run --coaching-only`). The coaching output is opinionated about the operator's communication patterns, and it is worth previewing the tone before letting it land in a live Telegram stream.

## Pitfalls

> 🧨 **Pitfall.** Treating `cache/pending-debrief-*.json` as a delivery queue in any cron prompt or script. **Why:** this is the root cause of the 5x resend incident. Staging files are a workbench for human confirmation, not a queue of messages to send. Any cron that iterates `cache/*.json` to decide what to send will resend until a human intervenes, and the resend loop is composed by an LLM that rewords each iteration — which looks like genuine activity and is very hard to detect in real time. **How to avoid:** delivery decisions come from the current run's `transcript-scan.py` output only. `test_post_meeting_scan_idempotency.py` enforces this invariant. If you add a new orchestrator cron that touches the cache directory, add a regression test in the same commit that asserts "running twice in a row produces zero sends on the second run."

> 🧨 **Pitfall.** Appending to `coaching-history.json` (or any other append-only state file) without a grep-before-append gate. **Why:** the silent-prior half of the 5x resend incident was five duplicate coaching entries that nobody saw until six days later, because append-only files accumulate quietly. The grep-before-append check is one extra line of Python and it is the difference between a drift-silent failure and a hard-fail. **How to avoid:** every append to `coaching-history.json` reads the file first, greps for any existing entry with the same `event_id`, and skips if found. The check is enforced in Python code, not in the cron prompt — because cron prompts are easy to "improve" and invariants that live only in prompts do not survive the first rewording.

> 🧨 **Pitfall.** The pre-meeting brief learning to "generate talking points." **Why:** the deploy-day constraint (2026-04-08) was *factual schedule only, no generated talking points.* The operator's trust contract with Sergeant Murphy is that the brief is reporting, not composition. The moment the LLM starts composing "suggested openers" or "points to raise," the brief becomes untrustworthy as a source of truth — the operator can no longer skim it at 5 AM PT and assume every sentence is something they already know. **How to avoid:** the brief format is pure Python. No `agents.shared.llm.infer` call in `morning-meeting-brief.py`. If a future feature needs LLM composition, it goes in a separate script that explicitly labels its output as "proposed" and routes through a confirmation flow.

> 🧨 **Pitfall.** Running the transcription vendor auth flow on the VPS. **Why:** same reason as [Google OAuth on the VPS](12-mistress-mouse.md#pitfalls) — the consent flow needs a real browser, and the VPS doesn't have one. Trying to run it headless wastes 20 minutes and leaves a half-authorized state behind. **How to avoid:** run `{vendor}-auth-manual.py` on the operator's laptop, verify with a one-shot `transcript-scan.py --ping`, then SCP `cache/{vendor}-token.json` to the VPS workspace.

> 🧨 **Pitfall.** Treating a transcription vendor 401 as "I should retry in 30 seconds." **Why:** vendor OAuth tokens can get invalidated outside the agent's control (vendor-side session cleanup, security-event revocations, policy changes). A 401 means the refresh token no longer refreshes, period. Retrying on a hot loop burns alerts and cron budget and does not help. **How to avoid:** the 401 handler stamps `cache/{vendor}-last-alert.json` on first failure, rate-limits subsequent alerts to 90-minute windows, and waits for manual re-auth. The operator runs the auth script locally, SCPs the new token, and the next cron recovers automatically.

> 🧨 **Pitfall.** Confirm-flow double-writing commitments. **Why:** the `/confirm` handler reads the pending debrief file, writes commitments to `commitments/active.md`, and deletes the pending file. If the operator sends `/confirm` twice in quick succession (once before the first handler completes), both handlers read the same pending file and both write — resulting in duplicated commitments in `active.md`. The specified fix is to grep `commitments/active.md` for `event_id: {EVENT_ID}` before writing, and refuse with "Already confirmed" if found. **How to avoid:** the grep-before-write check is specified in `SOUL.md.example` but check it is actually implemented in the confirm-handler code before deploying. If the check is missing, the agent will appear to work under normal operator cadence and silently double-write under high-variance load.

> 🧨 **Pitfall.** The Workflowy session expiring silently on Sergeant Murphy's side. **Why:** same underlying failure as the [Ch 12 Workflowy pitfall](12-mistress-mouse.md#pitfalls), but on the other side of the routing boundary. When the Workflowy session is dead, `workflowy-sync.py --search` returns empty for every query, which Sergeant Murphy interprets as "this event is not a meeting" — and he drops real meetings from the morning brief entirely. Symptom: the brief is suspiciously short and the operator walks into a meeting cold. **How to avoid:** the heartbeat cron includes a `workflowy_auth` probe that actively verifies session validity (not just file existence — that was the false-positive bug fixed 2026-04-15). If `workflowy_auth` flips to `fail` in the heartbeat, the operator needs to refresh the Workflowy bearer token in `.env` before the next morning brief composes.

> 🧨 **Pitfall.** Skipping the dry-run preview before enabling the coaching feature. **Why:** the coaching analysis is transcript-grounded and deterministic in metrics but opinionated in tone. If the growth-areas list is mis-configured (for example, the operator's real growth area is "concision" but the config lists "empathy," which the operator already considers a strength), the coaching messages will read as off-key feedback from an LLM that thinks it knows the operator better than it does. That's the fastest way to lose operator trust in the agent. **How to avoid:** run `post-meeting-scan.py --dry-run --coaching-only` against a sample meeting or two, read the full coaching output, and verify the tone matches the operator's intent before flipping `coaching.enabled: true`. If the tone is off, the growth-areas list is the lever to fix — not the cron code.

## See also

- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the deploy path and safeguard story
- [Ch 08 — Your first agent](08-your-first-agent.md) — the general deploy walkthrough
- [Ch 12 — Mistress Mouse 🐭📅](12-mistress-mouse.md) — the routing boundary partner, and the canonical Google OAuth pattern
- [Ch 14 — Huckle Cat 🐱🤝](14-huckle-cat.md) — the third Google-OAuth agent
- [Ch 17 — Auth architectures](17-auth-architectures.md) — cross-agent auth reference (pending)
