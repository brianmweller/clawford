![Clawford](../assets/Clawford2.png)

# What is Clawford?

*Last updated: 2026-04-17 · Reading time: ~5 min · Difficulty: easy*

**TL;DR**

- Clawford is a personal fleet of LLM agents running on a cheap VPS. Each agent is a teleported Busytown character — yes, the Richard Scarry ones — and each has a narrow job.
- This guide is the scar-tissue version — what I'd tell a friend over beers, not a pitch deck. Most of it is written because something bit me first.
- It's aimed at people who want *multiple* agents sharing infrastructure, not one standalone bot.
- If you want the myth version before the manual version, read [docs/ballad-of-mr-fixit.md](../docs/ballad-of-mr-fixit.md) first.
- Start with [Ch 03 — Before you start](03-before-you-start.md) when you're ready to move.

## A fox named Mr Fixit

Every morning at 5am, a fox named Mr Fixit checks that the lights are still on in Clawford and sends me a one-line status to Telegram. If the news agent has stopped fetching headlines, Mr Fixit tells me. If a Dropbox sync has conflicted, Mr Fixit tells me. If everything is fine — which, astonishingly, it sometimes is — he says nothing at all, which I have come to recognize as its own kind of love language.

Clawford is what I call the thing that runs him, his four public coworkers, and the small file-based shared memory they all write into. The animals themselves teleported in from [Richard Scarry's Busytown](https://www.richardscarry.com/characters-1) — I borrowed them wholesale and put them to work here, for reasons that will not become clearer. It lives on a $30/month Hetzner box, stitched together with plain host crontab, `codex`, Dropbox, Telegram, some Python, and a great deal of hard-won regret.

(A sixth animal, a shopping agent, runs privately out of a separate repo — retailer automation sits on the wrong side of vendor terms of service to publish openly. This guide covers the five agents the public repo ships.)

## What Clawford tries to do

I wanted software I could trust to do small, recurring, personal-life jobs while I slept:

- Curate the news I actually read (Lowly Worm 🐛📰).
- Keep my family's calendar honest (Mistress Mouse 🐭📅).
- Prep me for meetings and debrief me after (Sergeant Murphy 🐷🔍).
- Remember who's who in my life, and when to follow up (Huckle Cat 🐱🤝).
- Watch the whole fleet so none of the above quietly die (Mr Fixit 🦊🔧).

Each agent has a narrow remit on purpose. They share a common brain — a directory of markdown files with schemas — so one agent can hand context to another without me playing messenger.

## What Clawford does *not* try to do

It is not a general-purpose assistant. It does not replace humans, make autonomous financial decisions, post to family group chats on my behalf, or hold opinions I didn't authorize it to hold. When an agent is unsure, it asks me. When it's sure and wrong, it eventually finds out, and I have words with it in the form of a new entry in a `MEMORY.md` file.

## The shape of the thing

```
          ┌─────────────────────────────────────────┐
          │          Telegram (you ↔ fleet)         │
          └───────────────────┬─────────────────────┘
                              │ 5 per-agent bots + 1 direct bot
                              │
    ┌────────┬───────┬────────┬────────┬────────┐
    │Fix-It  │Lowly  │ Mouse  │ Murphy │ Huckle │  ← Python agents:
    │ 🦊🔧   │ 🐛📰  │  🐭📅  │  🐷🔍  │  🐱🤝  │    SOUL · IDENTITY
    │        │ Worm  │        │        │  Cat   │    · TOOLS · crons
    └────┬───┴───┬───┴────┬───┴────┬───┴────┬───┘
         │       │        │        │        │
         └───────┴────┬───┴────────┴────────┘
                      │
             ┌────────┴────────┐        ┌──────────────┐
             │  host crontab   │        │    codex     │
             │ (every schedule │        │  (LLM calls  │
             │  fires from     │        │  via ChatGPT │
             │  here)          │        │  Plus)       │
             └─────────────────┘        └──────┬───────┘
                      │                        │
                      └───────────┬────────────┘
                                  │
                          ┌───────┴────────┐
                          │  Shared brain  │   ← Dropbox-synced
                          │ (facts, people,│    markdown with
                          │  tasks, notes) │    schemas
                          └────────────────┘
```

The shape is deliberately flat. Agent scripts are plain Python invoked from system crontab. LLM reasoning — ranking, composing, judgment calls — happens via `agents.shared.llm.infer()`, which wraps the `codex` CLI and rides a ChatGPT Plus subscription for every call. Deterministic work — scrapers, browser automation, fleet-health probes — runs as plain Python. There is no container runtime, no gateway service, no LLM cron scheduler with a 600-second ceiling. [Ch 07 — Intro to agents](07-intro-to-agents.md) draws the LLM-vs-deterministic line more carefully; [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md) explains why the shape looks like this and not like the platform Clawford grew out of.

## VPS or Mac mini?

I went with a VPS for four reasons, none of them religious:

1. **Always-on without kicking a family laptop.** A Mac mini under the TV works until someone unplugs it to dust.
2. **Blast radius.** If an agent goes feral, I'd rather that happened on rented hardware than on the box with my tax returns on it.
3. **Clean rebuild.** The whole fleet is Terraform-provisioned and configuration-managed, so "try again from scratch" is a 40-minute thing, not a weekend.
4. **Not my residential IP.** For most services this is a feature; for any Tier 3 integration against a vendor with aggressive bot defense it is the exact opposite, which is how I ended up with strong opinions about routing through home-ISP SOCKS tunnels ([Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice)).

A Mac mini is probably fine if your fleet is smaller, you already own the hardware, and you don't mind the first three points. I don't claim one path is right.

## Who this guide is for

- You're comfortable driving SSH, git, shell, and Python *with Claude Code or Codex as a copilot*. You don't need to be an expert in any of it — you need to be able to wield the tools well enough to unstick yourself when the LLM runs out of ideas.
- You want to run *multiple* agents sharing infrastructure. The whole guide is written around that case.
- You're OK with "works on my box" as a starting point and willing to carry some operational weight yourself.

## Who it isn't for

- **You just want one bot.** If all you need is a single agent to do one thing, Busytown is massive overkill. Clone a smaller scaffold.
- **You want a turnkey product.** This is a field guide, not a product. There are places in it where the answer is "I gave up and wrote a memory file."
- **You need a security-reviewed system.** I'm not a security expert. I'll tell you what I do and what I know I don't do ([Ch 19](19-security-and-hardening.md)), but audited it is not.

## If you read nothing else — quickstart

1. Sign up for a [Hetzner Cloud](https://console.hetzner.cloud/) account and a Dropbox account you're willing to dedicate to this.
2. Create a Telegram account and start a chat with [@BotFather](https://t.me/BotFather) — you'll make bots there.
3. Clone the repo and read [Ch 03 — Before you start](03-before-you-start.md) end-to-end *before* you provision anything.
4. Follow [Ch 04 — VPS setup](04-vps-setup.md) to stand up a locked-down box.
5. Walk [Ch 06 — Infra setup](06-infra-setup.md) to wire up the shared library, the shared brain, host crons, and the Dropbox daemon.
6. Deploy Mr Fixit first, following [Ch 09](09-mr-fixit.md). He's the training-wheels agent and the canary for everyone after him.
7. Wait for your first 5am status message. When it arrives, pour yourself something, and then pick your second agent from Ch 07.

## Caveat emptor

I'm writing this as I learn. Half of it is scar tissue from mistakes, and some of it will be wrong by the time you read it — services change their HTML, APIs deprecate, cookies mutate, and every so often I do something clever with a shell script at midnight that I regret by breakfast. Where I can, I've tried to tell you *why* a thing is the way it is, so that when it stops being that way you'll have enough context to adapt. Where I can't, I've just told you what bit me and left it at that.

If you find something here that's wrong, assume I'd like to know.

## See also

- [The full table of contents](index.md) for this guide.
- [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md) — the decision doc that explains why the runtime looks like this and not like the platform it used to sit on top of.
- [docs/ballad-of-mr-fixit.md](../docs/ballad-of-mr-fixit.md) — a five-act tragedy covering just the first day of setup. A lot more has gone wrong since; the ballad is the overture, not the opera. Read it when you want the lore instead of the manual.
