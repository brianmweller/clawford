# OpenClaw Agent System — Setup Guide

You've built a personal AI agent system themed after Richard Scarry's Busytown. Six specialized agents, each with its own Telegram bot and Busytown character identity, sharing a common knowledge layer and running 24/7 on a $15/month VPS. All six are deployed.

This guide walks you through the entire setup — from an empty Hetzner account to a working agent sending you Telegram messages. It is opinionated. It tells you what works, what doesn't, and what will waste your time. Every command has been tested. Every warning was learned the hard way.

## The Busytown Agent Roster

| Character | Agent | Role | Complexity |
|-----------|-------|------|-----------|
| 🦊🔧 **Mr Fixit** | fix-it | Infrastructure monitoring, repair, archival, security audits | Deploy first |
| 🐭📅 **Mistress Mouse** | family-calendar | Logistics, scheduling, family comms | High |
| 🐷🔍 **Sergeant Murphy** | meetings-coach | Meeting prep, debrief, follow-ups | Medium |
| 🦛🛒 **Hilda Hippo** | shopping | Multi-channel purchasing | Medium |
| 🐛📰 **Lowly Worm** | news-digest | Media curation and delivery | Low |
| 🐱🤝 **Huckle Cat** | connector | Relationship management, brain bootstrapping | High |

*Mr Frumble is on standby for when things go catastrophically wrong.*

## Prerequisites

- A [Hetzner Cloud](https://console.hetzner.cloud/) account (~$15/month for cpx31)
- [Telegram](https://telegram.org/) installed on your phone
- An [OpenAI](https://platform.openai.com/) subscription or API key (or Anthropic equivalent)
- Comfort with SSH and the command line
- ~4 hours for the initial setup (Chapters 0–4)

## Chapters

| # | Chapter | What You'll Do | Time |
|---|---------|---------------|------|
| 0 | [Before You Start](00-before-you-start.md) | Make decisions that save you hours | 15 min |
| 1 | [VPS Setup](01-vps-setup.md) | Provision a server with Terraform + Docker | 45 min |
| 2 | [Shared Brain](02-shared-brain.md) | Build the file-based knowledge layer | 20 min |
| 3 | [Dropbox Sync](03-dropbox-sync.md) | Get offsite backup working on a headless VPS | 45 min |
| 4 | [First Agent](04-first-agent.md) | Deploy Mr Fixit end-to-end | 60 min |
| 5 | [Telegram Bots](05-telegram-bots.md) | Give each agent its own identity | 15 min |
| 6 | [Testing](06-testing.md) | Verify your agent actually works | 20 min |
| 7 | [Hardening](07-hardening.md) | Fix the security gap you don't know you have | 15 min |
| 8 | [Growing the Team](08-growing-the-team.md) | Add more agents using the same pattern | varies |
| 9 | [CLI Reference](09-cli-reference.md) | The corrected command reference | bookmark |
| 10 | [Obsidian Bridge](10-obsidian-bridge.md) | Connect your vault to the agent brain | 15 min |

Start with [Chapter 0](00-before-you-start.md).
