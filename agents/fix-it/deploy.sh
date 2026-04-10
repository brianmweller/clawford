#!/usr/bin/env bash
# Deploy Mr Fixit — Run this AFTER `openclaw agents add fix-it`
# Usage: bash /tmp/deploy-fixit.sh
#
# Prerequisites:
#   - Docker container running: cd ~/openclaw && docker compose up -d
#   - openclaw agents add fix-it (interactive onboarding completed inside container)
#   - Device pairing approved
#   - SOUL.md, IDENTITY.md, TOOLS.md in /tmp/
#   - .env with TELEGRAM_CHAT_ID and FIXIT_BOT_TOKEN in /tmp/ or ~/openclaw/

set -euo pipefail

# Load secrets from .env
if [ -f /tmp/.env ]; then
    source /tmp/.env
elif [ -f ~/openclaw/.env ]; then
    source ~/openclaw/.env
elif [ -f .env ]; then
    source .env
fi

BRAIN="$HOME/Dropbox/openclaw-backup"
WORKSPACE="$HOME/.openclaw/fix-it-workspace"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID in .env}"
FIXIT_BOT_TOKEN="${FIXIT_BOT_TOKEN:?Set FIXIT_BOT_TOKEN in .env}"
TELEGRAM_ACCOUNT="default"
COMPOSE_FILE="$HOME/openclaw/docker-compose.yml"

# OpenClaw CLI wrapper — runs through Docker
oc() {
    docker compose -f "$COMPOSE_FILE" exec -T openclaw-gateway openclaw "$@"
}

echo "============================================"
echo "  Mr Fixit — Deployment Script"
echo "  OpenClaw 2026.4.1 (Docker)"
echo "============================================"
echo ""

# ── Step 1: Install Configuration Files ──────────────────────

echo "Step 1: Installing config files..."

if [ ! -d "$WORKSPACE" ]; then
    echo "  Creating workspace: $WORKSPACE"
    mkdir -p "$WORKSPACE"
fi

for file in SOUL.md IDENTITY.md TOOLS.md; do
    if [ -f "/tmp/$file" ]; then
        cp "/tmp/$file" "$WORKSPACE/$file"
        echo "  Copied $file -> $WORKSPACE/$file"
    else
        echo "  WARNING: /tmp/$file not found — skipping"
    fi
done

echo ""

# ── Step 2: Create Status File ───────────────────────────────

echo "Step 2: Initializing status file..."

cat > "$BRAIN/agents/fix-it.status.md" << 'EOF'
# Fix-It — Status

- **last_heartbeat:** —
- **status:** initializing
- **last_cron_run:** —
- **last_cron_result:** —
- **error_log:** none
- **token_usage_today:** 0
EOF

echo "  Written: $BRAIN/agents/fix-it.status.md"
echo ""

# ── Step 3: Configure Telegram Channel + Binding ─────────────

echo "Step 3: Configuring Telegram channel + binding..."

oc channels add --channel telegram \
  --token "$FIXIT_BOT_TOKEN" \
  --account "$TELEGRAM_ACCOUNT" \
  --name "Mr Fixit" 2>/dev/null || true
echo "  Telegram account '$TELEGRAM_ACCOUNT' configured"

oc agents bind --agent fix-it --bind "telegram:$TELEGRAM_ACCOUNT" 2>/dev/null || true
echo "  Agent fix-it bound to telegram:$TELEGRAM_ACCOUNT"

echo ""
echo "  NOTE: You must /start the Mr Fixit bot on Telegram and approve pairing:"
echo "  docker compose -f ~/openclaw/docker-compose.yml exec openclaw-gateway openclaw pairing approve telegram <CODE>"
echo ""

# ── Step 4: Set Up Exec Approvals ────────────────────────────

echo "Step 4: Setting exec approvals..."

oc approvals allowlist add --agent fix-it "/usr/bin/*"
echo "  Added /usr/bin/* to allowlist"

