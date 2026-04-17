![Clawford](../assets/Clawford2.png)

# Before you start

*Last updated: 2026-04-15 · Reading time: ~15 min · Difficulty: easy*

**TL;DR**

- The decisions in this chapter determine whether your first agent is live in 2 days or 2 weeks. Most of them are one-way doors, and they're all cheap to get right if you make them here.
- Budget **~$50/month** to run Clawford (VPS + ChatGPT Plus subscription), plus **~$1/month** in residential proxy fees later if you deploy the shopping agent, Hilda Hippo. Budget **2-3 days** to your first working agent and **2-4 weeks of evenings** to a full fleet. Don't lowball either number.
- Run Clawford on a dedicated VPS, not your daily driver. Use Telegram, not WhatsApp. Deploy Mr Fixit first. Those three are the cheapest "listen to me" decisions in the whole guide.
- The per-agent config files ship as `*.example` templates in git; on first setup you scaffold the unsuffixed siblings via `python3 agents/shared/deploy.py <agent> --bootstrap-configs`, edit your real values into them, and the siblings stay `.gitignore`d. That pattern is the seam that makes sharing this repo possible.
- Plan for rebuilds, not stability. Every service that touches the real world breaks eventually. The question isn't *whether*, it's *how fast you notice and recover*.

## What you need before Ch 04

You don't need everything below paid for and activated by the end of this chapter — just know what's coming, and have the free items in hand before you try to provision anything.

**Accounts (required):**

