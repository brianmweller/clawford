# Clawford — A Busytown for personal AI agents

A six-agent personal fleet running on a single VPS. Each agent is a
Richard Scarry Busytown character with a narrow remit, its own Telegram
bot, and shared memory it can read from and write to.

> Clawford was originally a thin layer on top of [OpenClaw](https://openclaw.ai).
> Between phases 0 and 7 (April 2026) every OpenClaw dependency was
> progressively removed — the LLM cron runtime, the exec-approvals
> layer, the gateway container, the `~/.openclaw/` workspace root,
> the `oc` / `oci` CLI wrappers — and Clawford is now a self-contained
> system of plain Python scripts, host crons, and a `deploy.py`
> built for purpose. The full story is in [`guide-v3/02-what-clawford-isnt.md`](guide-v3/02-what-clawford-isnt.md).
> The frozen pre-liberation guide is in [`guide-v2/`](guide-v2/index.md).

## The Agents

| Character | Agent | Role | Bot |
|-----------|-------|------|-----|
| 🦊🔧 **Mr Fixit** | fix-it | Fleet operator: monitoring, repair, archival, morning status | `@…_fixit_bot` |
| 🐛📰 **Lowly Worm** | news-digest | News + LinkedIn curation, ranked to your taste | `@…_lowly_worm_bot` |
| 🦛🛒 **Hilda Hippo** | shopping | Amazon + Costco orders, subscribe-and-save, morning delivery digest | `@…_shopping_bot` |
| 🐭📅 **Mistress Mouse** | family-calendar | Family schedule, reminders, school activity emails | `@…_mistress_mouse_bot` |
| 🐷🔍 **Sergeant Murphy** | meetings-coach | Meeting prep, debrief, commitment tracking, coaching | `@…_sergeant_murphy_bot` |
| 🐱🤝 **Huckle Cat** | connector | Relationship memory, daily nudges, notes triage | `@…_huckle_cat_bot` |

*If anything breaks catastrophically: **Mr Frumble** is waiting in the wings.*

## Architecture

- **Runtime:** plain host crons on the VPS, invoking Python scripts via
  `/usr/bin/python3` directly. No container, no LLM cron scheduler.
- **VPS:** Hetzner cpx31 (Hillsboro OR), Terraform-provisioned.
  Workspace root at `~/.clawford/`.
- **LLM:** OpenAI Codex via `codex infer`, riding a ChatGPT Plus
  subscription (zero marginal cost per call). Wrapped by
  `agents/shared/llm.py`.
- **Shared brain:** `~/Dropbox/openclaw-backup/` — markdown files for
  facts, people, commitments, tasks, notes. Synced via Dropbox to your
  workstation with 180-day version history. (The legacy `openclaw-backup`
  name is preserved to avoid resetting Dropbox sync history.)
- **Messaging:** one Telegram bot per agent, plus a fleet-wide direct
  bot for Mr Fixit's alerts.
- **Deploys:** `agents/shared/deploy.py` — file sync + 10 safeguards
  including pre-deploy backup, source-cleanliness gate, drift
  detection, and config-source resolution. SSH to the VPS, `git pull`,
  run `deploy.py <agent>` — local invocation writes to a dead mirror,
  not the production fleet.

## Telegram Relay

`telegram-relay/` is a lightweight local bot that bridges Telegram to
your local Claude Code CLI — no VPS, no Tailscale, no exposed ports.
Replaces the older Rudolf Von Flugel relay agent.

```bash
pip install -r telegram-relay/requirements.txt
export TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="..." OPENAI_API_KEY="..."
python telegram-relay/bot.py --cwd /path/to/project
```

Features: text relay, voice messages (Whisper transcription), `/ping`,
`/status`, `/cwd`.

## Directory Structure

```
clawford/
├── CHANGELOG.md           # Release history
├── DEPLOY.md              # Canonical deploy workflow + safeguards
├── VERSION                # Current version
├── agents/
│   ├── shared/            # deploy.py, llm.py, telegram, brain, fleet manifest
│   ├── fix-it/            # 🦊 Mr Fixit
│   ├── news-digest/       # 🐛 Lowly Worm
│   ├── shopping/          # 🦛 Hilda Hippo
│   ├── family-calendar/   # 🐭 Mistress Mouse
│   ├── meetings-coach/    # 🐷 Sergeant Murphy
│   └── connector/         # 🐱 Huckle Cat
├── ops/
│   ├── scripts/           # install-host-cron.sh, *-host.sh wrappers,
│   │                      # fleet-health.py, deploy helpers
│   └── brain/             # git-tracked brain config + scripts
├── telegram-relay/        # Local Telegram ↔ Claude Code bot
├── guide-v3/              # Live setup guide (Clawford-native)
├── guide-v2/              # Frozen pre-liberation guide (OpenClaw-era)
├── docs/                  # Design docs, ballad of Mr Fixit, migration notes
└── tests/                 # Test harness
```

## Setup Guide

Start here: **[guide-v3/index.md](guide-v3/index.md)** — the live
field manual. Covers VPS provisioning, codex auth, shared brain,
Dropbox sync, agent deployment, host-cron runtime, testing, and
hardening. Chapter 02 (`02-what-clawford-isnt.md`) explains the
liberation in detail.

The pre-liberation guide is preserved in [`guide-v2/`](guide-v2/index.md)
as a historical record of how the system used to work on top of
OpenClaw.

## First-time setup: populate local config files

Agent config / identity / state files (`USER.md`, `SOUL.md`,
`MEMORY.md`, `manifest.json`, etc. under `agents/*/`) contain personal
data — real names, calendar IDs, DOBs, contact emails — that isn't
tracked in git. Only their sanitized `.example` templates are
committed. On a fresh clone:

```bash
# Copy every agent-config .example template to its real name, then edit in your values.
find agents -name '*.example' | while read f; do
  target="${f%.example}"
  if [ ! -e "$target" ]; then
    cp "$f" "$target"
    echo "Created $target — edit in your values"
  fi
done
```

Files you'll want to review and personalize after the copy:
- `agents/*/USER.md` — your name, timezone, Telegram ID, communication preferences
- `agents/*/SOUL.md`, `agents/*/MEMORY.md` — agent behavior + persistent context
- `agents/family-calendar/calendar-config.json` — your Google Calendar IDs
- `agents/meetings-coach/meeting-config.json` — your work calendar + speaker names for transcript coaching
- `agents/connector/scripts/mine/family_map.py` + `mining-config.json` — your family email map and contact mining config
- `agents/*/manifest.json` — cron schedules + Telegram bot bindings

Templates use an obviously-fake placeholder family (Sam Smith / Alex
Rivera / Avery Smith-Rivera / Jordan Smith-Rivera / Jamie Park).
Replace with your own identities.

Also copy `.env.example` to `.env` and fill in your secrets (Telegram
bot tokens, OpenAI API key, VPS host, etc.).

## Security

- Agent SOUL.md and IDENTITY.md files are immutable on the VPS (`chattr +i`)
- Per-agent Telegram bots prevent cross-agent impersonation
- Secrets stored in `.env` (gitignored), never in code
- Three-tier defense: OS-level immutability on identity files, the
  script contract for cron messages, and `deploy.py`'s ten safeguards.
  See `guide-v3/06-infra-setup.md` for the full picture.

## Lore

- **[The Ballad of Mr Fixit](docs/ballad-of-mr-fixit.md)** — A play in
  five acts. A fox terraformed three times, possessed by dark magic,
  and installed in the chair of a murdered pig. Based on true events.
