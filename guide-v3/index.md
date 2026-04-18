![Clawford](../assets/Clawford2.png)

# Table of contents

*Last updated: 2026-04-17*

Twenty-one chapters, six sections. Pick a chapter by the job you want to
do, or read top-to-bottom if you're deploying a fleet from scratch.
Reading-time estimates are rough (230 words per minute); difficulty is
easy / moderate / hard / reference.

## Overview

<div class="grid cards" markdown>

-   **[01 — What is Clawford?](01-what-is-clawford.md)**

    ---

    `easy` · ~5 min

    A personal fleet of LLM agents on a $30/month VPS. Each agent is a
    teleported Busytown character with a narrow job.

-   **[02 — What Isn't Clawford?](02-what-isnt-clawford.md)**

    ---

    `moderate` · ~20 min

    The decision doc for why Clawford left the OpenClaw platform —
    written up front, not buried at the back as a retrospective.

</div>

## Setup

<div class="grid cards" markdown>

-   **[03 — Before you start](03-before-you-start.md)**

    ---

    `easy` · ~15 min

    The one-way-door decisions: dedicated VPS, Telegram over WhatsApp,
    Mr Fixit first. Cheap to get right, expensive to reverse.

-   **[04 — VPS setup](04-vps-setup.md)**

    ---

    `moderate` · ~15 min

    Terraform-provisioned Hetzner box. SSH-hardened, Tailscale overlay,
    `codex` installed, residential proxy wired in and tested.

-   **[05 — Dev setup](05-dev-setup.md)**

    ---

    `moderate` · ~15 min

    Claude Code as the dev environment. Red-green TDD for infra. The
    four things Claude Code gets wrong, in decreasing order of cost.

-   **[06 — Infra setup](06-infra-setup.md)**

    ---

    `moderate` · ~20 min

    Three boring pieces: the three-tier shared library, the shared
    brain (git + Dropbox), the host-cron runtime. Ten active deploy
    safeguards.

</div>

## Agents

<div class="grid cards" markdown>

-   **[07 — Intro to agents](07-intro-to-agents.md)**

    ---

    `moderate` · ~15 min

    The anatomy of a Clawford agent: eight workspace files, a manifest,
    the script contract, and the LLM-vs-deterministic line.

-   **[08 — Your first agent](08-your-first-agent.md)**

    ---

    `moderate` · ~10 min

    The seven-step first-deploy arc every agent inherits — Telegram
    bot, workspace bootstrap, scripts, deploy, smoke test.

-   **[09 — Mr Fixit 🦊🔧](09-mr-fixit.md)**

    ---

    `hard` · ~20 min

    The infrastructure fox. Fleet-health canary, brain validator,
    conflict-scanner. Currently on probation after the 2026-04-11
    confabulation episode.

-   **[10 — Lowly Worm — newsfeed 🐛📰](10-lowly-worm-newsfeed.md)**

    ---

    `moderate` · ~15 min

    A personalized morning news digest that learns from your thumbs.
    The preference-learning story is the hero of this chapter.

-   **[11 — Lowly Worm — social 🐛📰](11-lowly-worm-social.md)**

    ---

    `hard` · ~25 min

    Optional LinkedIn layer on top of the core newsfeed. Playwright,
    aria-label selectors, and the smart-reply chip incident.

-   **[12 — Mistress Mouse 🐭📅](12-mistress-mouse.md)**

    ---

    `hard` · ~20 min

    Family logistics: Google Calendar reader, three-tier reminders,
    school-email parser, WhatsApp digest. First Google OAuth agent.

-   **[13 — Sergeant Murphy 🐷🔍](13-sergeant-murphy.md)**

    ---

    `hard` · ~20 min

    Meeting prep, transcript-driven debrief, commitment tracking,
    coaching. Home of the 5x resend incident and the cache-is-not-a-queue
    rule.

-   **[14 — Huckle Cat 🐱🤝](14-huckle-cat.md)**

    ---

    `hard` · ~20 min

    Relationship memory across seven sources. The only agent where the
    mining pipeline runs **before** the first cron — by design.

-   **[15 — Hilda Hippo 🦛🛒](15-hilda-hippo.md)**

    ---

    `hard` · ~35 min

    Amazon + Costco orders, Subscribe & Save, delivery digest. Hardest
    auth in the fleet — a seven-act saga through Akamai, Azure B2C,
    and the home-ISP tunnel.

</div>

## Architecture

<div class="grid cards" markdown>

-   **[16 — The shared brain](16-shared-brain.md)**

    ---

    `moderate` · ~10 min

    The git + Dropbox directory that turns a pile of agents into a
    fleet. Four primitives, two halves, all appends.

-   **[17 — Auth architectures](17-auth-architectures.md)**

    ---

    `hard` · ~15 min

    Six auth shapes across the fleet, plus three cross-cutting idioms:
    local-then-SCP, gitignored credentials, no raw API keys in crons.

-   **[18 — The inbox](18-the-inbox.md)**

    ---

    `moderate` · ~20 min

    The inbound side. One async daemon polls six bots, routes each
    message to the right agent, stages every mutation behind a Confirm
    button.

-   **[19 — Security and hardening](19-security-and-hardening.md)**

    ---

    `hard` · ~25 min

    Seven defense layers against drift, accident, credential leak,
    trust erosion, promptware, and active attack.

</div>

## Reference

<div class="grid cards" markdown>

-   **[20 — Scripts and configs](20-scripts-and-configs.md)**

    ---

    `reference` · ~10 min

    The catalog: every tracked script and config file, organised by
    job. A lookup, not a read-through.

-   **[21 — Glossary](21-glossary.md)**

    ---

    `reference` · ~15 min

    Every Clawford-specific term, alphabetically. Plus named incidents
    and retired terms for historical context.

</div>

## Lore

<div class="grid cards" markdown>

-   **[The Ballad of Mr Fixit](../docs/ballad-of-mr-fixit.md)**

    ---

    `easy` · ~10 min

    A five-act tragedy covering the first day of setup. The myth
    version before the manual version.

</div>

## See also

- [Guide v2](../guide-v2/index.md) — the frozen OpenClaw-era guide,
  preserved as historical record.
