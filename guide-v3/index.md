![Clawford](../assets/Clawford2.png)

# Clawford Guide (v3)

*Last updated: 2026-04-15 · Live guide*

> **Guide v3 is the live guide.** Every chapter in the table below is drafted against the post-liberation Clawford-native runtime. The OpenClaw-era [guide v2](../guide-v2/index.md) is frozen and preserved as historical record. For the strategic reasoning behind the rewrite, start with [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md).

## Full table of contents

| # | Chapter |
|---|---------|
|   | **Overview** |
| 01 | [What is Clawford?](01-what-is-clawford.md) |
| 02 | [What Isn't Clawford?](02-what-isnt-clawford.md) |
|   | **Setup** |
| 03 | [Before you start](03-before-you-start.md) |
| 04 | [VPS setup](04-vps-setup.md) |
| 05 | [Dev setup](05-dev-setup.md) |
| 06 | [Infra setup](06-infra-setup.md) |
|   | **Agents** |
| 07 | [Intro to agents](07-intro-to-agents.md) |
| 08 | [Your first agent](08-your-first-agent.md) |
| 09 | [Mr Fixit 🦊🔧](09-mr-fixit.md) |
| 10 | [Lowly Worm — newsfeed 🐛📰](10-lowly-worm-newsfeed.md) |
| 11 | [Lowly Worm — social 🐛📰](11-lowly-worm-social.md) |
| 12 | [Mistress Mouse 🐭📅](12-mistress-mouse.md) |
| 13 | [Sergeant Murphy 🐷🔍](13-sergeant-murphy.md) |
| 14 | [Huckle Cat 🐱🤝](14-huckle-cat.md) |
| 15 | [Hilda Hippo 🦛🛒](15-hilda-hippo.md) |
|   | **Architecture** |
| 16 | [The shared brain](16-shared-brain.md) |
| 17 | [Auth architectures](17-auth-architectures.md) |
| 18 | [The inbox: making agents conversational](18-the-inbox.md) |
| 19 | [Security and hardening](19-security-and-hardening.md) |
|   | **Reference** |
| 20 | [Scripts and configs reference](20-scripts-and-configs.md) |
| 21 | [Glossary](21-glossary.md) |

## What's changing from v2

- **OpenClaw is gone.** Clawford now runs on a Clawford-native stack — `codex` for LLM calls (riding ChatGPT Plus), system crontab for scheduling, a real shared library under `agents/shared/` for the world-access layer, and a shared brain on git + Dropbox. Ch 02 explains why.
- **Nothing is buried.** Every chapter that mentioned OpenClaw in v2 either gets rewritten or migrated with the scar tissue stripped. Where v2 spent paragraphs explaining how to work around OpenClaw's 600-second cron timeout or its exec-approvals allowlist, v3 doesn't have to — those problems are gone.
- **The shared library is real.** v2 treated `agents/shared/` as a handful of deployment helpers. v3 describes a proper three-tier library for world access (clean APIs / stock Playwright / hardened Camoufox), plus a `brain.py` module for the shared-brain read/write pattern, plus an `llm.py` broker for the `codex` backend.

## See also

- [`../guide-v2/index.md`](../guide-v2/index.md) — the frozen OpenClaw-era guide
- [`../docs/ballad-of-mr-fixit.md`](../docs/ballad-of-mr-fixit.md) — the lore version, still accurate
