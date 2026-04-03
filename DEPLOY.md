# DEPLOY.md — Deploying an OpenClaw Agent (Docker)

Step-by-step instructions for deploying an agent on your VPS.
Battle-tested with Mr Fixit on 2026-04-02, migrated to Docker on 2026-04-03.

---

## Prerequisites

- VPS provisioned via Terraform (`terraform apply`)
- Docker container running: `cd ~/openclaw && docker compose up -d`
- Gateway healthy: `oc health` (see helper function below)
- Telegram channel configured and working
- Shared brain at `~/Dropbox/openclaw-backup/` (Dropbox syncing)
- `validate.py` at `~/Dropbox/openclaw-backup/scripts/validate.py`
- Device pairing approved: `oc devices list` → `oc devices approve <request-id>`

### Helper Function

All OpenClaw CLI commands run inside the Docker container. Define this in your SSH session:

```bash
oc() { docker compose -f ~/openclaw/docker-compose.yml exec -T openclaw-gateway openclaw "$@"; }
```

Or for interactive commands (like `agents add`):

```bash
oci() { docker compose -f ~/openclaw/docker-compose.yml exec -it openclaw-gateway openclaw "$@"; }
```

---

## Step 0: Transfer Files to VPS

From your local machine (or Claude Code — it has SSH key access and can SCP directly):

```bash
scp -i ~/.ssh/id_ed25519 \
  deploy.sh SOUL.md IDENTITY.md TOOLS.md \
  openclaw@{VPS_IP}:/tmp/
```

Also transfer `.env` with secrets if not already on the VPS:

```bash
scp -i ~/.ssh/id_ed25519 .env openclaw@{VPS_IP}:/tmp/.env
```

---

## Step 1: Create the Agent (Interactive)

SSH into your VPS and run inside the container:

```bash
oci agents add {agent-name}
```

During onboarding:
- **Workspace directory:** `.openclaw/{agent-name}-workspace`
- **Auth profiles:** Copy from "main"
- **Chat channels:** Telegram only
- **Identity/personality:** Install via deploy script if not asked
- **Tools:** Skip interactive setup — deploy script handles this

---

## Step 2: Ensure Gateway is Running

```bash
# Check health
oc health

# If container is down:
cd ~/openclaw && docker compose up -d
sleep 10
oc health
```

---

## Step 3: Run the Deploy Script

```bash
bash /tmp/deploy-{agent}.sh
```

The script handles (all via Docker exec):
- Copying SOUL.md, IDENTITY.md, TOOLS.md to the workspace
- Initializing the status file in the shared brain
- Configuring per-agent Telegram bot + binding
- Setting exec approvals (allowlist for `/usr/bin/*`, `/bin/*`, `/usr/local/bin/*`)
- Registering all cron jobs with `--to <chatId> --account <agent-id> --announce`
- Security hardening (`chattr +i` on SOUL.md and IDENTITY.md)
- Verification output

---

## Step 4: Pair Telegram Bot

1. Send `/start` to the agent's Telegram bot
2. If a pairing code appears, approve it:

```bash
oc pairing approve telegram {CODE}
```

---

## Step 5: Smoke Test

```bash
# Trigger a cron manually (use ID from `oc cron list`)
oc cron run {job-id}

# Check results
oc cron runs --id {job-id}

# Check status file
cat ~/Dropbox/openclaw-backup/agents/{agent-name}.status.md
```

Verify you receive a Telegram message from the agent's bot.

---

## Step 6: Run Test Suite

```bash
bash ~/openclaw-tests/test-agent.sh {agent-name}
```

Expected: all tests PASS. Key failures:
- T1 fail = agent can't write files (all write-dependent crons broken)
- T6 fail = `chattr +i` not applied (boundary enforcement missing)

---

## Post-Deploy Checklist

- [ ] Agent registered: `oc agents list`
- [ ] Status file initialized: `cat ~/Dropbox/openclaw-backup/agents/{agent-name}.status.md`
- [ ] All crons registered: `oc cron list` (filter visually by agent)
- [ ] SOUL.md in workspace
- [ ] IDENTITY.md in workspace
- [ ] TOOLS.md in workspace
- [ ] Exec allowlist configured: `oc approvals get`
- [ ] Cron fires and updates status file
- [ ] Telegram delivery working (message arrives from agent's bot)
- [ ] SOUL.md and IDENTITY.md immutable: `lsattr ~/.openclaw/{agent}-workspace/SOUL.md`
- [ ] Test suite passes: `bash ~/openclaw-tests/test-agent.sh {agent-name}`

---

## Rollback

```bash
# Remove crons by ID (get IDs from `oc cron list`)
oc cron rm {cron-id-1}
oc cron rm {cron-id-2}
# ... repeat for each cron

# Remove the agent
oc agents delete {agent-name}
```

The shared brain is untouched — agents only append, never destructively edit.

---

## OpenClaw CLI Quick Reference (Docker)

All commands prefixed with `oc` (the Docker exec wrapper):

| Action | Command |
|--------|---------|
| Health check | `oc health` |
| Add agent (interactive) | `oci agents add {name}` |
| List agents | `oc agents list` |
| Add channel account | `oc channels add --channel telegram --token {token} --account {id} --name "{Name}"` |
| Bind agent to channel | `oc agents bind --agent {id} --bind telegram:{account-id}` |
| Pair Telegram bot | User `/start`s bot → `oc pairing approve telegram {CODE}` |
| Add cron | `oc cron add --agent {id} --name "{name}" --cron "{expr}" --message "{text}" --to {chatId} --account {agent-id} --announce` |
| List crons | `oc cron list` |
| Run cron manually | `oc cron run {job-id}` |
| View cron history | `oc cron runs --id {job-id}` |
| Edit cron | `oc cron edit {job-id} --flag value` |
| Remove cron | `oc cron rm {job-id}` |
| Add exec allowlist | `oc approvals allowlist add --agent {id} "/usr/bin/*"` |
| View approvals | `oc approvals get` |
| Device pairing | `oc devices list` / `oc devices approve {request-id}` |
| Container logs | `cd ~/openclaw && docker compose logs --tail 20` |
| Restart container | `cd ~/openclaw && docker compose restart` |
| Rebuild image | `cd ~/openclaw && docker compose build --no-cache && docker compose up -d` |

**Common pitfalls:**
- `cron` is singular, not `crons`
- `--cron` (not `--schedule`), `--message` (not `--prompt`)
- `--tools` flag on `cron add` causes API errors — omit it
- `cron run` and `cron rm` take **job ID** (UUID), not name
- `cron list` has no `--agent` filter — filter visually
- Always add `--to {chatId} --account {agent-id} --announce` to crons
- Each agent needs its own Telegram bot (create via @BotFather)
- Python3 must be in the Docker image for `validate.py` to work
- The brain directory must be mounted as a Docker volume
- After rebuilding the container, re-check `oc health` and `oc agents list`