oc approvals allowlist add --agent fix-it "/bin/*"
echo "  Added /bin/* to allowlist"

oc approvals allowlist add --agent fix-it "/usr/local/bin/*"
echo "  Added /usr/local/bin/* to allowlist"

oc approvals allowlist add --agent fix-it "python3 -"
echo "  Added python3 stdin to allowlist"

# ── Exec policy: trusted local automation ──
# Without this, crons fail with "exec denied: Cron runs cannot wait for
# interactive exec approval." The LLM generates compound shell commands
# (redirects, pipes, heredocs) that don't match simple allowlist patterns.
# For a private VPS running trusted agents, security=full + ask=off is the
# right posture — no human approval needed for exec calls.
oc config set tools.exec.security full
oc config set tools.exec.ask off
echo "  Set tools.exec.security=full, ask=off"

echo ""

# ── Step 5: Register Crons ───────────────────────────────────

echo "Step 5: Registering 9 crons..."

# 1. Heartbeat — every 30 minutes (SILENT on all-clear)
# CRITICAL: heartbeat is the ONLY cron that writes fix-it.status.md, and it
# OVERWRITES (not appends). Other fix-it crons must NOT touch the file.
# This prevents the unbounded growth that hit 271KB by 2026-04-09.
HEARTBEAT_PROMPT='Heartbeat check. Do these steps in order.

STEP 1 — Check agent health.
For each file matching ~/Dropbox/openclaw-backup/agents/*.status.md, parse the last_heartbeat field. Accept BOTH of these timestamp formats as valid (normalize each to UTC before comparing):
  - `2026-04-09 21:31 UTC` (space-separated, informal — used by fix-it, shopping, meetings-coach, news-digest)
  - `2026-04-09T22:02:00Z` (ISO-8601 with Z suffix — used by family-calendar)
A value in either format is a VALID timestamp. Do not flag ISO-8601 Z values as "invalid timestamp" — they are correct. Cross-reference with `openclaw agents list` (run via exec). For each agent that is BOTH locally registered AND has a brain status file, check if last_heartbeat is older than 90 minutes from now. Ignore the `main` internal (no status file expected). Ignore placeholder status files for agents not in `openclaw agents list`.

STEP 2 — Decide overall status.
- If all registered+filed agents have fresh heartbeats: status = healthy.
- If any has stale heartbeat (>90 min): status = degraded. Note the unhealthy agent name.

STEP 3 — OVERWRITE the status file.
Write the following exact 7-line snapshot to ~/Dropbox/openclaw-backup/agents/fix-it.status.md, REPLACING all existing content. Use python3: `python3 -c "open(\"/home/node/Dropbox/openclaw-backup/agents/fix-it.status.md\",\"w\").write(\"\"\"<content>\"\"\")"` — this is atomic, no shell expansion issues. Do NOT use temp files. Do NOT use $(cat ...) or any command substitution. Do NOT write to /tmp first. Write DIRECTLY to the status file in one step.

# Fix-It — Status

- **last_heartbeat:** {now in YYYY-MM-DD HH:MM UTC}
- **status:** {healthy | degraded}
- **last_cron_run:** heartbeat-check at {now}
- **last_cron_result:** {one sentence — e.g., "all 5 agents within 90-min threshold" OR "{agent} stale, last heartbeat {time}"}
- **error_log:** {none | "{agent} unhealthy: {reason}" — only this run findings, do NOT include past errors}
- **token_usage_today:** —

STEP 4 — Telegram (only if degraded).
If status = degraded, send: "⚠️ {agent} unresponsive. Last heartbeat: {time}. Investigate."
If status = healthy, produce NO output. The status file overwrite is silent.

ABSOLUTE RULES:
1. The status file must be OVERWRITTEN (truncate-write), never appended. The file is a snapshot, not a log.
2. Do NOT include error history from past runs in error_log. Only THIS run findings.
3. Do NOT add any append-style entries below the snapshot. The file is exactly the 7-line block above (plus the header), nothing more.
4. Do NOT read or preserve any old content from the existing file. Overwrite blindly.
5. If the overwrite fails for any reason, send a Telegram alert: "❌ fix-it heartbeat: failed to overwrite status file: {error}".
6. NEVER write to a temp file then cat it. NEVER use $(cat ...) or $(...) substitution in the write command. Write the content DIRECTLY to fix-it.status.md in a single python or heredoc command.'

oc cron add \
  --agent fix-it \
  --name "heartbeat-check" \
  --cron "*/30 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "$HEARTBEAT_PROMPT"
