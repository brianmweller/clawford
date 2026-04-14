![Clawford](../assets/Clawford2.png)

# What Clawford Isn't

*Last updated: 2026-04-14 · Reading time: ~20 min · Difficulty: moderate*

**TL;DR**

- Clawford is not an OpenClaw fleet. It used to be. This chapter is the story of why it isn't anymore, written up front as a decision doc rather than buried at the back as a retrospective.
- A personal fleet needs a **library**, not a **platform**. OpenClaw is a platform. Once I stopped trying to use the platform's abstractions and started building the library I actually needed, the platform became a weekly maintenance tax paying for one asset I could replace in a weekend.
- The one load-bearing thing OpenClaw gave me was `openclaw infer` — a CLI that routes LLM calls through Codex OAuth so they ride a ChatGPT Plus subscription at zero marginal cost. OpenAI ships `codex` directly and it does the same thing. Everything else I had already rebuilt.
- I am not advocating that anyone else leave OpenClaw. I am describing what happened as my fleet outgrew it. If your fleet fits neatly inside OpenClaw's skill library, you are the median user this platform was built for — skip this chapter, keep using it, and godspeed.

## The morning the whole fleet went dark

It was a Tuesday morning in early April. I rolled over, reached for my phone, and noticed Telegram was quiet in a way it shouldn't have been. No morning digest from Lowly Worm. No 10:30 family brief from Mistress Mouse. No meeting prep from Sergeant Murphy. Just silence.

I pulled up the fleet-health dashboard and every agent was red. Same error on every single one: `exec blocked by approval policy`.

I hadn't touched the approvals policy. I hadn't deployed anything overnight. Nothing had changed on my side. What had changed was that OpenClaw 2026.4.11 had shipped in the background, and 2026.4.11 had added a new hardcoded `exec preflight` that rejected any command containing shell operators — `;`, `&&`, output redirects, `sh -lc`, exit-code capture, all of it.

The problem wasn't the preflight in principle. The problem was that every cron session in my fleet had an LLM that would reflexively wrap commands like this:

```
python3 /path/to/script.py; printf "EXIT:%s" \$?
```

It wrapped them because LLMs like to be thorough, and "also print the exit code" is the kind of thing a responsible-looking command wants to do. It had worked fine in 2026.4.10. In 2026.4.11 the preflight looked at the `;` and the `printf` and the `$?` and rejected the whole thing before it ran. Every cron. Every agent. Same morning.

The fix took me most of a day, and it had two sides. Scripts had to start reporting their status via a JSON line on stdout, so the LLM had no *reason* to wrap the command in exit-code capture. And cron messages had to open with an explicit `"run this bare — do not append, do not capture, do not redirect"` preamble, so the LLM had no *temptation*. The script contract that the old Ch 06 pitfalls section describes in great detail — that whole thing is a direct scar from this morning. It exists because OpenClaw 2026.4.11 cascaded the entire fleet in one upgrade, with no release-note warning and no migration guide.

That was the morning I started asking the question that became this chapter: *is any of this actually infrastructure, or is it just something that happens to work until it doesn't?*

The answer came back depressingly consistent. Most of it was the second thing.

## What I thought OpenClaw was giving me

When I first built Clawford on top of OpenClaw, I thought the platform was providing six things:

1. **Cron scheduling** — a cron runtime with an LLM session baked in, so you could schedule reasoning work on a clock
2. **Exec-approvals policy** — an allowlist-based security layer governing what the LLM could actually run
3. **Comms bindings** — Telegram and WhatsApp channel integrations with the message-routing plumbing built in
4. **LLM inference** — authenticated LLM calls via `openclaw infer`, riding a ChatGPT Plus subscription
5. **Skill library** — 100+ built-in skills and a community marketplace of more
6. **Filesystem layout** — a convention for where agent state lives (`~/.openclaw/<agent>-workspace/`)

That's the pitch, and it's a real pitch. For the median OpenClaw user — someone who wants a chatbot-that-can-do-stuff running 24/7 on a Mac Mini, pointed at Gmail and calendar — the pitch delivers. I read the reviews. I watched the videos. Plenty of people extract real value from exactly those six things.

The problem is that Clawford is not the median OpenClaw deployment.

## What OpenClaw actually gave me