- A **[Hetzner Cloud](https://console.hetzner.cloud/)** account. Ch 04 walks the cpx31 provisioning with Terraform. Other providers (DigitalOcean, Linode, Vultr, OVH, AWS Lightsail) will also work, but swapping providers is a Ch 04 problem.
- A **Dropbox** account with ~10 GB free. The shared brain and agent backups sync through Dropbox — a dedicated account for this fleet is cleaner than co-mingling with your personal files.
- A **Telegram** account. You'll create one bot per agent via [@BotFather](https://t.me/BotFather).
- A **[GitHub](https://github.com)** account (or equivalent git host) for your canonical remote. The checkout on your local box is the source of truth; the remote is backup plus a place to review diffs.

**Dev environment (required):**

- **Git** locally.
- **[Claude Code](https://claude.com/claude-code)** or the **Codex CLI** — or an LLM pair-programming environment you already trust. Much of this guide assumes you'll be driving the tooling *with* an LLM copilot, not against a blank terminal. See [Ch 01](index.md)'s "Who this guide is for" for the framing. [Ch 05 — Dev setup](05-dev-setup.md) covers what Claude Code gets right and, more importantly, what it gets wrong.

**What you should already be comfortable with:**

Shell and git, run and read. Python familiarity is welcome but not required — Claude Code eventually figures out a broken scraper if you give it enough room. Zero prior experience with the platform Clawford grew out of is assumed; this whole guide is the on-ramp.

**LLM provider (required):**

A **ChatGPT Plus / Codex subscription** (~$20/month). Clawford's LLM entry point is `agents.shared.llm.infer()`, which wraps the `codex` CLI and rides your ChatGPT Plus subscription for every agent LLM call. See "Picking your LLM provider" below for the reasoning and the (limited) alternatives.

**Optional — deferred until Ch 15:**

- A **residential proxy subscription** (IPRoyal, Bright Data, or similar). Only matters once you deploy Hilda Hippo for purchasing — Amazon and Costco detect datacenter IPs and will block you — so you'll need a sticky residential egress to reach them. Don't buy this until you're actually ready to deploy her. Budget **~$1/month** when you do — the actual bandwidth the fleet puts through the proxy is small, because Hilda's hot path is small HTTPS API calls, not heavy page scrapes. Uninformed usage (scraping full pages through the proxy on every query) would cost more; most providers' minimum-volume plans land in the $8-30/month range if you don't pay attention to what you're routing through.

## What it'll cost you

Rough monthly numbers from my own setup. Your mileage will vary with region and currency.

| Line item | Cost | Kicks in | Notes |
|---|---|---|---|
| Hetzner VPS (cpx31) | ~$30/month | Ch 04 | 4 vCPU / 8 GB RAM / 160 GB SSD. Availability varies by region — I picked cpx31 partly because the slightly larger cpx32 wasn't available in mine. Smaller SKUs are fine for small fleets. |
| ChatGPT Plus subscription | ~$20/month | Ch 04 | Flat rate. What `codex` rides for every LLM call in the fleet. |
| Dropbox | free or ~$12/month | Ch 06 | Free tier fits the shared brain; paid only if you archive a lot of brain snapshots. |
| Residential proxy | ~$1/month | Ch 15 | Only if you deploy the shopping agent, Hilda Hippo. Cost is this low because the hot path is small HTTPS calls, not page scrapes. Uninformed usage lands in the $8-30/month range. |
| **Baseline (no proxy)** | **~$50/month** | | |
| **Full fleet with proxy** | **~$51/month** | | |

None of this includes your time, which is the expensive part.

## How long it'll actually take

I've been doing this for a while and my calibration is still usually off. Assume:

- **First working agent (Mr Fixit) end-to-end:** 1 full day if absolutely nothing goes wrong. **2-3 days realistic.** The first deploy of any new agent is a minefield — see [Ch 09](09-mr-fixit.md) for the list of silent failures I hit on Mr Fixit and how to avoid each one.
- **Full six-agent fleet:** **2-4 weeks of evenings**, depending on which services you're integrating and how much scar tissue you inherit from this guide.
- **Ongoing operations after the fleet is stable:** a few hours per week of "something broke and I need to look at it," front-loaded after each new deploy and whenever an upstream service (Google, Amazon, Costco, LinkedIn) changes something under you.

Don't bundle this onto a hard work deadline. You will spend more time in a browser inspector chasing a cookie than you expect to.

## Three decisions to make right now

These three are cheap to get right up-front and expensive to reverse later.

### Run it on a dedicated VPS — and don't run as root

A Clawford fleet runs with broad filesystem and shell access on its host. That's how the agents do their work — Playwright browsers, Camoufox sessions, Gmail tokens, Dropbox state, outgoing SSH. Pointing that at your work laptop means accidental file modifications, runaway processes during debugging, and a security posture you did not consent to.

Use a dedicated VPS. Ch 04 walks Hetzner cpx31 (~$30/month, 4 vCPU, 8 GB RAM, 160 GB SSD), which is comfortable for all six agents with headroom. **Availability varies by region** — I picked cpx31 partly because the slightly larger cpx32 wasn't available in mine. If your region offers a different set of SKUs, pick one step above what you think you need. A Raspberry Pi or old Mac mini *can* work if you already have one and don't mind the tradeoffs in [Ch 01](index.md)'s VPS-vs-Mac-mini section — but the whole guide is written against a VPS.

On whichever host you pick, do not run the fleet as `root`. You'll hit package-manager permission errors, lose the ability to run systemd user services cleanly, and expand the blast radius of anything that goes wrong. The Terraform in Ch 04 creates a non-root `openclaw` user for you (the name is a historical artifact from the pre-liberation era — the platform it refers to is gone but the Unix user lives on as the fleet's operator account). If you're setting up manually instead, create one before you install anything:

```bash
adduser openclaw
usermod -aG sudo openclaw
```

Everything in the rest of the guide assumes you SSH in as that user.

### Telegram, not WhatsApp

Clawford technically supports both. Use Telegram anyway.

WhatsApp support rides on Baileys, an unofficial library that reverse-engineers WhatsApp Web. It brings three problems you will eventually hit:

- **Account bans.** Operators who lean on it report being banned within days.
- **24-hour messaging window.** WhatsApp blocks proactive bot messages after 24 hours of silence, which is exactly when you'd want an overnight cron to page you.
- **Silent session expiration.** The web session dies without an error, and your agents go quietly dark.

Telegram uses the official Bot API. No ban risk, unlimited proactive messages, and each bot gets its own name and avatar so you can tell who's talking to you at a glance.

> ⚠️ **Warning.** Install Telegram on your phone before Ch 06. Bot creation via @BotFather runs through a real Telegram client, not a CLI.

### Deploy Mr Fixit first

Mr Fixit is the infrastructure fox: he monitors every other agent's health, validates the shared brain, runs fleet-health probes, and escalates to you when something is broken. Deploy him first for three reasons:

1. **He teaches you the deployment workflow with the lowest stakes in the fleet.** If Mr Fixit breaks, nothing downstream has been built yet.
2. **Once he's running, he monitors everything you deploy afterward.** Your second agent inherits a canary for free.
3. **The first deploy of *any* new agent is a minefield of silent failures.** Better to eat those failures on the infra fox than on the agent that's wired to your calendar, your inbox, or your credit card.

## The `*.example` template pattern

Before you clone anything, know that the repo has a strict shape that keeps PII out of git:

- Every per-agent config file ships as `<file>.example` — e.g., `agents/fix-it/IDENTITY.md.example`, `agents/shopping/manifest.json.example`, `agents/connector/scripts/mine/family_map.py.example`.
- The unsuffixed siblings (`IDENTITY.md`, `manifest.json`, `family_map.py`) are `.gitignore`d. They exist only on your working machine.
- **First-time setup** for each agent you plan to deploy is: `python3 agents/shared/deploy.py <agent-id> --bootstrap-configs` to scaffold every unsuffixed sibling from its `.example` template, then edit each one with your real values (names, IDs, preferences) and delete the `CLAWFORD_BOOTSTRAP_UNEDITED` sentinel comment from the top of every `.md` file. The bootstrap tool refuses to overwrite anything that already exists, so re-running it after you add a new agent is safe. Never commit the unsuffixed files.

The templates ship populated with a fake placeholder family — Sam Smith (operator), Alex Rivera (partner), and kids Avery and Jordan — so the checked-in state is self-consistent and runnable without being anyone's actual life. When you personalize, you replace those values with your own in the unsuffixed copies. The sentinel rail is what stops you from accidentally shipping the placeholder cast into a live workspace if you forget to edit a file: `deploy.py`'s Safeguard 10 ([Ch 06](06-infra-setup.md)) refuses any deploy that still carries it.

> 🔦 **Tip.** If you ever see real names or identifiers drift into a `.example` file, treat it as a PII leak and fix it before pushing. The `.example` files are what I expect to be in public git; the unsuffixed copies are what I expect to be in yours only. Running `git status` before every commit and watching for tracked unsuffixed agent configs is a cheap habit that catches this before it matters.

## Picking your LLM provider

Clawford has one LLM provider: **ChatGPT Plus via `codex`**. Every agent's LLM call goes through `agents.shared.llm.infer()`, which is a thin wrapper over the `codex` CLI that OpenAI ships for developers. `codex` authenticates against your ChatGPT Plus subscription on first run (browser OAuth) and writes a token to `~/.codex/auth.json` that subsequent calls reuse. The VPS keeps its own copy of that file, SCP'd from a laptop that did the interactive auth.

The reasoning, for posterity:

- **Flat monthly bill.** ~$20/month regardless of how hard the fleet works. A leaked key cannot bleed me per-token the way a leaked API key can, because there is no per-token — the subscription is the bill.
- **Strong models.** GPT-5.4 is what `codex` currently routes to for the agent workloads Clawford runs — news ranking, meeting-transcript summarization, morning-digest composition, LLM-as-judge for preference learning. I have not needed to swap in a different model in any cron.
- **Scope is auditable.** The credential is tied to an account I already watch. Adding a new client or a new agent doesn't add a new billing surface.

If you want to use a different model provider — API-key OpenAI, Anthropic, a self-hosted model, something else — the seam to swap is `agents/shared/llm.py`. The file is ~200 lines and its public contract is one function: `infer(prompt, *, json_mode=False, timeout=30, model=None) -> InferResult`. As long as a replacement returns the same `InferResult` shape (with `.text` and `.outputs` fields), every agent in the fleet picks up the new backend. I haven't done this and can't vouch for specific alternatives, but the swap is clean if you want it.

> ⚠️ **Warning.** Do not wire Clawford agents against a Claude Pro or Claude Max subscription. Anthropic's developer terms explicitly forbid routing programmatic / agentic traffic through consumer subscriptions — it's an account-ban risk for you and a legal risk for anyone sharing code that demonstrates it. If you want Claude models specifically, use an Anthropic API key (pay-per-token) and a matching backend swap in `llm.py`.

## Spicy take — auto-login and auto-MFA aggressively

Every service with multi-factor authentication (MFA) — Google, Costco, Amazon, LinkedIn, and so on — eventually logs the agent out. The default answer in a lot of agent frameworks is *"the operator hand-types a code whenever it breaks."* I refused to accept that as a stable state and ended up building a fair amount of [Camoufox](https://camoufox.com/) plumbing plus little scrapers that pull login codes out of my authenticator app and my SMS inbox, so the fleet can re-auth itself without me in the loop.

The cost is real: that plumbing is fragile, it takes debugging when a service changes its login flow, and every auth vector is a potential security issue you are choosing to automate instead of interactive-prompt. The benefit is that my overnight crons actually run overnight.

This is a personal choice. Reasonable operators land on the other side and prefer a manual re-auth step with a Telegram nudge — it's simpler, it's arguably safer, and it works if you're at a keyboard often enough. The guide covers the automated path in [Ch 17](17-auth-architectures.md), but if you'd rather not maintain it, that's a defensible call. Know which side you want to be on before you start Ch 04.

## Pick one thing to prioritize

These trade off against each other. Decide which matters most before you design anything:

| If you care most about… | Prioritize… | At the cost of… |
|---|---|---|
| **Privacy** | Self-hosted LLM, hardened VPS, minimal third-party services | Agent capability ceiling drops noticeably |
| **Cost** | API-key-only billing, free Dropbox tier, skip the proxy | Surprise bills are possible; Hilda Hippo doesn't work without a proxy |
| **Unattended reliability** | Auto-MFA everything, Mr Fixit first, fleet-health probe on the host | Highest maintenance burden; most surface area to debug |
| **Breadth of agents** | Deploy all six, integrate broadly | Each additional agent is its own rabbit hole |
| **Speed to first win** | Deploy Mr Fixit only, skip proxies and Hilda | A smaller surface until you add the next agent — though a single-agent setup doesn't even need the shared brain, so it's lean rather than crippled |

There's no wrong answer. There is, however, a wrong answer in pretending you'll prioritize all five and ending up with half of each.

## The meta-lesson — plan for rebuilds, not stability

If there's one idea to carry from this chapter into every other one, it's this: **everything in this fleet will break eventually, and the measure of a good setup is how fast you notice and recover, not how long you go between failures.** Services change their HTML, cookies expire, session tokens rotate during a rebuild, Dropbox conflicts pile up, residential proxies rotate out of a good neighborhood. The stability narrative — *"once it's set up it just runs"* — is a story I told myself early on and paid for later in long debugging sessions.

Every design choice in this guide is downstream of that assumption. Mr Fixit as the canary. The `*.example` template pattern. The `fleet-health.json` probe. Red-green TDD for infra code. Committing everything to git. They only cohere as a system if you start from "this will break, how do I recover fast." Start from "this should be stable," and half of the guide reads like over-engineering until the morning you page yourself trying to figure out which three things broke at once.

## Pitfalls you'll hit

> 🧨 **Pitfall.** Committing real credentials, names, or IDs into an agent config that isn't a `.example` file. **Why:** the repo's PII hygiene assumes every unsuffixed agent config is gitignored; one sloppy `git add -A` and you've leaked a chat ID or a family email into history. I had to rewrite 285 commits of history with `git-filter-repo` the first time I shared this repo, and I do not recommend it as a reusable fix. **How to avoid:** stage files by name, not with `-A` or `.`. Run `git status` before every commit and look for tracked unsuffixed agent configs — they should never appear.

> 🧨 **Pitfall.** Picking WhatsApp "just to try it" because your family already uses it. **Why:** Baileys-based WhatsApp bindings ban within days, and the 24-hour inactivity window silently drops the exact overnight alerts you'd want most. **How to avoid:** stand the fleet up on Telegram first, always. If you later need WhatsApp for human reachability (I do, for Mistress Mouse), wire it as a *delivery target* an agent hands off to, not as the agent's primary channel.

> 🧨 **Pitfall.** Underestimating time-to-first-agent and booking real deadlines against it. **Why:** first deploy of a new agent hits silent failures (Ch 20). A "should be an afternoon" plan routinely turns into three evenings of debugging BotFather, a Dropbox conflict from a file you didn't know was open, and a Playwright login flow that worked locally but not on the VPS. **How to avoid:** plan 2-3 days to Mr Fixit and 2-4 weeks of evenings to a full fleet. Tell the people waiting on you something generous.

## See also

- [Ch 01 — What is Clawford?](index.md) — the framing decisions this chapter's details hang off.
- [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md) — the decision doc explaining why the stack looks the way it does.
- [Ch 04 — VPS setup](04-vps-setup.md) — the next stop; you'll put the decisions here to work there.
- [Ch 05 — Dev setup](05-dev-setup.md) — Claude Code as the dev environment, and the four things it gets wrong.
- [Ch 09 — Mr Fixit](09-mr-fixit.md) — the first-deploy-minefield war story that motivates "deploy Mr Fixit first."
- [Ch 17 — Auth architectures](17-auth-architectures.md) — the automated re-auth patterns the spicy take above is pointing at.
- [README.md](../README.md) — the canonical `*.example` → unsuffixed-sibling copy loop and the "First-time setup" section referenced above.
- [docs/ballad-of-mr-fixit.md](../docs/ballad-of-mr-fixit.md) — for when this chapter starts feeling too dry.
