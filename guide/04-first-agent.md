# Chapter 4: First Agent — Deploying Mr Fixit

Your first agent will hit every obstacle. That's why Fix-It is first — it monitors everything else, and you learn the deployment process before anything important breaks.

---

## What Mr Fixit does

Mr Fixit is the IT admin of your agent network. It runs 10 scheduled
agent-side cron jobs, plus two host-side crons that watch the whole
fleet:

| Cron | Frequency | Telegram |
|------|-----------|----------|
| Brain validation | Every 6 hours | Silent on pass, alerts on failure |
| Dropbox conflict scan | Every 2 hours | Silent on clean, alerts on conflict |
| File size monitor | Daily 12:00 UTC | Silent on clean, alerts on large file |
| Monthly archival | 1st of month 03:00 UTC | Always (reports what was archived) |
| Security audit | Weekly Sunday 04:00 UTC | Always (reports findings) |
| Update check | Weekly Wednesday 04:00 UTC | Always (reports version) |
| Cron self-check | Daily 00:00 UTC | Silent on pass, alerts on missing crons |
| Obsidian briefing | Daily 12:10 UTC | Silent on success, alerts on failure |
| Workspace snapshot | Daily 03:30 UTC | Silent on success, alerts on failure |
| Probation end reminder | One-shot (date-gated) | Always (if fired) |

Routine checks use `--no-deliver` + `--failure-alert` so you only get
notified when something needs attention. Reports (monthly archival,
security, updates) always deliver.

### Fleet health is a host cron, not an agent cron

The per-agent `heartbeat-check` and LLM-driven `morning-status` crons
that earlier versions of this guide documented are **retired**. Fleet
health now runs as two direct host crons (no `docker exec`, no LLM
tokens, no agent workspace):

| Host cron | Schedule | What it does |
|---|---|---|
| `fleet-health` | `*/15 * * * *` | `ops/scripts/fleet-health-host.sh` invokes `ops/scripts/fleet-health.py`, which calls each agent's `probe()` via `ops/scripts/probe-agent.py` and writes `~/Dropbox/openclaw-backup/fleet-health.json`. If any agent reports non-`ok`, the wrapper pushes one aggregated Telegram alert. |
| `morning-status` | `30 10 * * *` | `ops/scripts/morning-status-host.sh` invokes `agents/fix-it/scripts/morning-status.py`, which reads `fleet-health.json` + `fix-it/KNOWN_ISSUES.md` and writes `cache/morning-brief-ready.txt` for `morning-fleet-deliver` to pick up at 12:00 UTC. |

Both are registered by `ops/scripts/install-host-cron.sh`. Mr Fixit's
own agent-side probe (`agents/fix-it/scripts/heartbeat.py::probe`)
only monitors `fleet-health.json` freshness — if `generated_at` is
more than 30 minutes old it alerts that the orchestrator itself is
broken. Per-agent `.status.md` files in
`~/Dropbox/openclaw-backup/agents/` are a legacy artifact; nothing
writes them on a schedule anymore. `fleet-health.json` is the
authoritative registry.

## Step 1: Create a Telegram bot

1. Open Telegram, message **@BotFather**
2. Send `/newbot`
3. Name: `Mr Fixit`
4. Username: `openclaw_fixit_bot` (must end in `bot`)
5. Copy the bot token — add it to your `.env` as `FIXIT_BOT_TOKEN`

Give the bot a fox avatar if you want to match its personality.

## Step 2: Commit locally, push, pull on the VPS

**Do not `scp` files directly.** The canonical flow is `git commit →
push → pull on VPS → run deploy.py`. The hardened deploy tool at
`agents/shared/deploy.py` enforces this: it refuses to run if the
source repo has uncommitted modifications or untracked files.

From your local Clawford repo:

```bash
git add agents/fix-it/
git commit -m "fix-it: deploy prep"
git push origin master
```

Then on the VPS:

```bash
ssh openclaw@{server_ip}
cd ~/repo && git pull
```

Make sure `~/openclaw/.env` has `TELEGRAM_CHAT_ID` and
`FIXIT_BOT_TOKEN` (set once, persists across deploys):

```bash
grep -E '^(TELEGRAM_CHAT_ID|FIXIT_BOT_TOKEN)=' ~/openclaw/.env
```

If either is missing, add them from your password manager (NOT from a
copy of the Clawford repo — `.env` is gitignored precisely so secrets
stay out of git).

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

## Step 5: Run the unified deploy tool

```bash
cd ~/repo && python3 agents/shared/deploy.py fix-it
```

The tool reads `agents/fix-it/manifest.json` and, guided by six
safeguards, performs:

1. **Backup** — tars the current workspace to
   `~/.openclaw/deploy-backups/fix-it-<ts>.tar.gz` AND mirrors it to
   `~/Dropbox/openclaw-backup/deploy-backups/`. (Safeguard 1.)
2. **Source-clean check** — refuses if `~/repo` has uncommitted or
   untracked state. (Safeguard 2. Override: `--allow-dirty`.)
