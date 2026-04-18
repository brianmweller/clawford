# Mistress Mouse 🐭📅 — the family-calendar agent

*Last updated: 2026-04-18 · Reading time: ~20 min · Difficulty: hard*

> **TL;DR.** Mistress Mouse is the household-logistics agent: she reads a family's Google Calendars, composes a morning briefing delivered at 5 AM PT, fires 60/30/15-minute reminders for the events that matter today, parses activity-provider emails (school closures, cancellations, signup windows), surfaces Google Calendar invites that actually need a response, and summarizes WhatsApp family-chat traffic into a once-a-day digest. She is also the first agent in a Clawford fleet to go through Google OAuth — the local-auth-then-SCP pattern she pioneered is the same pattern [Sergeant Murphy](13-sergeant-murphy.md) and [Huckle Cat](14-huckle-cat.md) reuse. Read [the WhatsApp section](#the-whatsapp-chapter) before binding her to a real phone number — there is a live ban risk on personal accounts. Read [the routing boundary section](#the-routing-boundary-with-sergeant-murphy) before deploying her alongside Sergeant Murphy.

## Meet the agent

Mistress Mouse is the Richard Scarry character — a mouse who runs the household. Her job in a Clawford fleet is family logistics: the stuff that a human operator tracks across two Google accounts, four or five subscribed calendars, a shared school portal, a partner's events calendar, and a family WhatsApp group, and that falls on the floor the moment the operator gets busy for a week.

