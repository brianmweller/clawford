# OpenClaw Agents

A six-agent personal AI system running on OpenClaw. Each agent has a dedicated Telegram bot, a defined role, and a set of scheduled cron jobs.

## Agents

| Agent | Status | Bot | Role |
|-------|--------|-----|------|
| **Mr Fixit** (fix-it) | Deployed | @openclaw_fixit_bot | Infrastructure, monitoring, repair, archival |
| Family Calendar | Not deployed | — | Logistics, scheduling, family comms |
| Meetings Coach | Not deployed | — | Meeting prep, debrief, follow-ups |
| Shopping | Not deployed | — | Multi-channel purchasing |
| News Digest | Not deployed | — | Media curation and delivery |
| Connector | Not deployed | — | Relationship management, social nudges |

## Architecture

- **VPS:** mindclaw (Hetzner, Ubuntu 24.04)
- **Shared Brain:** `~/Dropbox/openclaw-backup/` — file-based knowledge layer (facts, commitments, tasks, notes, people)
- **Sync:** Dropbox (VPS ↔ cloud ↔ local)
- **Messaging:** Per-agent Telegram bots
- **LLM:** OpenAI Codex (gpt-5.4) via OpenClaw gateway

## Directory Structure

```
openclaw-agents/
├── DEPLOY.md              # Generic agent deployment template
├── docs/                  # Design docs and specs
│   ├── shared-brain-schema.md
│   └── ...
├── brain/                 # Shared brain setup
│   └── setup-brain.sh
├── agents/                # Per-agent packages
│   └── fix-it/
│       ├── SOUL.md        # Core operating principles
│       ├── IDENTITY.md    # Personality and tone
│       ├── TOOLS.md       # Available tools and permissions
│       ├── CRONS.md       # Scheduled jobs spec
│       └── deploy.sh      # Deployment automation
└── tests/                 # Test harness
    └── setup-tests.sh
```

## Setup Guide

Start here: **[guide/index.md](guide/index.md)** — a 10-chapter field manual covering VPS provisioning, shared brain, Dropbox sync, agent deployment, testing, and security hardening. Every command battle-tested.

Quick start:

```bash
cp .env.example .env
# Fill in Telegram bot tokens and chat ID
```

See also [DEPLOY.md](DEPLOY.md) for the per-agent deployment reference.

## Security

- Agent SOUL.md and IDENTITY.md files are made immutable on the VPS (`chattr +i`) after deployment
- Exec commands restricted via `openclaw approvals allowlist`
- Per-agent Telegram bots prevent cross-agent impersonation
- Secrets stored in `.env` (gitignored), never in code
