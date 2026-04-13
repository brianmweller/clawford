# Clawford — A Busytown for OpenClaw Agents

A six-agent personal AI system themed after Richard Scarry's Busytown. Each agent has its own Telegram bot, a defined role, and a Busytown character identity. Runs on OpenClaw.

## The Agents

| Character | Agent | Status | Bot | Role |
|-----------|-------|--------|-----|------|
| 🦊🔧 **Mr Fixit** | fix-it | Deployed | @openclaw_fixit_bot | Infrastructure, monitoring, repair, archival |
| 🐛📰 **Lowly Worm** | news-digest | Deployed | @openclaw_lowly_worm_bot | Media curation and delivery |
| 🦛🛒 **Hilda Hippo** | shopping | Deployed | @openclaw_shopping_bot | Multi-channel purchasing |
| 🐭📅 **Mistress Mouse** | family-calendar | Deployed | @openclaw_mistress_mouse_bot | Family logistics, scheduling, comms |
| 🐷🔍 **Sergeant Murphy** | meetings-coach | Deployed | @openclaw_sergeant_murphy_bot | Meeting prep, coaching, debrief, commitments |
| 🐱🤝 **Huckle Cat** | connector | Deployed | @openclaw_huckle_cat_bot | Relationship management, brain bootstrapping, notes triage |

*And if anything breaks catastrophically: **Mr Frumble** is waiting in the wings.*

## Telegram Relay

`telegram-relay/` is a lightweight local bot that replaces the VPS-based relay agent. It runs on your desktop and bridges Telegram to your local Claude Code CLI — no VPS, no Tailscale, no exposed ports.

```bash
pip install -r telegram-relay/requirements.txt
export TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="..." OPENAI_API_KEY="..."
python telegram-relay/bot.py --cwd /path/to/project
```

Features: text relay, voice messages (Whisper transcription), `/ping`, `/status`, `/cwd`.

## Architecture

- **VPS:** <your-tailscale-host> (Hetzner cpx31, Hillsboro OR) — Terraform-managed, Docker-based
- **Shared Brain:** `~/Dropbox/openclaw-backup/` — file-based knowledge layer (facts, commitments, tasks, notes, people)
- **Sync:** Dropbox (VPS ↔ cloud ↔ local)
- **Messaging:** Per-agent Telegram bots (one bot per character)
- **Local relay:** `telegram-relay/bot.py` — Telegram ↔ Claude Code on desktop
- **LLM:** OpenAI Codex (gpt-5.4) via OpenClaw gateway

## Directory Structure

```
clawford/
├── CHANGELOG.md           # Release history
├── DEPLOY.md              # Generic agent deployment template (Docker)
├── VERSION                # Current version
├── agents/                # Per-agent packages
│   ├── fix-it/            # 🦊 Mr Fixit (deployed)
│   ├── news-digest/       # 🐛 Lowly Worm (deployed)
│   ├── shopping/          # 🦛 Hilda Hippo (deployed)
│   ├── family-calendar/   # 🐭 Mistress Mouse (deployed)
│   ├── meetings-coach/    # 🐷 Sergeant Murphy (deployed)
│   └── connector/         # 🐱 Huckle Cat (deployed) + mine/ data pipeline
├── telegram-relay/        # Local Telegram ↔ Claude Code bot
├── brain/                 # Shared brain setup
│   └── setup-brain.sh
├── docs/                  # Design docs and specs
│   ├── ballad-of-mr-fixit.md
│   └── shared-brain-schema.md
├── guide/                 # 10-chapter setup field manual
│   └── index.md           # Start here
├── infra/                 # Infrastructure config
├── openclaw-docker-config/ # Docker config for OpenClaw
└── tests/                 # Test harness
    └── setup-tests.sh
```

## Setup Guide

Start here: **[guide/index.md](guide/index.md)** — a 10-chapter field manual covering VPS provisioning, shared brain, Dropbox sync, agent deployment, testing, and security hardening.

## First-time setup: populate local config files

Agent config / identity / state files (`USER.md`, `SOUL.md`, `MEMORY.md`, `manifest.json`, etc. under `agents/*/`) contain personal data — real names, calendar IDs, DOBs, contact emails — that isn't tracked in git. Only their sanitized `.example` templates are committed. On a fresh clone:

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

Templates use an obviously-fake placeholder family (Sam Smith / Alex Rivera / Avery Smith-Rivera / Jordan Smith-Rivera / Jamie Park). Replace with your own identities.

Also copy `.env.example` to `.env` and fill in your secrets (Telegram bot tokens, OpenAI API key, VPS host, etc.).

## Security

- Agent SOUL.md and IDENTITY.md files are immutable on the VPS (`chattr +i`)
- Exec commands restricted via `openclaw approvals allowlist`
- Per-agent Telegram bots prevent cross-agent impersonation
- Secrets stored in `.env` (gitignored), never in code

## Lore

- **[The Ballad of Mr Fixit](docs/ballad-of-mr-fixit.md)** — A play in five acts. A fox terraformed three times, possessed by dark magic, and installed in the chair of a murdered pig. Based on true events.