Over two weeks of building, I routed around five of the six pitched things:

- **Cron scheduling** → routed around. The LLM cron runtime has a hard 600-second budget. My news digest kept getting killed mid-compose. The fix was to run a **host cron** (plain system crontab) that called `openclaw infer` from a Python composer, bypassing the cron runtime entirely. I was writing host crons; OpenClaw's cron just happened to register them for me.
- **Exec-approvals policy** → routed around. I started with strict allowlists. I ended with `policy=full` (maximally permissive) plus deterministic Python guards inside the agent scripts themselves. The long version is in the old Ch 06; the short version is *you cannot pattern-match your way out of a Turing-complete shell language*, and every week I spent fighting the allowlist was a week I wasn't improving the fleet.
- **Comms bindings** → half routed around, half revealed as a liability. Telegram is a bot token and an HTTP POST; I was already calling the API directly from Python in several places. WhatsApp is implemented on top of Baileys, which reverse-engineers the WhatsApp Web protocol, and Meta's detection is good enough that automating it can get your number banned. The binding that looked like infrastructure was never going to survive contact with reality.
- **Skill library** → never meaningfully used. I tried adopting a few community skills in the first week and hit two problems immediately: the skill system is buggy (skills show "enabled" but are silently disabled until you hand-edit config), and a non-trivial fraction of the community marketplace contains skills with actively malicious behavior — community estimates put the number around fifteen percent. I wrote my own scripts from scratch and stopped looking at the marketplace.
- **Filesystem layout** → vestigial. `~/.openclaw/<agent>-workspace/` is just a directory. My agents read and write files in it with normal Python I/O. Rename it to `~/.clawford/` and nothing meaningful changes.

That leaves one thing: **`openclaw infer`**. And `openclaw infer` is a wrapper around Codex OAuth that rides my ChatGPT Plus subscription.

Which is exactly what OpenAI's first-party `codex` CLI does directly.

The rest of OpenClaw, at that point, was a weekly maintenance tax paying for one asset that had a first-party replacement.

## The six walls I hit

Everyone's migration story has a specific trigger, but mine had six. I'll list them in roughly the order they hit me, because each one individually wasn't enough to leave — it was the sixth that tipped me over.

### Wall 1: the 600-second cron timeout

Lowly Worm's morning news digest was the first cron that needed to reason over a lot of content. Fetch ~200 RSS items. Rank them against a preference model. Pick the top 15-20. Write an extended headline for each. Group them by topic. Deliver. The LLM kept composing 80% of a digest and then vanishing — the cron process was being hard-killed at the 10-minute budget, and there was no way to split the work across multiple cron ticks because each tick started a fresh session with no memory of the last one.

The fix was to bypass the LLM cron entirely: run a host cron that calls `openclaw infer` from a Python composer, where the timeout is whatever I set it to and the work can be chunked across as many calls as I want. That fix worked. But it also told me that the cron runtime — which I had thought was central to the whole platform — was the first thing I routed around, in the *first* agent I built. The old Ch 06 has the full write-up; the pitfall is documented there because the workaround (host cron + `openclaw infer` composer) is the same workaround you'd reach for as a new user today.

### Wall 2: exec-approvals allowlists are unwinnable

I started with strict approvals. The agent would attempt a command, OpenClaw would check it against an allowlist, and it would prompt me if the command wasn't on the list. Sounds fine. In practice: the LLM constructs shell pipelines that no regex can preemptively bless. `sh -c "cmd1 && cmd2"` evades pattern-matching because the matcher sees `sh`, not the inner commands. Disable `sh`, the LLM finds `bash`. Disable both, it finds `python3 -c "os.system(...)"`. You cannot pattern-match your way out of a Turing-complete shell language, and the harder you try, the more you train the LLM to route around you.

I gave up on pattern-based defense and moved the defense to three other layers: OS-level immutability on identity files (`chattr +i` on `SOUL.md` / `IDENTITY.md`), deterministic Python guards inside the scripts themselves, and a strict script contract that forbids shell operators in cron command strings. The approvals allowlist became `policy=full`. My actual security story lives in the security chapter, not in the allowlist.

The week I spent fighting allowlists was the first time I felt OpenClaw was actively in my way. It wasn't the last.

### Wall 3: OpenClaw clobbers what it doesn't own

