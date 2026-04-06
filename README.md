# Clawford — A Busytown for OpenClaw Agents

A six-agent personal AI system themed after Richard Scarry's Busytown. Each agent has its own Telegram bot, a defined role, and a Busytown character identity. Runs on OpenClaw.

## The Agents

| Character | Agent | Status | Bot | Role |
|-----------|-------|--------|-----|------|
| 🦊🔧 **Mr Fixit** | fix-it | Deployed | @openclaw_fixit_bot | Infrastructure, monitoring, repair, archival |
| 🐛📰 **Lowly Worm** | news-digest | Deployed | @openclaw_lowly_worm_bot | Media curation and delivery |
| 🐭📅 **Mistress Mouse** | family-calendar | Not deployed | — | Logistics, scheduling, family comms |
| 🐷🔍 **Sergeant Murphy** | meetings-coach | Not deployed | — | Meeting prep, debrief, follow-ups |
| 🦛🛒 **Hilda Hippo** | shopping | Not deployed | — | Multi-channel purchasing |
| 🐱🤝 **Huckle Cat** | connector | Not deployed | — | Relationship management, social nudges |

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
│   └── news-digest/       # 🐛 Lowly Worm (deployed)
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

## Security

- Agent SOUL.md and IDENTITY.md files are immutable on the VPS (`chattr +i`)
- Exec commands restricted via `openclaw approvals allowlist`
- Per-agent Telegram bots prevent cross-agent impersonation
- Secrets stored in `.env` (gitignored), never in code

## Lore

- **[The Ballad of Mr Fixit](docs/ballad-of-mr-fixit.md)** — A play in five acts. A fox terraformed three times, possessed by dark magic, and installed in the chair of a murdered pig. Based on true events.
