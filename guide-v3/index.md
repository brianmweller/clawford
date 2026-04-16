![Clawford](../assets/Clawford2.png)

# Clawford Guide (v3)

*Last updated: 2026-04-15 (Phase 6 — Ch 04 and Ch 07 drafted) · Live guide, under active migration from v2*

> **Guide v3 is under active construction.** This is the Clawford-native guide that replaces the OpenClaw-era [guide v2](../guide-v2/index.md). Most chapters are still migrating from v2, with OpenClaw-era scar tissue stripped. For the fully-populated (but frozen) OpenClaw-era version, see guide-v2. For the strategic reasoning behind the rewrite, start with [Ch 02 — What Clawford Isn't](02-what-clawford-isnt.md).

## Full table of contents

| # | Chapter | Status |
|---|---------|--------|
| 01 | [What is Clawford?](01-what-is-clawford.md) | **drafted** (Phase 7d) |
| 02 | [What Clawford Isn't](02-what-clawford-isnt.md) | **drafted** (Phase 0) |
| 03 | [Before you start](03-before-you-start.md) | **drafted** (Phase 7d) |
| 04 | [VPS setup](04-vps-setup.md) | **drafted** (Phase 6) |
| 05 | [Dev setup](05-dev-setup.md) | **drafted** (Phase 7d) |
| 06 | [Infra setup](06-infra-setup.md) | **drafted** (Phase 5) |
| 07 | [Intro to agents](07-intro-to-agents.md) | **drafted** (Phase 6) |
| 07-0 | [Your first agent](07-0-your-first-agent.md) | **drafted** (Phase 7d) |
| 07-1 | [Mr Fixit 🦊🔧](07-1-mr-fixit.md) | **drafted** (Phase 7d) |
| 07-2a | [Lowly Worm — newsfeed 🐛📰](07-2a-lowly-worm-newsfeed.md) | **drafted** (Phase 7d) |
| 07-2b | [Lowly Worm — social 🐛📰](07-2b-lowly-worm-social.md) | **drafted** (Phase 7d) |
| 07-3 | Mistress Mouse 🐭📅 *(net-new in v3)* | pending |
| 07-4 | Sergeant Murphy 🐷🔍 *(net-new in v3)* | pending |
| 07-5 | Huckle Cat 🐱🤝 *(net-new in v3)* | pending |
| 07-6 | [Hilda Hippo 🦛🛒](07-6-hilda-hippo.md) | **drafted** (Phase 7d) |
| 07-7 | Auth architectures *(net-new in v3)* | pending |
| 08 | Security and hardening *(net-new in v3)* | pending |
| 09 | Scripts and configs reference | pending |
| 10 | CLI reference | pending |
| 11 | Glossary | pending |
| 99 | [Unsorted operator lessons](99-unsorted-lessons.md) | **holding pen** — raw lessons awaiting triage into their natural chapters |

See [`docs/v2-to-v3-migration.md`](../docs/v2-to-v3-migration.md) for the per-chapter migration checklist and status.

## What's changing from v2

- **OpenClaw is gone.** Clawford now runs on a Clawford-native stack — `codex` for LLM calls (riding ChatGPT Plus), system crontab for scheduling, a real shared library under `agents/shared/` for the world-access layer, and a shared brain on git + Dropbox. Ch 02 explains why.
- **Nothing is buried.** Every chapter that mentioned OpenClaw in v2 either gets rewritten or migrated with the scar tissue stripped. Where v2 spent paragraphs explaining how to work around OpenClaw's 600-second cron timeout or its exec-approvals allowlist, v3 doesn't have to — those problems are gone.
- **The shared library is real.** v2 treated `agents/shared/` as a handful of deployment helpers. v3 describes a proper three-tier library for world access (clean APIs / stock Playwright / hardened Camoufox), plus a `brain.py` module for the shared-brain read/write pattern, plus an `llm.py` broker for the `codex` backend.

## See also

- [`../guide-v2/index.md`](../guide-v2/index.md) — the frozen OpenClaw-era guide
- [`../docs/v2-to-v3-migration.md`](../docs/v2-to-v3-migration.md) — per-chapter migration status
- [`../docs/ballad-of-mr-fixit.md`](../docs/ballad-of-mr-fixit.md) — the lore version, still accurate
