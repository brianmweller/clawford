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
1. Copies all workspace files to the agent's workspace:
   - **SOUL.md** — personality, boundaries, operating model
   - **IDENTITY.md** — name, emoji, tone
   - **TOOLS.md** — available tools and permissions
   - **AGENTS.md** — hard rules, role, config architecture, agent roster
   - **USER.md** — human's name, timezone, preferences
   - **HEARTBEAT.md** — 30-minute recurring checklist
   - **MEMORY.md** — persistent lessons (seeded, agent maintains over time)
2. Deletes BOOTSTRAP.md if present (generic onboarding, overrides identity)
3. Initializes the status file in the shared brain
4. Adds the agent's Telegram bot as the default channel account
5. Binds the agent to the default Telegram account
6. Configures exec approvals
7. Registers all cron jobs with Telegram delivery
8. Locks SOUL.md and IDENTITY.md with `chattr +i`
9. Prints verification output

> **WARNING:** OpenClaw auto-loads 8 workspace files at every session start. If AGENTS.md, USER.md, or HEARTBEAT.md are missing or generic, the agent won't know its rules, its human, or its recurring tasks. This was the root cause of Mr Fixit's repeated identity crises and config amnesia.

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

## Silent crons

Routine health checks shouldn't wake you up. Use `--no-deliver` + `--failure-alert` on crons where "all clear" is boring:

```bash
oc cron edit {heartbeat-id} \
  --no-deliver \
  --failure-alert \
  --failure-alert-to {chatId} \
  --failure-alert-account-id {agent-id} \
  --failure-alert-channel telegram \
  --message "...If ALL agents are healthy: update the status file silently and produce NO output. If any agent is unhealthy: send me a Telegram message..."
```

Apply this to: heartbeat-check, conflict-scan, brain-validation, file-size-monitor, cron-self-check.

Keep these noisy (always deliver): morning-status, monthly-archival, security-audit, update-check.

## Claude Code (via shell, NOT ACP)

Mr Fixit can invoke Claude Code for complex diagnostics and multi-file repairs. Claude Code runs as a **one-shot shell command** — not as an ACP session.

> **WARNING: Do NOT use ACP for Claude Code.** ACP creates persistent sessions that take over the Telegram channel. The agent's identity gets replaced by Claude, and the user ends up talking to the wrong LLM. Always use `claude -p` as a one-shot shell command.

**1. Bake Claude Code into the Docker image** (Dockerfile, after `USER node`):

```dockerfile
RUN mkdir -p /home/node/.claude/local/bin \
  && PLATFORM="linux-$(uname -m | sed 's/x86_64/x64/' | sed 's/aarch64/arm64/')" \
  && VERSION=$(curl -fsSL https://storage.googleapis.com/.../claude-code-releases/latest) \
  && curl -fsSL -o /home/node/.claude/local/bin/claude ".../releases/${VERSION}/${PLATFORM}/claude" \
  && chmod +x /home/node/.claude/local/bin/claude
ENV PATH="/home/node/.claude/local/bin:${PATH}"
```

**2. Authenticate Claude Code** with your subscription:

```bash
# On the host, generate a setup token:
claude setup-token

# Add to .env (on VPS):
echo 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oXXX' >> ~/openclaw/.env

# Add to docker-compose.yml environment:
CLAUDE_CODE_OAUTH_TOKEN: ${CLAUDE_CODE_OAUTH_TOKEN:-}

# Restart and verify:
docker compose up -d
docker compose exec -T openclaw-gateway claude auth status
# Should show: loggedIn: true, authMethod: oauth_token
```

**3. How the agent uses it:**

Always include `--add-dir ~/Dropbox/openclaw-backup/` — without it, Claude Code is sandboxed to the workspace and can't access the brain.

**Single-shot** (simple diagnostics):
```bash
claude -p "analyze ~/Dropbox/openclaw-backup/agents/fix-it.status.md" \
  --output-format text --add-dir ~/Dropbox/openclaw-backup/
```

**Multi-turn** (complex repairs — analyze, get human input, act):
```bash
# Turn 1: Analyze
SESSION_ID=$(uuidgen)
claude -p "analyze all agent status files" \
  --output-format text --add-dir ~/Dropbox/openclaw-backup/ \
  --session-id "$SESSION_ID"
# Agent reports findings to human, waits for direction

# Turn 2: Act (Claude remembers turn 1)
claude -p "fix the issues you found" \
  --output-format text --add-dir ~/Dropbox/openclaw-backup/ \
  --resume "$SESSION_ID"
```