3. **Drift check** — refuses if the workspace has changed since the
   last recorded deploy. (Safeguard 4. Override: `--accept-drift`.)
4. **Config files** — copies manifest-listed workspace files
   (SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md,
   MEMORY.md, CRONS.md), unlocking any chattr-immutable ones before
   write and re-locking afterwards.
5. **Scripts** — copies manifest-listed Python scripts under
   `scripts/` and chmods them 755.
6. **State files** — seeds `grocery-list.json`, `pending-actions.json`,
   etc. only if absent. Live accumulated data is preserved.
7. **Channel & binding** — ensures the Telegram bot token is
   registered and the agent is bound to it (idempotent).
8. **Approvals** — adds each manifest-listed exec allowlist pattern.
9. **Crons** — syncs the manifest's cron list with live state:
   `cron edit --message` for UPDATEs (preserves history), `cron add`
   for new. UPDATEs show a unified diff and prompt for confirmation
   unless `--yes-updates` is passed. (Safeguard 3.)
10. **Smoke test** — if the manifest has a `smoke_test` block and
    `--smoke-test` is passed, fires the test cron and auto-restores
    the pre-deploy backup if it fails. (Safeguard 6.)

> **WARNING:** OpenClaw auto-loads 8 workspace files at every session start. If AGENTS.md, USER.md, or HEARTBEAT.md are missing or generic, the agent won't know its rules, its human, or its recurring tasks. This was the root cause of Mr Fixit's repeated identity crises and config amnesia.

## Step 6: Pair the Telegram bot

1. Send `/start` to your Mr Fixit bot on Telegram
2. If a pairing code appears, approve it:

```bash
oc pairing approve telegram {CODE}
```

Some setups auto-pair from the backup — if the bot responds without a code, you're good.

## Step 7: Smoke test

Two things to verify: (a) fix-it's agent-side crons are registered
and firable, and (b) the host-side `fleet-health` cron reaches
fix-it's `probe()` and gets a healthy response.

**(a) Agent-side cron.** List the crons and fire one as a sanity check:

```bash
oc cron list
oc cron run {conflict-scan-uuid}
```

Pick `conflict-scan` (every 2 hours, silent on clean) or
`brain-validation` (every 6 hours, silent on pass) — both should
return quickly and produce no Telegram output on success.

**(b) Host-side fleet-health.** Fire the orchestrator directly and
read the result:

```bash
bash ~/repo/ops/scripts/fleet-health-host.sh
python3 -c "import json; d=json.load(open('/home/openclaw/Dropbox/openclaw-backup/fleet-health.json')); print(d['generated_at']); [print(f'  {k}: {v[\"status\"]}') for k,v in d['agents'].items()]"
```

`fleet-health.json` should have a fresh `generated_at` and show
`fix-it: ok` alongside every other deployed agent. If fix-it shows
`error` with an alert about `fleet-health.json missing`, the
orchestrator hasn't run yet — re-fire the wrapper and check
`~/.openclaw/logs/fleet-health-host.log` for the last run's exit
code and any Python traceback.

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
oc cron edit {cron-uuid} \
  --no-deliver \
  --failure-alert \
  --failure-alert-to {chatId} \
  --failure-alert-account-id {agent-id} \
  --failure-alert-channel telegram \
  --message "...If the check passes: produce NO output. If it fails: send me a Telegram message..."
```

Apply this to: `conflict-scan`, `brain-validation`, `file-size-monitor`, `cron-self-check`, `obsidian-briefing`, `workspace-snapshot`.

Keep these noisy (always deliver): `monthly-archival`, `security-audit`, `update-check`, `probation-end-reminder`.

Fleet-level alerting (agent degraded, `fleet-health.json` stale)
comes from the host-side `fleet-health` cron — not from an agent
cron — so there is nothing to `--no-deliver` for it. Its wrapper
(`fleet-health-host.sh`) pushes to Telegram only when
`summarize()` returns non-`ok`.

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
claude -p "analyze ~/Dropbox/openclaw-backup/fleet-health.json" \
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
- [ ] All 10 agent-side crons registered: `oc cron list` (brain-validation, conflict-scan, file-size-monitor, monthly-archival, security-audit, update-check, cron-self-check, obsidian-briefing, probation-end-reminder, workspace-snapshot)
- [ ] Host-side `fleet-health` cron installed: `crontab -l | grep fleet-health`
- [ ] `fleet-health.json` generated and fresh: `ls -la ~/Dropbox/openclaw-backup/fleet-health.json` shows a timestamp within the last 15 minutes
- [ ] fix-it's entry in `fleet-health.json` is `ok`
- [ ] All workspace files populated: SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md, CRONS.md, probation.md (no BOOTSTRAP.md)
- [ ] Exec allowlist configured: `oc approvals get`
- [ ] An agent-side cron (e.g. `conflict-scan`) fires cleanly
- [ ] Telegram message arrives from Mr Fixit bot on a failing-state test
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