echo "  [1/9] heartbeat-check (silent on all-clear, OVERWRITES status file)"

# 2. Morning status — daily at 06:00 UTC
# Structured 5-step prompt with known-issue suppression, staleness detection,
# and per-alert verification. Replaces the old free-form "note any open alerts"
# prompt which was freestyling classifications and re-reporting stale status.
MORNING_STATUS_PROMPT='Compile the morning status report. Follow these steps in order.

STEP 1 — Read the known-issues list FIRST.
Read ~/Dropbox/openclaw-backup/fix-it/KNOWN_ISSUES.md. Parse each entry: match pattern, expires date, reason, escalation conditions. Any entry past its expires date is IGNORED (treat as not-known). Keep the parsed list in mind for step 3.

STEP 2 — Gather raw state.
For each file matching ~/Dropbox/openclaw-backup/agents/*.status.md, record: agent name, the status field value, the last_heartbeat field value, the error_log field (first 500 chars), any agent-specific auth fields (google_auth, workflowy_auth, krisp_auth, amazon_session, costco_session), and the file mtime via stat -c %Y. Also run: python3 ~/Dropbox/openclaw-backup/scripts/validate.py and capture its result. Also run: find ~/Dropbox/openclaw-backup/ -name "*conflicted copy*" -type f and capture the list.

STEP 3 — Classify each REGISTERED agent into exactly one bucket.
Ignore placeholder status files for undeployed agents. Ignore the "main" internal. Use the following rules IN ORDER — first match wins:

(a) Heartbeat stale beyond 6 hours → 🚨 DOWN. The agent is not running.

(b) CRITICAL RULE: read the status FIELD, not the error_log history. The error_log may contain old entries from before a fix landed. If status is "ok" or "healthy", the agent IS healthy regardless of what error_log contains — classify as ✅ HEALTHY and move on. Do NOT derive "degraded" from error_log text when status says ok.

(c) status is "degraded" or an auth field shows an error: check the known-issues list. If a non-expired entry matches the text → ℹ️ KNOWN. Include the entry reason in parentheses.

(d) status is "degraded" and last_heartbeat is older than 6 hours: → ⚠️ STALE. The content has not been refreshed since the error was written; we cannot tell if it is still real. Do NOT treat as an open alert.

(e) Otherwise (fresh heartbeat, status=degraded, no known-issue match): do ONE verification action appropriate to the error before alerting. If the error mentions a token → check the token file mtime on disk via stat. If the error mentions a cron → run oc cron list and check the cron is still registered. If the error mentions exec approval → run oc config get tools.exec and verify security=full and ask=off. Include the verification output in your report. If verification confirms the problem → 🚨 OPEN ALERT. If verification shows the problem resolved → ✅ HEALTHY (note the status file is stale).

(f) Default → ✅ HEALTHY.

STEP 4 — Format the report EXACTLY as follows and send to Telegram:

🦊🔧 Morning Status — {YYYY-MM-DD HH:MM UTC}

Overall: {✅ all clear | ℹ️ {N} known | ⚠️ {N} stale | 🚨 {N} open alerts}

Agents:
  {bucket-emoji} {agent} — {one-line summary with heartbeat age}

Brain: {validation PASS/FAIL with counts}
Dropbox: {conflicts result}

🚨 Open alerts: (OMIT this entire section if none)
  - {agent}: {the issue}
    Verified: {exact action you took in step 3e and the result}
    Next: {restart / human needed / specific recommended command}

ℹ️ Known (pending human action): (OMIT this entire section if none)
  - {agent}: {issue} ({reason from KNOWN_ISSUES.md}) — expires {date}

⚠️ Stale (not re-verified): (OMIT this entire section if none)
  - {agent}: status file last updated {hours}h ago; content may be resolved — next agent run will refresh

STEP 5 — ABSOLUTE RULES (violating any of these is a bug in your report):
1. Never classify an agent as "degraded" based on error_log text alone when the status field is "ok".
2. Never report a known-issue as an open alert. The known-issues list is authoritative for suppression.
3. Never include an agent in the 🚨 Open alerts section without a "Verified:" line showing exactly what you did to re-check.
4. If in doubt between ⚠️ stale and 🚨 alert, prefer ⚠️ stale.
5. Authority reminder: you may restart agents and re-register crons. You may NOT provision credentials, edit other agents SOUL/IDENTITY (immutable), or touch deploy.sh files. Anything needing human decision goes in 🚨 Open alerts with "Next: human needed".
6. If KNOWN_ISSUES.md is missing or unreadable, note it in the report header and proceed without suppression — do NOT fail silently.'

oc cron add \
  --agent fix-it \
  --name "morning-status" \
  --cron "55 11 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "$MORNING_STATUS_PROMPT"
echo "  [2/9] morning-status (structured 5-step prompt with known-issue suppression, 11:55 UTC = 4:55 AM PT)"

# 3. Brain validation — every 6 hours (SILENT on pass)
oc cron add \
  --agent fix-it \
  --name "brain-validation" \
  --cron "0 */6 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: python3 ~/Dropbox/openclaw-backup/scripts/validate.py. If validation PASSES: produce NO output and DO NOT touch fix-it.status.md (the heartbeat-check cron is the only writer). If validation FAILS: send me a Telegram message with the specific failures immediately."