The agent orchestrates: generates a session ID, runs Claude turn by turn, reports findings in its own voice between turns, and waits for human direction before proceeding. Claude Code never talks directly to the user.

**4. SOUL.md must explicitly say:**

```
Do NOT use ACP spawn or /acp commands. ACP hijacks the Telegram channel.
Always use `claude -p` with `--add-dir ~/Dropbox/openclaw-backup/`.
For multi-turn: use --session-id on turn 1, --resume on subsequent turns.
Report Claude's findings in your own voice. Never let Claude respond directly.
```

## Post-deploy checklist

- [ ] Agent registered: `oc agents list` shows fix-it
- [ ] Status file created with heartbeat timestamp
- [ ] All 9 crons registered: `oc cron list`
- [ ] All 8 workspace files populated: SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md, MEMORY.md (no BOOTSTRAP.md)
- [ ] Exec allowlist configured: `oc approvals get`
- [ ] Cron fires and updates status file
- [ ] Telegram message arrives from Mr Fixit bot
- [ ] SOUL.md and IDENTITY.md are immutable: `lsattr ~/.openclaw/fix-it-workspace/SOUL.md`
- [ ] Silent crons configured: routine checks don't notify on all-clear
- [ ] Claude Code working: agent can invoke `claude -p` and report findings in its own voice
- [ ] Claude Code authenticated: `docker compose exec -T openclaw-gateway claude auth status` shows `loggedIn: true`
- [ ] Agent knows who it is: "Who are you?" → answers as Mr Fixit, not generic assistant

---

## Troubleshooting: The Identity Crisis

This section documents problems we hit and solved. You may encounter them too.

### Agent responds as a generic assistant

**Symptom:** You message the bot and it says "I'm your assistant" or "I'm Claude" instead of identifying as Mr Fixit.

**Cause 1 — Wrong Telegram routing:** Messages go to the `main` agent (shared workspace with generic files) instead of `fix-it` (per-agent workspace with SOUL.md). Check: `oc sessions --active 5 --all-agents` — if the session shows `agent:main:main`, routing is broken.

**Fix:** The agent's Telegram bot must be the `default` account (with token in `TELEGRAM_BOT_TOKEN` env var). Named accounts show "not configured" and don't receive inbound messages. Update the binding to `accountId: "default"`.

**Cause 2 — BOOTSTRAP.md overrides identity:** The shared workspace may contain a `BOOTSTRAP.md` that tells the agent to figure out its identity from scratch. This overrides SOUL.md.

**Fix:** Delete `BOOTSTRAP.md` from the agent's workspace and the shared workspace. The agent reads SOUL.md and IDENTITY.md directly.

### ACP hijacks the Telegram channel

**Symptom:** After using Claude Code via ACP, ALL subsequent messages go to Claude (Sonnet) instead of Mr Fixit (GPT-5.4). The agent says "I'm Claude, an AI assistant made by Anthropic."

**Cause:** ACP creates persistent sessions bound to the Telegram thread via `~/.openclaw/telegram/thread-bindings-*.json`. `/stop`, `/new`, `/reset` do NOT unbind — they reset the ACP session but keep the thread binding.

**Fix:** 
1. Clear thread bindings: `echo '{"version":1,"bindings":[]}' > ~/.openclaw/telegram/thread-bindings-default.json`
2. Disable ACP: `oc config set acp.enabled false` (or `acp.dispatch.enabled false`)
3. Use `claude -p` (shell) instead of ACP for Claude Code invocation
4. Restart the gateway

### Claude Code can't access the brain

**Symptom:** Mr Fixit invokes Claude Code but it says "access blocked — outside allowed working directory."

**Fix:** Add `--add-dir ~/Dropbox/openclaw-backup/` to every `claude -p` invocation. Without it, Claude Code is sandboxed to the workspace directory.

### Claude Code session bleeds between requests

**Symptom:** Mr Fixit uses `--resume` from a previous request's session, giving Claude stale context for a new task.

**Fix:** SOUL.md must say: "Every new request gets a new session ID via `uuidgen`. Never reuse. Simple one-off tasks skip `--session-id` entirely."

---

Next: [Chapter 5 — Telegram Bots](05-telegram-bots.md)
