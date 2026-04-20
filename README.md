# Clawford — A Busytown for personal AI agents

A personal fleet of LLM agents running on a single VPS. Each agent is
a Richard Scarry Busytown character with a narrow remit, its own
Telegram bot, and shared memory it can read from and write to.

> **This is a personal project, published for reading and reference.**
> Not accepting contributions — please don't open PRs or issues as a
> support channel. Fork freely under the MIT license.
>
> Clawford was originally a thin layer on top of OpenClaw. Every
> OpenClaw dependency was progressively removed over April 2026 — the
> LLM cron runtime, the exec-approvals layer, the gateway container,
> the `~/.openclaw/` workspace root, the `oc` / `oci` CLI wrappers —
> and Clawford is now a self-contained system of plain Python scripts,
> host crons, and a `deploy.py` built for purpose. The full story is
> in [`guide-v3/02-what-isnt-clawford.md`](guide-v3/02-what-isnt-clawford.md).

## ⚠️ Vendor terms disclaimer

This repository ships only the integrations that sit within the
relevant vendor terms of service:

- Google (Gmail, Calendar, Google Messages) via official OAuth
- Krisp meeting transcripts via MCP
- RSS / Pocket feeds
- Telegram bot APIs

A companion *shopping* agent that automates retailer flows (Amazon
Subscribe & Save, Costco reorders), plus a *social* pipeline that
scrapes LinkedIn, plus a *WhatsApp* delivery channel via reverse-
engineered `Baileys` — all violate their respective vendor terms
and run privately out-of-tree. They are intentionally not in this
repo. If you're considering building your own fleet: decide whether
your use case justifies the ToS risk before reaching for those
patterns.

## 📖 The Guide

**Start here: [guide-v3/index.md](guide-v3/index.md)** — twenty-one
chapters, written as scar-tissue. The README is an orientation map;
the guide is the field manual.

**Overview**

