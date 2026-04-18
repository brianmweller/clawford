# Scripts and configs reference

*Last updated: 2026-04-16 · Reading time: ~10 min · Difficulty: reference*

> **TL;DR.** This chapter is a reference, not a tutorial. It catalogs every tracked script and config file that a Clawford fleet touches, organized by **what it does** (shared library / deploy tooling / ops helpers / per-agent) rather than **where it lives**. If you know the name of the thing you are looking for, jump to [§ Alphabetical index](#alphabetical-index). If you know the *job* you need to do but not the name of the file, start with the category that matches the job. Every entry links back to the chapter that explains the deeper context.

## Category 1 — Shared library

Everything in `agents/shared/` is world-access infrastructure that every agent can import. This is the "Tier 1 / Tier 2 / Tier 3" world-access layer from [Ch 06 — Infra setup](06-infra-setup.md), plus a handful of ops modules.

### Tier 1 — clean APIs

| Module | Exports | What it does |
|--------|---------|--------------|
| `agents/shared/telegram_api.py` | `send_telegram`, `timed_send`, `send_photo`, `edit_message` | The Telegram bot client. Unified 429 backoff, automatic retry via `retry_policy.py`. Every agent that sends a message goes through this module. |
| `agents/shared/telegram_inbox.py` | `poll_updates`, `parse_callback_query`, `parse_command` | The inbound side — reads bot updates, dispatches `/confirm N` + `/dismiss N` + inline keyboard callbacks. |
| `agents/shared/google_oauth.py` | `build_flow`, `get_credentials`, `refresh_if_stale` | Wraps `InstalledAppFlow` with the detail you always forget — redirect URI, scope validation, token-refresh drift handling. See [Ch 17 Shape 1](17-auth-architectures.md#shape-1-oauth-20-with-long-lived-refresh-token). |
| `agents/shared/llm.py` | `infer(prompt, *, json_mode, timeout) -> InferResult` | The LLM broker. Routes through the `codex infer` CLI riding a ChatGPT Plus subscription. Zero marginal cost, enforces timeout, logs every prompt+response for audit. |
| `agents/shared/brain.py` | `read_brain`, `write_brain`, `update_people_file`, `append_inbox` | The shared-brain access layer. Atomic writes with a Dropbox-aware lock file. See [Ch 06 — Infra setup](06-infra-setup.md) for the git + Dropbox split. |
| `agents/shared/heartbeat_base.py` | `HeartbeatProbe` base class | Every agent's `scripts/heartbeat.py` subclasses this and implements `probe() -> dict`. The base class handles the timing envelope, the error catch, and the exit-code discipline. |

### Tier 2 — stock Playwright

| Module | Exports | What it does |
|--------|---------|--------------|
| `agents/shared/playwright_profile.py` | `launch_persistent_profile`, `ensure_xvfb`, `cleanup_profile_lock` | Chromium persistent-profile launch + Xvfb management for the "cookies live, tokens refresh" case. Consumers: LinkedIn keepalive in [Ch 11](11-lowly-worm-social.md), Google Messages scraper in [Ch 14](14-huckle-cat.md). |

### Tier 3 — Camoufox + residential proxy

| Module | Exports | What it does |
|--------|---------|--------------|
| `agents/shared/camoufox_proxy.py` | `launch_hardened`, `sticky_proxy_session`, `totp_code(secret)` | Camoufox launcher with residential-proxy sticky port + TOTP helper for auto-MFA. See [Ch 17 Shape 5](17-auth-architectures.md#shape-5-camoufox-residential-proxy-auto-mfa) for the deployment story and [Ch 15 Hilda Hippo](15-hilda-hippo.md) for the scar tissue. |

### Ops modules

| Module | Exports | What it does |
|--------|---------|--------------|
| `agents/shared/deploy.py` | CLI: `python3 -m agents.shared.deploy <agent>` | The deploy tool. 10 active safeguards. See [Ch 07 — Intro to agents](07-intro-to-agents.md) and [Ch 19 § Defense layer 3](19-security-and-hardening.md#defense-layer-3-the-deploy-tool-safeguards). |
| `agents/shared/contract_wrap.py` | `contract_main(probe_fn)` | Wrapper that every cron-invoked script uses to enforce the script contract (exit 0 always, one JSON line on stdout, no shell). See [Ch 19 § Defense layer 2](19-security-and-hardening.md#defense-layer-2-the-script-contract). |
| `agents/shared/dispatcher.py` | `dispatch_command(text, handlers)` | Telegram command dispatcher — parses `/confirm 3` + `/dismiss 3` + inline callbacks and routes to handler functions. |
| `agents/shared/conversation.py` | `Conversation` context manager | Stateful conversation helper for multi-turn LLM dialogues. Most agents don't use this; it exists for the narrow set that genuinely need it. |
| `agents/shared/fleet_health_types.py` | `AgentStatus`, `FleetHealth`, `probe_fields` | Dataclasses for fleet-health schema. Imported by every agent's `heartbeat.py` and by `ops/scripts/fleet-health.py`. |
| `agents/shared/retry_policy.py` | `retry_with_backoff`, `RateLimit429` | Unified retry for network calls — used by `telegram_api.py`, `llm.py`, and every agent that talks to external services. |
| `agents/shared/subprocess_helpers.py` | `run_ok`, `run_capture` | Argument-list-only subprocess wrappers (no `shell=True` ever). Enforces [Ch 19 § Defense layer 2](19-security-and-hardening.md#defense-layer-2-the-script-contract) rule 3 at the import surface. |
| `agents/shared/tool_use.py` | `Tool`, `ToolRegistry` | Deprecated. Predates the script contract. No live consumers; kept for reference while a full retirement is evaluated. |
| `agents/shared/workspace-snapshot.py` | CLI | Snapshots a workspace directory for debugging + drift analysis. Not called from cron; operator tool. |

## Category 2 — Deploy and install tooling

Everything in `ops/scripts/` that installs, registers, or bootstraps something on the VPS. These are the scripts the operator runs (or that another script runs) to make the VPS look the way the repo says it should.

| Script | Runs where | What it does |
|--------|-----------|--------------|
| `ops/scripts/install-host-cron.sh` | VPS | The single source of truth for the crontab. Drift-detects and rewrites `crontab -l` against the `CONTRACT_ENTRIES` block + `DIRECT_ENTRIES` block embedded in the script. Idempotent. The cron surface for every agent lives here; see [Ch 06 — Infra setup](06-infra-setup.md). |
| `ops/scripts/install-host-deps.sh` | VPS | `pip install` for the shared library's Python dependencies. Reads the pinned `requirements.txt`. |
| `ops/scripts/install-host-system-deps.sh` | VPS | `sudo apt install` for the system-level dependencies that the pip packages need to build (libjpeg-dev for Pillow, xvfb + openbox for Camoufox headful fallback). Must run before `install-host-deps.sh` on a fresh VPS. |
| `ops/scripts/sync-brain-to-vps.sh` | local | Rsync helper for pushing the git-tracked half of the shared brain (`ops/brain/*`) to the VPS. The Dropbox half of the brain syncs via the Dropbox daemon; this script only handles the git half. |
| `ops/scripts/set-bot-commands.sh` | local | Registers the Telegram bot's `/command` menu via BotFather's API. One-shot during initial deploy of a new bot. |
| `ops/scripts/set-bot-descriptions.sh` | local | Registers the Telegram bot's description + about text. Same shape as `set-bot-commands.sh`. |

## Category 3 — Ops helpers and host cron wrappers

The "host wrappers" — one `*-host.sh` per cron — sit in `ops/scripts/` and are the glue between `cron` and the Python scripts that live in each agent's workspace. Every wrapper follows the same shape: set `ENV_FILE`, set `LOG_FILE`, set `LOCK_FILE` (for single-instance crons), then `exec` the Python script with the right argument list.

| Wrapper | Cron schedule (UTC) | What it wraps | Reference |
|---------|----------------------|---------------|-----------|
| `ops/scripts/fleet-health-host.sh` | `*/15 * * * *` | `ops/scripts/fleet-health.py` | [Ch 09 — Mr Fixit](09-mr-fixit.md) |
| `ops/scripts/morning-status-host.sh` | `*/30 * * * *` | The fix-it morning status poll | [Ch 09 — Mr Fixit](09-mr-fixit.md) |
| `ops/scripts/morning-fleet-deliver-host.sh` | `0 12 * * *` | The 5 AM PT fleet delivery aggregator (reads every agent's `cache/morning-brief-ready.txt` and sends one composite Telegram message) | [Ch 06 — Infra setup](06-infra-setup.md) |
| `ops/scripts/news-digest-morning-edition-host.sh` | `30 10 * * *` | Lowly Worm's morning composition cron | [Ch 10](10-lowly-worm-newsfeed.md) |
| `ops/scripts/fix-it-cron-self-check-host.sh` | `*/30 * * * *` | Mr Fixit's self-check probe | [Ch 09 — Mr Fixit](09-mr-fixit.md) |
| `ops/scripts/costco-token-refresh-host.sh` | every 15 min | Hilda Hippo's Costco persistent-daemon token refresh | [Ch 15 Hilda Hippo](15-hilda-hippo.md) |
| `ops/scripts/script-contract-host.sh` | manual / deploy-time | Runs the script-contract compliance check against a target agent | [Ch 19 § Defense layer 2](19-security-and-hardening.md#defense-layer-2-the-script-contract) |

Ops scripts that are not cron-invoked:

| Script | What it does |
|--------|-------------|
| `ops/scripts/fleet-health.py` | The fleet-health orchestrator — calls every agent's `heartbeat.py`, aggregates the results into `~/Dropbox/clawford-backup/fleet-health.json`, surfaces anomalies. |
| `ops/scripts/probe-agent.py` | Operator tool. Runs an agent's heartbeat probe in isolation and prints the JSON output. Used for debugging a single agent without firing the whole fleet-health cron. |
| `ops/scripts/crlf-scan.py` | Windows-dev-box hygiene — scans the repo for CRLF line endings in tracked `*.sh` files. Run as a pre-commit check on the Windows machine. See [Ch 05 § Windows dev box](05-dev-setup.md). |
| `ops/scripts/test_fleet_health.py` | Pytest suite for `fleet-health.py`. |
| `ops/scripts/test_probe_agent.py` | Pytest suite for `probe-agent.py`. |
| `ops/scripts/test_crlf_scan.py` | Pytest suite for `crlf-scan.py`. |

## Category 4 — Per-agent script conventions

Every agent directory under `agents/{agent}/` follows the same skeleton. The exact files vary by agent — see the per-agent chapter for the full list — but the naming conventions are consistent.

### Required per-agent files

| Path | Purpose | Reference |
|------|---------|-----------|
| `agents/{agent}/manifest.json` | The per-agent manifest: cron list, workspace path, smoke test, config-file list. Consumed by `deploy.py`. See [§ manifest.json schema](#manifestjson-schema) below. |
| `agents/{agent}/SOUL.md.example` | Agent identity doc template — gitignored `SOUL.md` is populated from this during deploy. Receives `chattr +i` post-deploy. | [Ch 19 § Defense layer 1](19-security-and-hardening.md#defense-layer-1-os-level-immutability) |
| `agents/{agent}/IDENTITY.md.example` | Operator-facing identity template. Same treatment as `SOUL.md`. | [Ch 19 § Defense layer 1](19-security-and-hardening.md#defense-layer-1-os-level-immutability) |
| `agents/{agent}/TOOLS.md` | Complete tool inventory — every script the agent can run, what each does, what each requires. This file is tracked in git (not `.example`) because it is operator-editable rather than LLM-editable. |
| `agents/{agent}/AGENTS.md` | Agent-interaction surface. How this agent talks to the other agents in the fleet (if at all). |
| `agents/{agent}/CRONS.md` | Per-cron spec — the authoritative description of every cron the agent registers. The `CRONS.md` file is the source of truth that `install-host-cron.sh` is validated against. |
| `agents/{agent}/scripts/heartbeat.py` | Required. Subclasses `HeartbeatProbe` from `heartbeat_base.py`. Produces a JSON status line for `fleet-health.py` to aggregate. |
| `agents/{agent}/.env.example` | Template for the workspace-local `.env` file. Tracked in git with placeholder values; the real `.env` is gitignored. |
| `agents/{agent}/.gitignore` | Per-agent gitignore listing every credential-shaped filename (`token.json`, `credentials.json`, `.env`, `cache/`, profile directories). |

### Script naming conventions

| Pattern | Meaning | Example |
|---------|---------|---------|
| `{vendor}-auth.py` or `{vendor}-auth-manual.py` | One-shot OAuth setup script, runs locally, SCPed token to VPS. | `google-auth-setup.py`, `transcript-auth-manual.py` |
| `{vendor}-fetch.py` | I/O-only reader from an external source. Deterministic Python, no LLM, emits JSON. | `gcal-fetch.py`, `gmail-fetch.py` |
| `{vendor}-mine.py` | One-off or periodic mining script. Heavier than `-fetch.py` — pagination, caching, rate-limiting. | `gmessages-mine.py`, `workflowy-read.py` |
| `{thing}-check.py` | Deterministic read + classify. No side effects. | `activity-email-check.py`, `gmail-invite-check.py` |
| `{thing}-alert.py` | Orchestrator — calls `-check.py`, runs LLM, sends Telegram. Always a host cron entry point. | `activity-email-alert.py`, `whatsapp-chat-alert.py` |
| `{thing}-host.sh` | Cron wrapper. Lives under `ops/scripts/`, not `agents/{agent}/scripts/`. Exec bit in git. | `fleet-health-host.sh`, `morning-fleet-deliver-host.sh` |
| `heartbeat.py` | Per-agent required file. Subclasses `HeartbeatProbe`. | every agent has one |

## Category 5 — Config file schemas

### `manifest.json` schema

Every agent has exactly one `manifest.json` at `agents/{agent}/manifest.json`. The deploy tool reads it and runs Safeguard 7 against it. Required fields:

```json
{
  "agent_id": "family-calendar",
  "display_name": "Mistress Mouse",
  "emoji": "🐭📅",
  "workspace": "~/.clawford/family-calendar-workspace",
  "config_files": [
    {"source": "SOUL.md.example", "target": "SOUL.md", "immutable": true},
    {"source": "IDENTITY.md.example", "target": "IDENTITY.md", "immutable": true},
    {"source": "TOOLS.md", "target": "TOOLS.md", "immutable": true},
    {"source": "calendar-config.template.json", "target": "calendar-config.json", "immutable": false}
  ],
  "crons": [
    {"name": "morning-briefing", "schedule": "30 10 * * *", "script": "scripts/morning-briefing.py"}
  ],
  "smoke_test": "python3 scripts/heartbeat.py --dry-run"
}
```

Safeguard 7 validates every field. Missing `agent_id`, missing `workspace`, mismatched `agent_id` / `workspace` path, malformed `cron.schedule`, or missing `smoke_test` all refuse the deploy.

### `.env` schema

Every agent's `.env` file is loaded by `agents.shared.env.load_env` on script startup. The file is KEY=VALUE with one entry per line. Values containing whitespace or special characters go in double quotes.

Common keys across multiple agents:

| Key | Used by | Purpose |
|-----|---------|---------|
| `TELEGRAM_BOT_TOKEN` | every agent that sends | The bot's API token |
| `TELEGRAM_CHAT_ID` | every agent that sends | The chat id to send to |
| `WORKFLOWY_TOKEN` | [Murphy](13-sergeant-murphy.md), [Huckle](14-huckle-cat.md) | Workflowy bearer token |
| `PROXY_USER` / `PROXY_PASS` / `PROXY_HOST` / `PROXY_PORT` | [Hilda Hippo](15-hilda-hippo.md) | Residential proxy credentials, sticky port is 10000 |
| `{VENDOR}_TOTP_SECRET` | [Hilda Hippo](15-hilda-hippo.md) | The TOTP seed for auto-MFA on vendor login |

### Identity file conventions

The four identity files (`SOUL.md`, `IDENTITY.md`, `TOOLS.md`, `AGENTS.md`) all live at the root of the agent's workspace and all get `chattr +i` after deploy. Two come from `.example` templates (`SOUL.md.example`, `IDENTITY.md.example`) because they get per-agent identity substitution; the other two are tracked in git directly because they are operator-editable reference docs.

See [Ch 19 § Defense layer 1](19-security-and-hardening.md#defense-layer-1-os-level-immutability) for the reasoning.

## Alphabetical index

For when you know the name but not the category.

- [`brain.py`](#tier-1-clean-apis) — Category 1
- [`camoufox_proxy.py`](#tier-3-camoufox-residential-proxy) — Category 1
- [`contract_wrap.py`](#ops-modules) — Category 1
- [`conversation.py`](#ops-modules) — Category 1
- [`costco-token-refresh-host.sh`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`crlf-scan.py`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`deploy.py`](#ops-modules) — Category 1
- [`dispatcher.py`](#ops-modules) — Category 1
- [`fix-it-cron-self-check-host.sh`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`fleet-health-host.sh`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`fleet-health.py`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`fleet_health_types.py`](#ops-modules) — Category 1
- [`google_oauth.py`](#tier-1-clean-apis) — Category 1
- [`heartbeat_base.py`](#tier-1-clean-apis) — Category 1
- [`install-host-cron.sh`](#category-2-deploy-and-install-tooling) — Category 2
- [`install-host-deps.sh`](#category-2-deploy-and-install-tooling) — Category 2
- [`install-host-system-deps.sh`](#category-2-deploy-and-install-tooling) — Category 2
- [`llm.py`](#tier-1-clean-apis) — Category 1
- [`manifest.json`](#manifestjson-schema) — Category 5
- [`morning-fleet-deliver-host.sh`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`morning-status-host.sh`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`news-digest-morning-edition-host.sh`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`playwright_profile.py`](#tier-2-stock-playwright) — Category 1
- [`probe-agent.py`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`retry_policy.py`](#ops-modules) — Category 1
- [`script-contract-host.sh`](#category-3-ops-helpers-and-host-cron-wrappers) — Category 3
- [`set-bot-commands.sh`](#category-2-deploy-and-install-tooling) — Category 2
- [`set-bot-descriptions.sh`](#category-2-deploy-and-install-tooling) — Category 2
- [`subprocess_helpers.py`](#ops-modules) — Category 1
- [`sync-brain-to-vps.sh`](#category-2-deploy-and-install-tooling) — Category 2
- [`telegram_api.py`](#tier-1-clean-apis) — Category 1
- [`telegram_inbox.py`](#tier-1-clean-apis) — Category 1
- [`tool_use.py`](#ops-modules) — Category 1 (deprecated)
- [`workspace-snapshot.py`](#ops-modules) — Category 1

## See also

- [Ch 06 — Infra setup](06-infra-setup.md) — the shared library tier discussion + the shared brain + the host-cron runtime
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the deploy path and the safeguard story
- [Ch 08 — Your first agent](08-your-first-agent.md) — the walkthrough that uses these scripts end-to-end
- [Ch 19 — Security and hardening](19-security-and-hardening.md) — the three defense layers, including the script contract + deploy safeguards
- [Ch 21 — Glossary](21-glossary.md) — alphabetical reference for every term the guide uses