She is a **family** calendar agent, not a work calendar agent — Sergeant Murphy owns work meetings. The bright line between the two is in [§ The routing boundary with Sergeant Murphy](#the-routing-boundary-with-sergeant-murphy). If you only need one of the two, deploy Sergeant Murphy if your bottleneck is post-meeting coaching and deploy Mistress Mouse if your bottleneck is "my kid's school closed and I didn't know until noon."

## Why you'd want one

- A morning briefing every day at 5 AM PT that already knows which events are yours, which are your partner's, which are shared, and which kid has what. The briefing composes at `30 10 UTC` and delivers at `0 12 UTC` as part of the fleet 5 AM PT path.
- Three-tier reminders (60 min / 30 min / 15 min) for events on your own calendars, with per-calendar opt-out so a partner who finds reminders annoying can be skipped automatically.
- Activity-provider emails (school closures, lesson cancellations, signup windows) parsed and classified into five categories with a Telegram alert per non-trivial item, so the "the school emailed at 6 AM and nobody saw it" class of failure goes away.
- Google Calendar invite surfacing — the common `METHOD:REQUEST` stuff you actually need to respond to, filtered out of the noise of calendar updates and cancellations.
- A once-a-day digest of family WhatsApp chat traffic, delivered to Telegram, so the "partner sent a schedule change in WhatsApp and I missed it because I was heads-down" failure mode goes away.

## Why you might skip this one

- If your household has zero Google Calendar footprint, she has nothing to read. Port the scripts to iCloud or Outlook if that's your world, but that's a rewrite, not a config change.
- If you don't have an activity-provider email corpus to parse (no school, no lessons, no signups), the activity-email cron is dead weight. Disable it and deploy the rest.
- If your partner doesn't share a calendar with you, the per-calendar routing loses most of its value.
- If you don't want to run WhatsApp at all — and [§ The WhatsApp chapter](#the-whatsapp-chapter) below is a strong argument for why you might not — the two WhatsApp crons can be disabled without affecting the rest of the agent.

## What makes this agent hard

Three things, roughly in order of how long each took to get right.

**Google OAuth is not hard, but it's the first piece of the fleet that does not run on API keys.** Everything before Mistress Mouse — Mr Fixit, Lowly Worm, Hilda Hippo — either talks to public web pages (Playwright) or talks to vendor APIs that take an API key (OpenAI, Telegram, the news sources). Google Calendar and Gmail require a full OAuth2 consent dance with a browser, a redirect URL, a consent screen, test users, and a refresh token that lives on disk. That story is in [§ The Google OAuth pattern](#the-google-oauth-pattern) below. It is the reusable chapter of this guide — Sergeant Murphy and Huckle Cat both reuse it verbatim.

**Family calendar logistics are not a generic calendar problem.** A work calendar has one person. A family calendar has two adults with two Google accounts, three or four kids with activities, a school district, a couple of sports subscriptions, and a partner whose preferences about reminders are different from the operator's. The per-calendar `remind` flag (landed 2026-04-12) is what lets this work — every calendar in `calendar-config.json` has an explicit opt-in or opt-out for reminders, so the agent never sends a "🔴 NOW" Telegram alert about the partner's Pilates class at a time that would be interpreted as a snarky nudge. The scar tissue that forced this flag is in [Pitfalls](#pitfalls) below.

**Family WhatsApp is a liability surface, not a feature surface.** See [§ The WhatsApp chapter](#the-whatsapp-chapter). The short version is that the library Mistress Mouse uses to read WhatsApp (Baileys) gets personal numbers banned by Meta on a non-deterministic schedule, and every outbound message from this agent must go to Telegram for the operator to forward, never to a WhatsApp group directly. There is a three-rule contract in [§ The WhatsApp chapter](#the-whatsapp-chapter) that is non-negotiable if you deploy the WhatsApp side of this agent at all.

## The Google OAuth pattern

Mistress Mouse was the first agent in the fleet to need a browser-based auth flow. She deployed on 2026-04-08 and the OAuth story was load-bearing for her first cron to fire, so it got solved under time pressure and then promoted to a pattern that the rest of the fleet reuses. (She was tracked as part of the phase-4a liberation rollup on 2026-04-14, but the initial pre-liberation deploy was 2026-04-08.)

The pattern is five rules.

**Rule 1 — Desktop credentials, not service-account credentials.** Service accounts do not work for personal Google Calendar or personal Gmail. The only credential type that reads a personal Google account is an OAuth2 Desktop Client, created in Google Cloud Console under "Credentials → Create credentials → OAuth client ID → Desktop app." The credentials download as a `credentials.json` file that lives in the agent's workspace (gitignored) and never touches git.

**Rule 2 — Add yourself as a test user before the first run.** The Google OAuth consent screen has an "add test users" section. If you skip it, the first consent attempt returns `Access blocked: {app name} has not completed the Google verification process`, and the error message buries the lede (it looks like a permissions problem; it is actually a "this app is in testing and you are not on the allowlist" problem). Add the operator's email to the test-users list and the flow works immediately. For a personal Clawford fleet you can stay in testing mode indefinitely — verification is only required if you plan to ship to non-test users.

**Rule 3 — Run the auth flow locally, never on the VPS.** `InstalledAppFlow.run_local_server(port=8080, open_browser=True)` pops open a browser on the local machine, runs the redirect dance against `localhost:8080`, and writes `token.json` to the current directory. Doing this on the VPS requires headless-browser automation (fragile), an X11 forward (slow and sometimes broken), or a manual URL copy-paste flow (error-prone). The local-then-SCP pattern sidesteps all of that: run the auth flow on the operator's laptop, verify the token works against a one-shot `gcal-fetch.py --days 1` test, then `scp token.json operator@vps:~/.clawford/family-calendar-workspace/token.json`. That pattern is what Sergeant Murphy and Huckle Cat both reuse.

**Rule 4 — The refresh token lives forever unless you revoke it.** Once you have a valid `token.json` with a refresh token, Google will keep refreshing it on demand indefinitely — no rotation, no manual re-auth, no expiration. The only things that invalidate the refresh token are: explicit user revocation (from the Google account security page), deletion of the Google Cloud project, or removal from the test-users list. None of those happen by accident. This is a fire-and-forget setup after the first local auth run.

**Rule 5 — Expanding scopes requires a new auth flow.** Mistress Mouse started with `calendar.readonly` scope (just enough to read events), then later added `calendar` (full write) and `gmail.readonly` (for invite parsing). Upgrading scopes requires running `InstalledAppFlow.run_local_server` again against a new scope list, which pops a fresh consent screen, which writes a new `token.json`. Mistress Mouse's auth-setup script (`google-auth-setup.py`, kept in the workspace but not run from cron) takes the full scope list as a constant and is safe to re-run at any time — it will short-circuit if the existing `token.json` already covers the requested scopes, and run the flow if not.

## The WhatsApp chapter

This is the section you must read before deploying the WhatsApp crons. It is also the section you might read and then decide to deploy Mistress Mouse without the WhatsApp side at all. Both are reasonable.

**The architecture.** Mistress Mouse reads WhatsApp via [Baileys](https://github.com/WhiskeySockets/Baileys), a reverse-engineered whatsapp-web library. Baileys binds to a personal WhatsApp account as a "linked device" — the same kind of pairing WhatsApp Web uses. The binding lives in a session directory under `~/.clawford/family-calendar-workspace/whatsapp-session/` and survives restarts as long as the session hasn't expired. Two crons consume it: `whatsapp-chat-alert` (every 2 hours, LLM-classified) and `whatsapp-schedule-post` (once a day at 5 AM PT).

**The ban risk.** Baileys is not an authorized API. Meta actively detects reverse-engineered clients and bans the personal phone numbers they are bound to, on a non-deterministic schedule. The ban is typically a hard number ban — the personal account is locked, and recovery requires contacting Meta support with a phone-ownership proof. In the worst case, the ban is permanent. **This is not a theoretical risk.** It is the reason the Clawford guide treats the WhatsApp binding as a liability surface and not a feature surface, and it is the reason every outbound message path goes through Telegram.

**The three-rule contract.** If you deploy the WhatsApp side of Mistress Mouse, these three rules are non-negotiable:

1. **Outbound goes to Telegram, never to a WhatsApp group.** The `whatsapp-schedule-post.py` cron composes a daily digest and sends it to the operator's Telegram, not to the family WhatsApp group. If the operator wants to forward the digest to the family group, that is a manual copy-and-paste action the operator performs by hand. The agent never initiates a group message. The scar tissue that forced this rule is in [Pitfalls](#pitfalls) below — the first draft of the cron sent the digest straight to the family group and had to be pulled within hours.

2. **`dmPolicy=disabled` on the bound account.** The bot framework layered on top of Baileys has a "respond to direct messages" mode that defaults to on in most templates. If you leave it on, the framework will auto-reply to any DM the bound account receives — including DMs from the operator's partner, in the operator's voice, with a pairing-code message header. This has happened. It is confusing to everyone involved. Set `dmPolicy=disabled` in the agent config before the first cron fires.

3. **One WhatsApp account per deployment.** Do not share the bound Baileys session across multiple agents. Huckle Cat (the connector agent) has its own WhatsApp-reading surface for mining contact relationships; if you deploy both Mistress Mouse and Huckle Cat, use two different WhatsApp accounts for the two bindings. A single account bound to two Baileys sessions at once is a fast path to a ban.

**The alternative: deploy without WhatsApp.** `whatsapp-schedule-post` and `whatsapp-chat-alert` can both be moved to `disabled-agents.txt` (see [Ch 06 — Infra setup](06-infra-setup.md)) without affecting the morning briefing, the reminder check, the activity-email cron, or the Gmail-invite cron. Mistress Mouse works fine without WhatsApp. Every other agent in the fleet that reads WhatsApp (currently just Huckle Cat) is also optional on the same grounds.

## The routing boundary with Sergeant Murphy

When Sergeant Murphy deployed on top of Mistress Mouse in mid-April, both agents immediately started touching the same Google Calendar entries — Murphy because meetings are calendar events, Mistress Mouse because events are calendar events. The operator got duplicate reminders, two different briefing formats, and cross-agent coordination noise.

The fix went through two revisions. The first (2026-04-12) was "Workflowy presence is the bright line" — every meeting worth coaching had a Workflowy node, so the asymmetry worked as a classifier. That held until mid-April when it became clear Murphy needed to pick up video-link meetings the operator hadn't taken the time to tag in Workflowy (and, separately, that Mistress Mouse was blanking event descriptions before her own classifier ran, so a Webex URL pasted into the description was invisible to her). The revised rule (2026-04-18):

**A calendar event is a *meeting* — and only Sergeant Murphy touches it — iff it has a videoconferencing link (Google Meet, Zoom, Teams, Webex) OR the operator has linked it in Workflowy. Otherwise it's an *event*, and only Mistress Mouse touches it.**

Physical signal OR human signal, with either flipping the ownership. The videoconference check covers every meeting the operator didn't think to tag (most of them). The Workflowy check remains the human override — an in-person coffee worth coaching on is Murphy's even with no meet link, if the operator tagged it.

The architectural shape also changed in the same pass. Before 2026-04-18 each agent ran its own classifier inline. That failed because Mistress Mouse deliberately drops event descriptions from her output (safety-by-default against rendering untrusted HTML to Telegram), which meant her classifier couldn't see description-embedded Webex URLs even when they were right there. The fix was to introduce a **shared brain calendar index**: once per morning tick (10:25 UTC, 5 min before the 10:30 fleet brief), `calendar-index-build.py` fetches every upcoming event on the raw Google Calendar API, classifies each one with the OR rule against the raw description, and writes `~/Dropbox/clawford-backup/status/calendar-index.json`. Both agents read the index as the authority. The local `has_videoconference_link` check remains on each agent as a fallback for events the index hasn't seen yet (e.g. something the operator just created mid-day).

If you deploy only Mistress Mouse and not Sergeant Murphy, the classifier still runs — everything will be classified as meeting or event, and Mistress Mouse will simply surface only the events. The index is still useful (and cheap) in single-agent mode; you do not need to disable it.

## Current state

As of 2026-04-15, Mistress Mouse runs entirely on host crons under `~/.clawford/family-calendar-workspace/`. Six live host crons, with six supporting I/O scripts and a small pile of state files.

**Host cron surface.** Registered via `ops/scripts/install-host-cron.sh`:

| Cron | Schedule (UTC) | What it does |
|------|----------------|--------------|
| `morning-briefing` | `30 10 * * *` | Compose today/tomorrow briefing (Monday adds week-ahead section); writes `cache/morning-brief-ready.txt` for fleet-deliver to pick up at 12:00 UTC |
| `reminder-check` | `*/5 * * * *` | Compare events against wall-clock, fire 60/30/15-min reminders, dedup via `sent-reminders.json`, skip per-calendar `remind=false` |
| `activity-email-alert` | `15 */2 * * *` | Fetch recent activity-provider emails, LLM-classify into {closure, cancellation, action, event, fyi, none}, send one Telegram alert per non-none item |
| `gmail-invite-alert` | `30 */3 * * *` | Parse ICS attachments from Gmail, surface METHOD:REQUEST invites only, dedup via `seen-invites.json` |
| `whatsapp-chat-alert` | `45 */2 * * *` | LLM-classify recent WhatsApp chat into action types, send Telegram alert per classified item. **Optional — see [§ The WhatsApp chapter](#the-whatsapp-chapter).** |
| `whatsapp-schedule-post` | `0 12 * * *` | Daily WhatsApp chat digest, synced with the fleet 5 AM PT delivery window. **Optional — see [§ The WhatsApp chapter](#the-whatsapp-chapter).** |

**I/O scripts** (deterministic Python, subprocess-safe, called by the orchestrators above):

- `gcal-fetch.py` — multi-calendar reader; flags conflicts; time-blocks by Morning/Afternoon/Evening; consults the shared brain calendar index to drop Murphy-owned meetings
- `calendar-index-build.py` — 10:25 UTC cron; builds the shared brain calendar index that encodes the Mouse/Murphy routing boundary (see [§ The routing boundary with Sergeant Murphy](#the-routing-boundary-with-sergeant-murphy))
- `gcal-write.py` — create/move/remove events; requires `--confirm` flag for safety (preview mode without it); writes audit log to `logs/calendar-writes.jsonl`
- `reminder-check.py` — also serves as an orchestrator; see cron table above
- `activity-email-check.py` — recursive MIME parser; no LLM; emits raw event dicts for the alert cron to classify
- `gmail-invite-check.py` — ICS attachment parser; filters METHOD:REQUEST only
- `chat-parse-schedule.py` — reads Baileys session store, extracts schedule-change keywords from family chat messages

**Workspace layout** under `~/.clawford/family-calendar-workspace/`:

```
SOUL.md                 # immutable (chattr +i)
IDENTITY.md             # immutable
TOOLS.md                # durable identity
AGENTS.md               # durable identity
USER.md                 # operator profile (gitignored, template in repo)
HEARTBEAT.md            # last-run status
MEMORY.md               # persistent notes
token.json              # Google OAuth refresh token (gitignored)
credentials.json        # Google OAuth client credentials (gitignored)
sent-reminders.json     # reminder dedup state
seen-invites.json       # invite dedup state
calendar-config.json    # calendar list + activity providers + per-calendar flags
whatsapp-session/       # Baileys session store (if WhatsApp enabled)
logs/                   # calendar-writes.jsonl audit log
cache/                  # morning-brief-ready.txt + staging files
scripts/                # all Python scripts listed above
```

## Deployment walkthrough

This adds onto [Ch 08 — Your first agent](08-your-first-agent.md). Everything in Ch 08 applies; the items below are the Mistress-Mouse-specific additions. Read [Ch 08](08-your-first-agent.md) first and treat the list below as the overlay.

**Pre-step: Google Cloud project setup.** Before you write a single line of code, create a Google Cloud project at `console.cloud.google.com/projectcreate`, enable the Google Calendar API and the Gmail API, then go to "Credentials → Create credentials → OAuth client ID → Desktop app" and download the resulting `credentials.json`. Go to "OAuth consent screen → Test users" and add the operator's email. This is the step that `Access blocked: ... has not completed the Google verification process` means you forgot.

**Pre-step: `calendar-config.json`.** Copy `agents/family-calendar/calendar-config.template.json` to `calendar-config.json` in the workspace and fill in the calendar IDs, friendly names, and `remind` flags for each calendar. The template ships with three calendars (primary, partner, kids) as placeholders. The `remind=false` flag on a calendar means the reminder check will silently drop events from that calendar. The `activityProviders` array maps sender-email patterns to provider names for the activity-email cron.

**Step 3a: Run the Google OAuth flow locally.** `python3 agents/family-calendar/scripts/google-auth-setup.py --scopes calendar gmail.readonly` on the local laptop. A browser pops, consent goes through, `token.json` lands in the current directory. Verify with `python3 agents/family-calendar/scripts/gcal-fetch.py --days 1` — it should print a JSON blob with today's events. If it prints an empty events list, check that the calendar IDs in `calendar-config.json` match what `gcal-fetch --list-calendars` returns.

**Step 3b: SCP `token.json` to the VPS.** `scp token.json operator@vps:~/.clawford/family-calendar-workspace/token.json`. Verify on the VPS with the same `gcal-fetch.py --days 1` test. If the VPS test fails but the local test passed, check that `credentials.json` was also SCPed (it has to live next to `token.json`).

**Step 3c (optional): WhatsApp setup.** If deploying the WhatsApp crons, run `python3 agents/family-calendar/scripts/whatsapp-pair.py` on the VPS. It prints a pairing code. Open WhatsApp on the operator's phone, go to "Linked devices → Link a device → Link with phone number instead," and enter the pairing code. The session writes to `whatsapp-session/` and survives restarts. **Do not skip the [§ The WhatsApp chapter](#the-whatsapp-chapter) rules before this step.**

**Step 5: Register the host crons.** Add the six Mistress Mouse entries to the `CONTRACT_ENTRIES` block in `ops/scripts/install-host-cron.sh`. The six entries land under the `family-calendar` section and each one calls `python3 ~/repo/agents/family-calendar/scripts/{cron-name}.py`. Run `ops/scripts/install-host-cron.sh` on the VPS — it drift-detects and rewrites the crontab idempotently.

**Step 6: Deploy.** `python3 agents/shared/deploy.py family-calendar` on the VPS, as documented in [Ch 07 — Intro to agents](07-intro-to-agents.md). Verify with `python3 agents/family-calendar/scripts/reminder-check.py --dry-run` — it should print a JSON blob with the reminders it would send, plus a `dry_run: true` field.

**Step 7: Wait for the next `*/5` tick** and check the Telegram channel. If you deployed at 10:23 UTC, at 10:30 UTC the morning-briefing cron will fire, write `cache/morning-brief-ready.txt`, and the cron will exit cleanly. The fleet-deliver at 12:00 UTC will pick up the file and actually send the Telegram message.

## Pitfalls

> 🧨 **Pitfall.** Deploying the WhatsApp crons without reading [§ The WhatsApp chapter](#the-whatsapp-chapter). **Why:** the Baileys ban risk is real, non-deterministic, and can lock a personal WhatsApp account permanently. The "dmPolicy=disabled" rule is non-optional and exists because an earlier version auto-replied to a partner's personal DM with a pairing-code message in the operator's voice, which was confusing to everyone involved. **How to avoid:** read [§ The WhatsApp chapter](#the-whatsapp-chapter) in full, then either follow all three rules exactly or disable the two WhatsApp crons via `disabled-agents.txt` and deploy the rest of the agent without them.

> 🧨 **Pitfall.** First-draft outbound WhatsApp delivery. **Why:** the first version of `whatsapp-schedule-post.py` (2026-04-08) sent the daily digest straight to the family WhatsApp group. That cron ran exactly once before getting pulled — family groups are not a place where an automated summary lands well, and Meta's ban detection also looks for automated-looking group posts from linked devices. **How to avoid:** outbound from the WhatsApp side of Mistress Mouse goes to the operator's Telegram only. The operator decides what, if anything, gets forwarded to the family group by hand. This is rule 1 of the three-rule contract.

> 🧨 **Pitfall.** Forgetting to add the operator to the Google Cloud OAuth consent-screen test-users list. **Why:** the first time `google-auth-setup.py` runs, the browser pops a consent screen, and if the operator's email is not in the test-users list, the screen shows `Access blocked: {app name} has not completed the Google verification process` with no indication that the fix is to add yourself as a test user. The error message makes this look like a permissions problem with the API scope. It is not. **How to avoid:** the Google Cloud Console "OAuth consent screen" page has a "Test users" section near the bottom — add the operator's email there before the first auth flow. You can add or remove test users at any time without re-running the flow.

> 🧨 **Pitfall.** Running the OAuth flow on the VPS. **Why:** `InstalledAppFlow.run_local_server(port=8080, open_browser=True)` needs an actual browser. On a headless VPS it will either hang forever waiting for a redirect that never happens, or (worse) spin up a local HTTP server on port 8080 that quietly conflicts with whatever else you might be running. The local-then-SCP pattern exists to avoid this entire class of pain. **How to avoid:** always run `google-auth-setup.py` on the operator's laptop, SCP the resulting `token.json` (and `credentials.json` if it isn't already there) to `~/.clawford/family-calendar-workspace/`, and verify with a one-shot `gcal-fetch.py --days 1` on the VPS.

> 🧨 **Pitfall.** Missing per-calendar `remind` flag. **Why:** without the flag (the 2026-04-12 fix), every event on every calendar gets 60/30/15-minute reminders fired. That means the operator's partner's Pilates class fires a `🔴 NOW: Pilates` Telegram alert every week at the same time, which is the textbook example of an automation that could be interpreted as a snarky nudge from the operator — even though the agent fired it and the operator had no idea it was happening. **How to avoid:** every calendar in `calendar-config.json` needs an explicit `remind` flag. Default to `remind=false` for any calendar that is not the operator's own primary calendar. Re-read the config before the first cron fires and verify against the output of `reminder-check.py --dry-run`.

> 🧨 **Pitfall.** Upgrading Google OAuth scopes without re-running the auth flow. **Why:** the refresh token is scoped to exactly the scopes the consent screen showed the first time. If you add `calendar` (write) to a token that was originally generated for `calendar.readonly` only, every write call will return a 403 with a cryptic message about insufficient permissions. The refresh token will not auto-upgrade. **How to avoid:** any time you change the scope list in `google-auth-setup.py`, re-run the script on the local laptop. It will detect the scope mismatch, pop a fresh consent screen, and write a new `token.json`. SCP the new file to the VPS and redeploy. This is a once-per-scope-change operation, not a recurring one.

> 🧨 **Pitfall.** The routing boundary check against Workflowy silently failing. **Why:** the Workflowy presence check in `gcal-fetch.py` is a best-effort lookup against Workflowy's search endpoint. If the Workflowy session is expired, the search call returns an empty result set — which Mistress Mouse interprets as "no Workflowy node, so I own this event," which means she might start sending reminders for work meetings that Sergeant Murphy should own. The symptom is duplicated reminders across both agents. **How to avoid:** the Workflowy session is maintained by Sergeant Murphy's own auth pipeline; see [Ch 13](13-sergeant-murphy.md) for the pattern. If deploying Mistress Mouse standalone (without Sergeant Murphy), the Workflowy check always returns empty and Mistress Mouse owns everything — which is the intended standalone behavior. The bug only manifests when both agents are deployed and Murphy's Workflowy session has expired; fix Murphy first, not Mistress Mouse.

> 🧨 **Pitfall.** Schedule-change detection in WhatsApp dropping ambiguous messages. **Why:** `chat-parse-schedule.py` uses a keyword-based filter for "schedule change language" — times, cancellations, date references. It is deliberately conservative: it would rather miss a real schedule change than flood the operator with false positives. That means a message like "hey can you push it back an hour" is caught, but "does tomorrow still work for the thing" is not. The agent is not a complete substitute for reading the WhatsApp group. **How to avoid:** the agent is a safety net, not a primary channel. Read the WhatsApp group at whatever cadence you currently do; the agent's value is catching the things that hit in the middle of a busy day, not replacing the channel entirely.

## See also

- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the deploy path and safeguard story
- [Ch 08 — Your first agent](08-your-first-agent.md) — the general deploy walkthrough
- [Ch 13 — Sergeant Murphy 🐷🔍](13-sergeant-murphy.md) — the routing boundary partner
- [Ch 14 — Huckle Cat 🐱🤝](14-huckle-cat.md) — reuses the Google OAuth pattern
- [Ch 17 — Auth architectures](17-auth-architectures.md) — the cross-agent auth reference (pending)
