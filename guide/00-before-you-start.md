# Chapter 0: Before You Start

These decisions will save you hours. Get them wrong and you'll redo your entire setup.

---

## Use an isolated machine, not your daily driver

OpenClaw has full filesystem access. It can read, write, and execute anything the OS user can. Running it on your work laptop is a recipe for accidental file modifications, runaway processes, and security incidents.

Use a dedicated VPS. Hetzner's cpx31 (4 vCPU, 8GB RAM, 160GB disk) costs ~$15/month and is more than enough for six agents. A Raspberry Pi or old Mac Mini also works, but a VPS gives you uptime, a static IP, and easy reprovisioning.

## Use Telegram, not WhatsApp

OpenClaw supports both. Use Telegram.

WhatsApp uses an unofficial library (Baileys) that reverse-engineers the WhatsApp Web protocol. This causes:
- **Account bans** — most users report being banned within days
- **24-hour messaging window** — WhatsApp blocks proactive messages from bots after 24 hours of inactivity, which means your overnight cron alerts won't deliver
- **Session expiration** — the web session silently expires, and your agents go dark without any error

Telegram uses the official Bot API. No ban risk, no messaging window, unlimited proactive messages, and each bot gets its own identity with a custom name and avatar.

> **ACTION:** Install Telegram on your phone now if you haven't already. You'll need it in Chapter 4.

## Deploy Fix-It first, not all six agents

Fix-It is the infrastructure agent — it monitors the health of all other agents, validates the shared brain, runs security audits, and archives stale data. Deploy it first because:

1. It teaches you the full deployment workflow with low stakes (if Fix-It breaks, nothing else depends on it yet)
2. Once running, it monitors everything you deploy afterward
3. The CLI syntax has several undocumented gotchas — better to learn them on an infrastructure agent than on one that touches your calendar or email

## Choose your model provider

OpenClaw supports multiple LLM providers. Your main options:

| Option | Cost | Setup |
|--------|------|-------|
| **OpenAI Codex subscription** | ~$20/month flat | OAuth login during onboarding |
| **OpenAI API key** | Pay-per-use (~$0.01-0.10 per agent turn) | Set `OPENAI_API_KEY` in `.env` |
| **Anthropic API key** | Pay-per-use | Set `ANTHROPIC_API_KEY` in `.env` |
| **Claude subscription** | ~$20/month flat | **Not recommended** — may violate ToS |

The Codex subscription is the best value for most users — flat rate, no surprise bills, and GPT-5.4 is excellent for agent tasks.

> **WARNING:** Using a Claude Pro/Max subscription with OpenClaw may violate Anthropic's Terms of Service. Use API keys instead if you want to use Claude models.

## Create a non-root user

Do not run OpenClaw as root. You'll hit Homebrew permission errors, can't use systemd user services, and it's a security risk. The Terraform setup in Chapter 1 creates an `openclaw` user automatically. If setting up manually, create one:

```bash
adduser openclaw
usermod -aG sudo openclaw
usermod -aG docker openclaw
```

---

Next: [Chapter 1 — VPS Setup](01-vps-setup.md)