There are two separate instances of this and they're both ugly.

The first is `BOOTSTRAP.md`. Every time OpenClaw runs its `agents add` flow (or any partial run of it), it writes a `BOOTSTRAP.md` file into the agent's workspace. That file is a "who are you? what should I call you?" first-run onboarding script. It's designed to fire once, let the agent introduce itself, and then get deleted. The problem: if `BOOTSTRAP.md` doesn't get deleted, the LLM reads it on every session start *instead of* the `SOUL.md` and `IDENTITY.md` files sitting right next to it. The agent's identity is correct on disk; the running session ignores it and behaves like a fresh install. The first time this hit me I spent an embarrassing number of hours staring at perfectly-correct workspace files and wondering why the agent kept introducing itself to me like a stranger. The fix in `deploy.py` is a one-liner: if `IDENTITY.md` exists, delete `BOOTSTRAP.md` before the agent sees a first message. Forever. It sits in Safeguard 10 now, and it has to, because OpenClaw keeps writing it.

The second is the Telegram bot surface. Every agent's slash-command menu (`/help`, `/status`, whatever you set) is re-applied by OpenClaw's channel-sync on every gateway restart — and "every gateway restart" includes every container bounce, every config-hash change, every `docker compose restart`. The menu OpenClaw re-applies is its own generic default: `/help`, `/status`, `/context`, `/exec`, and forty-five others. So if you set per-agent custom slash commands via `setMyCommands`, OpenClaw silently clobbers them within seconds of the next restart and your bots revert to a generic OpenClaw surface. There are two mitigations and I run both: a `commands.native: false` flag per agent to opt out, and an `entrypoint.sh` post-startup hook that fires `set-bot-commands.sh` at +25 seconds after container start as a belt-and-suspenders backup. If OpenClaw's config-hash logic ever changes under me, the hook reapplies.

The common thread: **OpenClaw does not respect state it didn't write.** Things I put in the workspace get overwritten. Things I push to the Bot API get overwritten. The platform assumes it is the only source of truth, and it is wrong, and I spend time every week keeping it from rewriting things I need stable.

### Wall 4: silent upgrades that tighten the ground under you

The exec-approvals baseline in Clawford (`ops/exec-approvals-baseline.json`) exists because I learned the hard way that OpenClaw upgrades can *silently tighten* the live approvals policy on the VPS without telling you. The baseline is committed to git; `deploy.py` Safeguard 8 checks the live file against the baseline on every deploy, and refuses to proceed if they've drifted. The drift is almost always an OpenClaw upgrade doing something I didn't ask for — a flag defaulting the other way, a policy tightening under a renamed key, a new `ask` value appearing in the agent block. The baseline catches it before the next morning's crons start failing.

