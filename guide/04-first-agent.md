# Chapter 4: First Agent — Deploying Mr Fixit

Your first agent will hit every obstacle. That's why Fix-It is first — it monitors everything else, and you learn the deployment process before anything important breaks.

---

## What Mr Fixit does

Mr Fixit is the IT admin of your agent network. It runs 9 scheduled cron jobs:

| Cron | Frequency | Telegram |
|------|-----------|----------|
| Heartbeat check | Every 30 min | Silent on all-clear, alerts on unhealthy agent |
| Morning status | Daily 06:00 UTC | Always (your daily briefing) |
| Brain validation | Every 6 hours | Silent on pass, alerts on failure |
| Dropbox conflict scan | Every 2 hours | Silent on clean, alerts on conflict |
| File size monitor | Daily 12:00 UTC | Silent on clean, alerts on large file |
| Monthly archival | 1st of month | Always (reports what was archived) |
| Security audit | Weekly Sunday | Always (reports findings) |
| Update check | Weekly Wednesday | Always (reports version) |
| Cron self-check | Daily midnight | Silent on pass, alerts on missing crons |

Routine checks use `--no-deliver` + `--failure-alert` so you only get notified when something needs attention. Reports (morning status, archival, security, updates) always deliver.

## Step 1: Create a Telegram bot

1. Open Telegram, message **@BotFather**
2. Send `/newbot`
3. Name: `Mr Fixit`
4. Username: `openclaw_fixit_bot` (must end in `bot`)
5. Copy the bot token — add it to your `.env` as `FIXIT_BOT_TOKEN`

Give the bot a fox avatar if you want to match its personality.

## Step 2: Transfer files to the VPS

From your local machine:

```bash
scp -i ~/.ssh/id_ed25519 \
  agents/fix-it/deploy.sh \
  agents/fix-it/SOUL.md \
  agents/fix-it/IDENTITY.md \
  agents/fix-it/TOOLS.md \
  openclaw@{server_ip}:/tmp/
```

Also transfer `.env` if not already on the VPS:

```bash
scp -i ~/.ssh/id_ed25519 .env openclaw@{server_ip}:/tmp/.env
```

## Step 3: Create the agent (interactive)

SSH into your VPS and run inside the Docker container:

```bash
oci agents add fix-it
```

During onboarding:
- **Workspace directory:** `/home/node/.openclaw/fix-it-workspace`
- **Copy auth profiles from main?** Yes
- **Chat channels:** Telegram only

The agent will be created. It may or may not ask for identity information — don't worry, the deploy script installs everything.

## Step 4: Device pairing — the hidden blocker

Check for pending pairing requests:

```bash
oc devices list
```

If you see a pending request, approve it:

```bash
oc devices approve {request-id}
```

> **WARNING:** Without device pairing approval, ALL agent operations fail with "pairing required." This is not documented in the CLI help. It is the single most common reason for a "working" agent that does nothing.

## Step 5: Run the deploy script

```bash
bash /tmp/deploy.sh
```

The script:
1. Copies SOUL.md, IDENTITY.md, TOOLS.md to the workspace
2. Initializes the status file in the shared brain
3. Adds the Mr Fixit Telegram bot as a channel account
4. Binds the fix-it agent to the `fixit` Telegram account
5. Configures exec approvals (`/usr/bin/*`, `/bin/*`, `/usr/local/bin/*`)
6. Registers all 9 cron jobs with Telegram delivery
7. Locks SOUL.md and IDENTITY.md with `chattr +i`
8. Prints verification output

## Step 6: Pair the Telegram bot

1. Send `/start` to your Mr Fixit bot on Telegram
2. If a pairing code appears, approve it:

```bash
oc pairing approve telegram {CODE}
```

Some setups auto-pair from the backup — if the bot responds without a code, you're good.

## Step 7: Smoke test

Get a cron job ID from the list:

```bash
oc cron list
```

Trigger the heartbeat check:

```bash
oc cron run {heartbeat-check-uuid}
```

Check your Telegram — you should get a message from Mr Fixit within 60 seconds. Also check the status file:

```bash
cat ~/Dropbox/openclaw-backup/agents/fix-it.status.md
```

The `last_heartbeat` should show the current time.

## The nine obstacles

These are the obstacles we hit during the first deployment. You may hit them too.

1. **`openclaw crons` → `openclaw cron`** — The CLI uses singular `cron`, not `crons`
2. **`--schedule` → `--cron`** — The cron expression flag is `--cron`, not `--schedule`
3. **`--prompt` → `--message`** — The message flag is `--message`, not `--prompt`
4. **`--tools` causes API errors** — Omit the `--tools` flag entirely on `cron add`
5. **"Pairing required"** — Run `oc devices list` and approve pending requests
6. **Exec denied in crons** — Add exec allowlist: `oc approvals allowlist add --agent fix-it "/usr/bin/*"`
7. **Telegram delivery fails** — Every cron needs `--to {chatId} --account {agent-id} --announce`
8. **`cron run` takes UUID, not name** — Get the UUID from `oc cron list`
9. **Gateway not running** — All operations fail. Check: `oc health`. Fix: `cd ~/openclaw && docker compose up -d`
10. **SCP'ing `openclaw.json` wipes agent registrations** — Agent registrations, bindings, and channel accounts are stored in `openclaw.json`. Never overwrite it from a local copy after agents are registered. Always pull the live version first: `ssh ... "cat ~/.openclaw/openclaw.json" > local.json`

## Post-deploy checklist

- [ ] Agent registered: `oc agents list` shows fix-it
- [ ] Status file created with heartbeat timestamp
- [ ] All 9 crons registered: `oc cron list`
- [ ] SOUL.md, IDENTITY.md, TOOLS.md in workspace
- [ ] Exec allowlist configured: `oc approvals get`
- [ ] Cron fires and updates status file
- [ ] Telegram message arrives from Mr Fixit bot
- [ ] SOUL.md and IDENTITY.md are immutable: `lsattr ~/.openclaw/fix-it-workspace/SOUL.md`

---

Next: [Chapter 5 — Telegram Bots](05-telegram-bots.md)