echo "  [3/9] brain-validation (silent on pass, does NOT touch status file)"

# 4. Dropbox conflict scan — every 2 hours (SILENT on clean)
oc cron add \
  --agent fix-it \
  --name "conflict-scan" \
  --cron "0 */2 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: find ~/Dropbox/openclaw-backup/ -name '*conflicted copy*' -type f. If NO conflicts found: produce NO output and DO NOT touch fix-it.status.md (the heartbeat-check cron is the only writer). If conflicts ARE found: send me a Telegram message with the filenames. Do NOT attempt to merge."
echo "  [4/9] conflict-scan (silent on clean, does NOT touch status file)"

# 5. File size monitor — daily at 12:00 UTC (SILENT on clean)
oc cron add \
  --agent fix-it \
  --name "file-size-monitor" \
  --cron "0 12 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: find ~/Dropbox/openclaw-backup/ -type f -size +500k. If NO large files found: produce NO output and DO NOT touch fix-it.status.md (the heartbeat-check cron is the only writer). If large files ARE found: send me a Telegram message with the filenames and sizes."
echo "  [5/9] file-size-monitor (silent on clean, does NOT touch status file)"

# 6. Monthly archival — 1st of each month at 03:00 UTC
oc cron add \
  --agent fix-it \
  --name "monthly-archival" \
  --cron "0 3 1 * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Run monthly archival. Create ~/Dropbox/openclaw-backup/archive/YYYY-MM/ for current month. Scan ALL files matching ~/Dropbox/openclaw-backup/facts/*.md (every monthly file, not just the current month). For each fact entry, calculate effective_confidence = original_confidence * 0.5^(days_since_recorded / half_life) using category half-lives: identity=never, established=365, situation=90, preference=180, plan=30, logistics=7, rumor=14. Move facts with effective_confidence < 0.2 AND recorded > 90 days ago to the archive. Also scan tasks/queue.md and move tasks with status done and completed > 90 days ago. Write an archive manifest listing everything moved. Report results on Telegram."