This is closely related to Wall 3 (OpenClaw clobbers what it doesn't own), but it is worth separating because the failure mode is different. Wall 3 is about state OpenClaw keeps overwriting; Wall 4 is about platform behavior quietly changing underneath you, with no migration guide and no warning in the release notes. Safeguard 8 is the tripwire. The drift it catches is almost never benign.

### Wall 5: the skills marketplace is a trap

This one is the shortest because I bounced off it fastest. The skill marketplace is the thing most OpenClaw videos lead with, and on paper, 100+ pre-built skills for common tasks is a lot of leverage. In practice, I tried a handful in the first week. They didn't work — the skills tab showed them as enabled, but they were silently disabled until I hand-edited the JSON config. The docs for writing my own were thin enough that community consensus was *"no AI knows how to write skills yet"* — even an LLM attempting to generate one would produce something broken. And then there is the ugliest part: by community estimates, roughly fifteen percent of the marketplace contains skills with actively malicious behavior — data exfiltration, credential logging, unexpected network calls. On a box that has access to Gmail, calendar, and files, that is a real supply-chain risk, not a theoretical one.

I stopped looking at the marketplace. I wrote my own scripts from scratch. The ecosystem stopped feeling like an asset.

### Wall 6: the Tuesday preflight cascade

The sixth wall was the one I opened this chapter with: the 2026.4.11 exec preflight, the whole fleet red with `exec blocked by approval policy` on a Tuesday morning, the day-long fix that carved the script contract into the codebase as a permanent scar. I won't re-tell it here.

It wasn't the worst of the six in absolute terms — Wall 2 (exec-approvals allowlists) probably cost me more total hours, and Wall 3 (`BOOTSTRAP.md` split-brain) was more psychologically demoralizing the first time it bit. But the Tuesday cascade was the one where the pattern finally clicked. Every wall on this list has the same shape: *OpenClaw shipped a breaking change, or had a breaking default, and I spent time working around it instead of improving the fleet.* That Tuesday was the moment I stopped making excuses for the platform.

None of these were huge individually. Collectively, they were a tax on my time that didn't connect to any work I cared about. And the tax was paying for — it always came back to this — one CLI that routed LLM calls through a ChatGPT subscription.

So I left.

## What a personal fleet actually needs

Here's the shape that emerged once I started building against the problem instead of the platform. A personal fleet — specifically one with *multiple* agents sharing infrastructure, which is the whole premise of this guide — needs four things, and none of them are cron scheduling, exec-approvals policy, or a skills marketplace.

### 1. A way to call an LLM at a flat subscription cost

Not pay-per-token. Pay-per-token is fine for a product; it is hostile for a personal fleet where you are running dozens of cron ticks a day and don't want to think about budget. The unlock is an LLM CLI that rides your ChatGPT (or Claude) subscription, because those subscriptions are flat-rate and generous.

OpenAI ships `codex` for this. Anthropic explicitly forbids it for Claude — [Claude Code's legal-and-compliance doc](https://code.claude.com/docs/en/legal-and-compliance) says *"Anthropic does not permit third-party developers to offer Claude.ai login or to route requests through Free, Pro, or Max plan credentials on behalf of their users"*. So your choice of LLM provider for a personal fleet is effectively determined by which one will let you do this. For me, that is OpenAI, and `codex` is the one dependency I genuinely couldn't walk away from. If Anthropic ever opens the policy, I'll take a look.

This is exactly one module in the Clawford library: [`agents/shared/llm.py`](../agents/shared/llm.py). It exposes a single `infer(prompt, ...)` function and dispatches to `codex` as the backend. Every LLM call in the fleet goes through it. Zero call sites know which backend they are hitting, so if the LLM landscape shifts, it is a one-file change.

### 2. Three tiers of access to the outside world

This is the one I wish someone had told me at the start. Personal fleets reach outside the box in three increasingly painful tiers, and each tier wants its own shared library.

- **Tier 1 — clean APIs.** Gmail, Google Calendar, Telegram Bot API, RSS, meeting transcripts, Workflowy. Well-behaved endpoints with documentation and SDKs. The shared infrastructure here is boring: an OAuth helper for Google, a Telegram delivery helper, a retry-with-backoff wrapper. If five of your agents each reimplement `sendMessage` to `api.telegram.org`, you have accidentally written five Telegram clients. Fix this early. In Clawford: [`agents/shared/telegram.py`](../agents/shared/telegram.py), [`agents/shared/google_oauth.py`](../agents/shared/google_oauth.py), [`agents/shared/heartbeat_base.py`](../agents/shared/heartbeat_base.py).
- **Tier 2 — stock headless Chromium.** Sites that work fine with a default Playwright browser, but need a *persistent profile* and *out-of-band* authentication. LinkedIn is the canonical example in this fleet — you authenticate once in a real browser, store the profile, and subsequent automated runs reuse the stored session. The shared infrastructure here is a profile-bootstrap helper, an Xvfb virtual-display wrapper, and a set of selectors that survive DOM rot (aria-labels, not hashed class names). In Clawford: [`agents/shared/playwright_profile.py`](../agents/shared/playwright_profile.py).
- **Tier 3 — fingerprint-aware browsers through residential proxies.** Sites with serious anti-bot systems. In this fleet, that is Costco and Amazon. A stock Chromium fails immediately; you need Camoufox (a fingerprint-aware Firefox fork), a residential proxy with sticky-session support, auto-MFA through TOTP, and careful cookie lifecycle management. The shared infrastructure here is the hardest and most agent-specific, but consolidating the proxy-config parsing and retry-policy classifier pays off every time you touch it. In Clawford: [`agents/shared/camoufox_proxy.py`](../agents/shared/camoufox_proxy.py), [`agents/shared/retry_policy.py`](../agents/shared/retry_policy.py).

OpenClaw offered nothing for any of these tiers. The browser automation work is entirely mine. The library I should have been building from day one is the library that makes these three tiers reusable across agents, not the library that glues skills together. See Ch 07-7 for the cross-cutting auth patterns.

### 3. A durable shared brain

This is the one I almost left off the list because it is so obvious once you have it that it stops feeling like infrastructure. The brain is where cross-agent state lives — facts, people, commitments, queues, per-agent status files, fleet-health snapshots. It is not ephemeral; it persists across sessions, across deploys, across crashes. It is what lets one agent remember who somebody is so another agent can remind you to follow up with them.

In Clawford, the brain is a directory tree split two ways:

- **`ops/brain/*`** is **git-tracked.** This is where schemas, canonical configs, and rules files live. Code review, history, rollback, the usual.
- **`~/Dropbox/openclaw-backup/people|status|facts|queues/*`** is **Dropbox-synced, not git.** This is where ephemeral state lives — status files that churn every few minutes, append-only queues, per-agent scratch.

The split matters. Agents write to Dropbox-side state freely without flooding git history. Deploy and host-cron probes write to git-side state deliberately. Multiple agents can write to the same brain safely because the append-only patterns were designed for it.

If you had asked me on day one whether the brain was part of OpenClaw or part of Clawford, I would have said OpenClaw. It is not. It is just files on disk, synced through Dropbox and git, with schemas I defined and helpers in [`agents/shared/brain.py`](../agents/shared/brain.py). It survives the migration untouched. It is probably the single most underrated piece of infrastructure in the whole fleet, and it exists entirely outside the OpenClaw dependency graph.

### 4. Deployment and health

Deploys need to be safe and idempotent. Health needs to be observable from somewhere that is not inside the thing you are observing. In Clawford, that is [`agents/shared/deploy.py`](../agents/shared/deploy.py) (ten safeguards, including a pre-deploy backup tarball so a bad deploy is one `tar -xzf` from recovery) and a host-cron-driven fleet-health probe that writes `fleet-health.json` to the brain.

Both are mine. Neither depended meaningfully on OpenClaw once I dug in — the original versions wrapped some `oc` CLI calls, but those turned out to be ceremony around cron registration, approvals baseline drift, and channel binding, all of which are OpenClaw-internal concepts that disappear in a Clawford-native world.

## The new shape

Here's what Clawford looks like after the migration. Compare to the diagram in [Ch 01](index.md) — the overall shape is the same, because the fleet is the same. What changed is what is in the middle.

```
          ┌─────────────────────────────────────────┐
          │         Telegram (you ↔ fleet)          │
          └────────────────────┬────────────────────┘
                               │
                 ┌─────────────┴────────────┐
                 │   agents/shared/llm.py   │   ← codex CLI + OAuth
                 │   (shared LLM broker)    │
                 └─────────────┬────────────┘
                               │
    ┌────────┬───────┬─────────┼─────────┬────────┬────────┐
    │Fix-It  │Lowly  │  Hilda  │  Mouse  │ Murphy │ Huckle │
    │ 🦊🔧   │ 🐛📰  │   🦛🛒  │   🐭📅  │  🐷🔍  │  🐱🤝  │
    └────────┴───┬───┴─────────┴─────────┴────────┴────────┘
                 │
    ┌────────────┴─────────────────────────────┐
    │                                          │
    │   agents/shared/ — world-access library  │
    │                                          │
    │    · telegram.py          (Tier 1)       │
    │    · google_oauth.py      (Tier 1)       │
    │    · heartbeat_base.py    (Tier 1)       │
    │    · playwright_profile.py (Tier 2)      │
    │    · camoufox_proxy.py    (Tier 3)       │
    │    · retry_policy.py      (Tier 3)       │
    │    · brain.py             (shared brain) │
    │    · llm.py               (LLM broker)   │
    │    · deploy.py            (deployment)   │
    │                                          │
    └────────────┬─────────────────────────────┘
                 │
          ┌──────┴───────────────┐
          │  Shared brain        │
          │                      │
          │  · ops/brain/*       │   git-tracked
          │  · ~/Dropbox/...     │   Dropbox-synced,
          │                      │   append-only
          └──────────────────────┘
```

Two things are worth noticing.

First, there is no "gateway" in the middle anymore. The thing that used to be "OpenClaw gateway" is `agents/shared/llm.py`, and it is a thin Python wrapper around `codex infer`. No Docker container, no process to babysit, no binary to keep updated. The LLM broker is ~50 lines.

Second, the library grew. Where the old diagram had a black box labeled "OpenClaw," the new diagram has a visible library of nine modules, and every module represents duplicated code that used to live scattered across per-agent scripts. Building the library was always going to happen, on any migration path or no migration path. What leaving OpenClaw forced was admitting that the library was the real work, and the platform was overhead.

## What I thought I'd miss (but don't)

Before I left, the skills marketplace was the one thing I thought I'd regret losing. OpenClaw's pitch videos spend a lot of time on the skill library, and on paper, 100+ pre-built skills for common tasks is a lot of leverage.

In practice, I don't miss it and I am not sure I ever used it meaningfully. The skills worth having — the ones that aren't in the fifteen percent that are actively malicious, and aren't in the further fraction that don't actually work because the skill system is buggy — turn out to be available directly from their upstream projects without the OpenClaw cruft and without the supply-chain risk. If I need a Playwright scraper for a specific site, I write it. If I need a Gmail helper, the `googleapiclient` Python SDK already does what I want. If I need to call an LLM, `codex infer` is one subprocess call. The skill marketplace was always a layer on top of things that exist on their own, and the layer was imposing more cost than value.

Running without it hasn't hurt. That is the honest answer.

## What the migration looked like

*This section gets polished at the end of the migration (Phase 7) with real receipts — calendar time per phase, specific incidents avoided during the migration window, measurable wins. For the working version, see the migration plan file itself.*

Rough shape of the plan:

- **Phase 0** — Draft this chapter as a decision doc and scaffold `guide-v3/` so the rest of the migration has somewhere to land.
- **Phase 1** — Bootstrap `codex` auth on the VPS and introduce a shared `llm.py` backend shim that both `openclaw infer` and `codex` dispatch through. No call sites touched yet.
- **Phase 2** — Build the complete shared library (Tier 1, Tier 2, Tier 3, plus `brain.py`) before migrating any agent. Shapes derived from existing duplicated code.
- **Phase 3** — Pilot-migrate news-digest (Lowly Worm). Verify by firing every cron in a day-in-the-life simulation, not by running long parallel soaks.
- **Phase 4** — Fleet-port the remaining five agents in complexity-ascending order.
- **Phase 5** — Rewrite `deploy.py`'s three OpenClaw-coupled Safeguards.
- **Phase 6** — Stop OpenClaw processes; promote `codex` to the default backend.
- **Phase 7** — Delete the `oc()` wrapper, rename `~/.openclaw/` → `~/.clawford/`, polish this chapter with receipts, finalize v3.

Red/green TDD throughout. Tests first, confirm red, implement, confirm green — no bottom-up implementations.

## See also

- [Ch 01 — What Is Clawford?](index.md) *(pending migration from v2)* — the brief welcome, with the post-migration architecture diagram
- [Ch 06 — Infra setup](06-infra-setup.md) *(pending rewrite)* — the shared library in detail, what each module does
- [Ch 07 — Intro to agents](07-intro-to-agents.md) *(pending rewrite)* — the anatomy of an agent in the Clawford-native runtime
- [Ch 07-7 — Auth architectures](07-7-auth-architectures.md) *(pending)* — the cross-cutting auth patterns (Google OAuth, persistent Chromium profile, Camoufox + residential proxy)
- [`../guide-v2/06-intro-to-agents.md`](../guide-v2/06-intro-to-agents.md) — the OpenClaw-era version of the intro-to-agents chapter, frozen as historical. The 600-second LLM cron budget, the permissive exec-approvals rationale, and the `BOOTSTRAP.md` split-brain story are the specific incidents that became walls 1, 2, and part of 3 in this chapter. The script contract section is the direct scar from Wall 6.
- [`../guide-v2/05-infra-setup.md`](../guide-v2/05-infra-setup.md) — the OpenClaw bot-commands clobber story that is the rest of Wall 3, plus Safeguard 8 drift-detection context for Wall 4.
- [`../docs/v2-to-v3-migration.md`](../docs/v2-to-v3-migration.md) — the per-chapter migration checklist
