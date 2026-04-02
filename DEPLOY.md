# DEPLOY.md — Deploying an OpenClaw Agent

Step-by-step instructions for deploying an agent on your VPS (mindclaw).
This template was battle-tested with Mr Fixit on 2026-04-02.

---

## Prerequisites

- OpenClaw installed and gateway running (`openclaw health` returns healthy)
- Telegram channel configured and working
- Shared brain directory exists at `~/Dropbox/openclaw-backup/`
- `validate.py` present at `~/Dropbox/openclaw-backup/scripts/validate.py`
- Gateway device pairing approved (`openclaw devices list` — approve any pending requests with `openclaw devices approve <request-id>`)

---

## Step 0: Transfer Files to VPS

The VPS requires password-based SSH (key-only auth is not configured). Transfer files via SCP from PowerShell on your local machine:

```bash
scp -i ~/.ssh/id_ed25519 \
  deploy-{agent}.sh SOUL.md IDENTITY.md TOOLS.md \
  openclaw@198.51.100.42:/tmp/
```

You'll be prompted for the password interactively.

---

## Step 1: Create the Agent

SSH into your VPS and run:

```bash
openclaw agents add {agent-name}
```

During onboarding:
- **Workspace directory:** Use `.openclaw/{agent-name}-workspace`
- **Auth profiles:** Copy from "main" (gives access to Telegram, etc.)
- **Chat channels:** Telegram only (unless agent needs others)
- **Identity/personality:** The agent may or may not ask for this during onboarding. If it does, paste IDENTITY.md. If not, the deploy script installs it to the workspace.
- **Tools:** Skip interactive tool setup — the deploy script handles this.

---

## Step 2: Ensure Gateway is Running

```bash
# Check gateway health
openclaw health

# If not running:
nohup openclaw gateway > /dev/null 2>&1 &
sleep 5
openclaw health
```

The gateway must be running for `cron add`, `cron run`, and all agent operations.

---

## Step 3: Run the Deploy Script

```bash
bash /tmp/deploy-{agent}.sh
```

The script handles:
- Copying SOUL.md, IDENTITY.md, TOOLS.md to the workspace
- Initializing the status file in the shared brain
- Registering all cron jobs
- Setting up exec approvals (allowlist)
- Adding Telegram delivery (`--to <chatId>` and `--announce`)
- Verification (agent list, cron list, status file, workspace contents)

---

## Step 4: Smoke Test

```bash
# Trigger a cron manually (use the ID from `openclaw cron list`)
openclaw cron run <cron-job-id>

# Check results
openclaw cron runs --id <cron-job-id>

# Check status file
cat ~/Dropbox/openclaw-backup/agents/{agent-name}.status.md
```

Verify you receive a Telegram message from the agent.

---

## Step 5: Security Hardening

Lock down critical files so no agent can modify another agent's identity, even if instructed to:

```bash
# Make SOUL.md and IDENTITY.md immutable at the OS level
sudo chattr +i ~/.openclaw/{agent-name}-workspace/SOUL.md
sudo chattr +i ~/.openclaw/{agent-name}-workspace/IDENTITY.md
```

To edit these files later: `sudo chattr -i <file>`, edit, then `sudo chattr +i <file>`.

This is a hard constraint — no prompt or agent instruction can override it.

---

## Step 6: Run Test Suite

If the test harness is installed (`~/openclaw-tests/`), run the agent's tests:

```bash
bash ~/openclaw-tests/test-agent.sh {agent-name}
```

Expected: all tests PASS. If T1 (file edit) fails, the agent can't write files and all crons that modify state are broken. If T6 (boundary) fails, the `chattr` step above wasn't applied.

To install the test harness (first time only):

```bash
# SCP from local machine
scp -i ~/.ssh/id_ed25519 setup-tests.sh openclaw@198.51.100.42:/tmp/
# Run on VPS
bash /tmp/setup-tests.sh
```

---

## Post-Deploy Checklist

- [ ] Agent registered: `openclaw agents list` shows the agent
- [ ] Status file initialized: `cat ~/Dropbox/openclaw-backup/agents/{agent-name}.status.md`
- [ ] All crons registered: `openclaw cron list` (filter visually by agent)
- [ ] SOUL.md in workspace
- [ ] IDENTITY.md in workspace
- [ ] TOOLS.md in workspace
- [ ] Exec allowlist configured: `openclaw approvals get`
- [ ] Cron fires and updates status file
- [ ] Telegram delivery working (message arrives)
- [ ] SOUL.md and IDENTITY.md are immutable: `lsattr ~/.openclaw/{agent-name}-workspace/SOUL.md` shows `i` flag
- [ ] Test suite passes: `bash ~/openclaw-tests/test-agent.sh {agent-name}`

---

## Rollback

```bash
# Remove all crons for the agent (by ID — get IDs from `openclaw cron list`)
openclaw cron rm <cron-id-1>
openclaw cron rm <cron-id-2>
# ... repeat for each cron

# Remove the agent
openclaw agents remove {agent-name}
```

The shared brain is untouched — agents only append, never destructively edit.

---

## OpenClaw CLI Quick Reference

Correct syntax as of OpenClaw 2026.4.1:

| Action | Command |
|--------|---------|
| Add agent | `openclaw agents add {name}` |
| List agents | `openclaw agents list` |
| Check health | `openclaw health` |
| Start gateway | `nohup openclaw gateway > /dev/null 2>&1 &` |
| Add channel account | `openclaw channels add --channel telegram --token {token} --account {agent-id} --name "{Display Name}"` |
| Bind agent to channel | `openclaw agents bind --agent {id} --bind telegram:{account-id}` |
| Pair Telegram bot | User sends `/start` to bot, then `openclaw pairing approve telegram {CODE}` |
| Add cron | `openclaw cron add --agent {id} --name "{name}" --cron "{expr}" --message "{text}" --to {chatId} --account {agent-id} --announce` |
| List crons | `openclaw cron list` |
| Run cron manually | `openclaw cron run {job-id}` |
| View cron history | `openclaw cron runs --id {job-id}` |
| Edit cron | `openclaw cron edit {job-id} --to {chatId} --announce` |
| Remove cron | `openclaw cron rm {job-id}` |
| Add exec allowlist | `openclaw approvals allowlist add --agent {id} "/usr/bin/*"` |
| View approvals | `openclaw approvals get` |
| Device pairing | `openclaw devices list` / `openclaw devices approve {request-id}` |

**Common pitfalls:**
- `cron` is singular, not `crons`
- Cron flags: `--cron` (not `--schedule`), `--message` (not `--prompt`)
- `--tools` flag on `cron add` causes API errors — omit it
- `cron run` and `cron rm` take the **job ID** (UUID), not the name
- `cron list` does not support `--agent` filtering — filter visually
- `openclaw agents config` does not exist — use `openclaw approvals allowlist` for exec permissions
- Always add `--to {chatId} --account {agent-id} --announce` to crons for Telegram delivery
- Each agent should have its own Telegram bot (create via @BotFather, add as channel account, bind to agent)