echo "  [6/9] monthly-archival"

# 7. Security audit — weekly Sunday 04:00 UTC
oc cron add \
  --agent fix-it \
  --name "security-audit" \
  --cron "0 4 * * 0" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Run openclaw security audit --deep RIGHT NOW — do not ask for permission, do not propose a plan, do not list what you intend to check. Just execute the command, read its output, and send a Telegram summary. Format: one line per finding with severity (critical/high/medium/low). If zero issues: send '✅ Security audit clean'. Do NOT run --fix. Do NOT run any checks beyond what openclaw security audit --deep returns (no port scans, no OS checks, no firewall inspection)."
echo "  [7/9] security-audit"

# 8. Update check — weekly Wednesday 04:00 UTC
oc cron add \
  --agent fix-it \
  --name "update-check" \
  --cron "0 4 * * 3" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Run: openclaw update. Report current version and whether an update is available on Telegram. Do NOT apply updates automatically. Wait for my confirmation."
echo "  [8/9] update-check"

# 9. Self-check — daily at midnight UTC (SILENT on pass)
oc cron add \
  --agent fix-it \
  --name "cron-self-check" \
  --cron "0 0 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: openclaw cron list. Verify all 9 fix-it crons are registered (heartbeat-check, morning-status, brain-validation, conflict-scan, file-size-monitor, monthly-archival, security-audit, update-check, cron-self-check). Filter the output visually for fix-it entries. If all 9 are present: produce NO output and DO NOT touch fix-it.status.md (the heartbeat-check cron is the only writer). If any are missing: attempt to re-register them and send me a Telegram message."
echo "  [9/9] cron-self-check (silent on pass, does NOT touch status file)"

echo ""

# ── Step 6: Security Hardening ───────────────────────────────

echo "Step 6: Security hardening..."

sudo chattr +i "$WORKSPACE/SOUL.md" 2>/dev/null && echo "  SOUL.md locked (immutable)" || echo "  WARNING: Could not lock SOUL.md (run: sudo chattr +i $WORKSPACE/SOUL.md)"
sudo chattr +i "$WORKSPACE/IDENTITY.md" 2>/dev/null && echo "  IDENTITY.md locked (immutable)" || echo "  WARNING: Could not lock IDENTITY.md (run: sudo chattr +i $WORKSPACE/IDENTITY.md)"

echo ""

# ── Verify ───────────────────────────────────────────────────

echo "============================================"
echo "  Verification"
echo "============================================"
echo ""

echo "Agent list:"
oc agents list
echo ""

echo "Crons registered:"
oc cron list
echo ""

echo "Exec approvals:"
oc approvals get
echo ""

echo "Status file:"
cat "$BRAIN/agents/fix-it.status.md"
echo ""

echo "Workspace:"
ls -la "$WORKSPACE/"
echo ""

echo "Immutable files:"
lsattr "$WORKSPACE/SOUL.md" "$WORKSPACE/IDENTITY.md" 2>/dev/null || echo "  (lsattr not available)"
echo ""

echo "============================================"
echo "  Deployment complete!"
echo ""
echo "  Smoke test (use IDs from cron list above):"
echo "  oc() { docker compose -f ~/openclaw/docker-compose.yml exec -T openclaw-gateway openclaw \"\$@\"; }"
echo "  oc cron run <heartbeat-check-id>"
echo "  oc cron run <brain-validation-id>"
echo "  oc cron run <morning-status-id>"
echo "  Send Telegram: 'Status check — report all systems.'"
echo ""
echo "  Run test suite:"
echo "  bash ~/openclaw-tests/test-agent.sh fix-it"
echo "============================================"