- [01 — What is Clawford?](guide-v3/01-what-is-clawford.md) — A personal fleet on a $30/mo VPS; the Busytown premise.
- [02 — What Isn't Clawford?](guide-v3/02-what-isnt-clawford.md) — Why Clawford left the OpenClaw platform.

**Setup**

- [03 — Before you start](guide-v3/03-before-you-start.md) — One-way-door decisions: dedicated VPS, Telegram channel, Mr Fixit first.
- [04 — VPS setup](guide-v3/04-vps-setup.md) — Terraform-provisioned Hetzner box, SSH-hardened, Tailscale overlay, `codex` installed.
- [05 — Dev setup](guide-v3/05-dev-setup.md) — Claude Code as the dev environment; red-green TDD for infra.
- [06 — Infra setup](guide-v3/06-infra-setup.md) — Three-tier shared library, shared brain (git + Dropbox), host-cron runtime, ten deploy safeguards.

**Agents**

- [07 — Intro to agents](guide-v3/07-intro-to-agents.md) — The anatomy of a Clawford agent: workspace files, manifest, script contract, LLM-vs-deterministic line.
- [08 — Your first agent](guide-v3/08-your-first-agent.md) — The seven-step first-deploy arc every agent inherits.
- [09 — Mr Fixit 🦊🔧](guide-v3/09-mr-fixit.md) — The infrastructure fox. Fleet-health canary, brain validator, conflict-scanner.
- [10 — Lowly Worm 🐛📰](guide-v3/10-lowly-worm-newsfeed.md) — A personalized morning news digest that learns from your thumbs.
- [12 — Mistress Mouse 🐭📅](guide-v3/12-mistress-mouse.md) — Family logistics; first Google OAuth agent.
- [13 — Sergeant Murphy 🐷🔍](guide-v3/13-sergeant-murphy.md) — Meeting prep, transcript-driven debrief, commitment tracking, coaching.
- [14 — Huckle Cat 🐱🤝](guide-v3/14-huckle-cat.md) — Relationship memory across seven sources.

**Architecture**

- [16 — The shared brain](guide-v3/16-shared-brain.md) — The git + Dropbox directory that turns a pile of agents into a fleet.
- [17 — Auth architectures](guide-v3/17-auth-architectures.md) — Six auth shapes across the fleet, three cross-cutting idioms.
- [18 — The inbox](guide-v3/18-the-inbox.md) — One async daemon polls six bots, routes each message, stages every mutation behind a Confirm button.
- [19 — Security and hardening](guide-v3/19-security-and-hardening.md) — Seven defense layers; bubblewrap profile covers every non-Fixit cron.

**Reference**

- [20 — Scripts and configs](guide-v3/20-scripts-and-configs.md) — The catalog. A lookup, not a read-through.
- [21 — Glossary](guide-v3/21-glossary.md) — Every Clawford-specific term, alphabetically.

## The Agents

| Character | Agent | Role | Bot |
|-----------|-------|------|-----|
| 🦊🔧 **Mr Fixit** | fix-it | Fleet operator: monitoring, repair, archival, morning status | `@…_fixit_bot` |
| 🐛📰 **Lowly Worm** | news-digest | RSS + Pocket newsfeed curation, ranked to your taste | `@…_lowly_worm_bot` |
| 🐭📅 **Mistress Mouse** | family-calendar | Family schedule, reminders, school activity emails | `@…_mistress_mouse_bot` |
| 🐷🔍 **Sergeant Murphy** | meetings-coach | Meeting prep, debrief, commitment tracking, coaching | `@…_sergeant_murphy_bot` |
| 🐱🤝 **Huckle Cat** | connector | Relationship memory, daily nudges, notes triage | `@…_huckle_cat_bot` |

*If anything breaks catastrophically: **Mr Frumble** is waiting in the wings.*

## Telegram Relay

`telegram-relay/` is a lightweight local bot that bridges Telegram to
your local Claude Code CLI — no VPS, no Tailscale, no exposed ports.

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
│   ├── shared/            # deploy.py, llm.py, telegram, brain, operator, fleet manifest
│   ├── fix-it/            # 🦊 Mr Fixit
│   ├── news-digest/       # 🐛 Lowly Worm
│   ├── family-calendar/   # 🐭 Mistress Mouse
│   ├── meetings-coach/    # 🐷 Sergeant Murphy
│   └── connector/         # 🐱 Huckle Cat
├── ops/
│   ├── scripts/           # install-host-cron.sh, *-host.sh wrappers,
│   │                      # fleet-health.py, deploy helpers
│   └── brain/             # git-tracked brain config + scripts
├── telegram-relay/        # Local Telegram ↔ Claude Code bot
├── guide-v3/              # Field guide (setup, operation, hardening)
└── tests/                 # Test harness
```

## First-time setup: populate local config files

Agent config / identity / state files (`USER.md`, `SOUL.md`,
`MEMORY.md`, `manifest.json`, etc. under `agents/*/`) contain personal
data — real names, calendar IDs, DOBs, contact emails — that isn't
tracked in git. Only their sanitized `.example` templates are
committed.

There's also one new file to create: `~/.clawford/operator.json`.
This holds the operator's own email addresses and name variants so
miners and triage scripts can tell "mail I sent" from "mail someone
sent me." Template is `agents/shared/operator.json.example`:

```bash
mkdir -p ~/.clawford
cp agents/shared/operator.json.example ~/.clawford/operator.json
# edit ~/.clawford/operator.json with your actual emails + name variants
```

Then the per-agent config sweep:

```bash
# Copy every agent-config .example template to its real name, then edit in your values.
find agents -name '*.example' -not -name 'operator.json.example' | while read f; do
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
  See [`guide-v3/19-security-and-hardening.md`](guide-v3/19-security-and-hardening.md)
  for the full picture.
- Security reports: see [`SECURITY.md`](SECURITY.md).

## License

MIT. See [`LICENSE`](LICENSE).
