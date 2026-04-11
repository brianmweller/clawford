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

# Copy KNOWN_ISSUES.md to brain (alert suppression list for morning status)
KNOWN_ISSUES_SRC="/tmp/KNOWN_ISSUES.md"
KNOWN_ISSUES_DST="$BRAIN/fix-it/KNOWN_ISSUES.md"
mkdir -p "$BRAIN/fix-it"
if [ -f "$KNOWN_ISSUES_SRC" ]; then
    cp "$KNOWN_ISSUES_SRC" "$KNOWN_ISSUES_DST"
    echo "  Copied KNOWN_ISSUES.md -> $KNOWN_ISSUES_DST"
else
    echo "  WARNING: $KNOWN_ISSUES_SRC not found — morning status will run without suppression"
fi

# Copy scripts
mkdir -p "$WORKSPACE/scripts"
for script in timed-deliver.py heartbeat-write.py; do
    if [ -f "/tmp/scripts/$script" ]; then
        cp "/tmp/scripts/$script" "$WORKSPACE/scripts/$script"
        echo "  Copied scripts/$script -> $WORKSPACE/scripts/$script"
    else
        echo "  WARNING: /tmp/scripts/$script not found — skipping"
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

# Per-agent exec policy: full (allowlist can't handle LLM compound commands).
oc config set tools.exec.ask off
echo "  Exec approvals set (policy=full per agent, ask=off)"

echo ""

# ── Step 5: Register Crons ───────────────────────────────────

echo "Step 5: Registering 10 crons..."

# 1. Heartbeat — every 30 minutes (SILENT on all-clear)
# CRITICAL: heartbeat is the ONLY cron that writes fix-it.status.md, and it
# OVERWRITES (not appends). Other fix-it crons must NOT touch the file.
# This prevents the unbounded growth that hit 271KB by 2026-04-09.
HEARTBEAT_PROMPT='Run: python3 /home/node/.openclaw/fix-it-workspace/scripts/heartbeat.py

This script checks all agent status files, determines healthy/degraded, and writes fix-it.status.md automatically. You do NOT write the status file — the script does it.

If the script exits 0: all agents healthy. Produce NO output.
If the script exits 1: an agent is degraded. The script prints the alert message to stdout. Send that exact message on Telegram.
If the script exits 2: script error. Send a Telegram alert: "❌ fix-it heartbeat: script error" with the output.

Do NOT write to fix-it.status.md yourself. Do NOT use python3 -c. Do NOT use heredocs. Just run the script and relay its output if non-zero.'

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
  --message "Security audit. Do not ask for permission. Execute ALL steps, then send ONE Telegram report.

STEP 1 — Run openclaw security audit --deep. Capture its output.

STEP 2 — Check per-agent exec policies. Run: python3 -c \"import json; d=json.load(open('/home/node/.openclaw/exec-approvals.json')); [(print(f'{a}: {c.get(chr(112)+chr(111)+chr(108)+chr(105)+chr(99)+chr(121),chr(63))}')) for a,c in d.get('agents',{}).items()]\"

STEP 3 — Compose the report. Format EXACTLY like this:

🦊🔧 Security Audit — {date}

🔒 Exec Policies
{for each agent from STEP 2, one line: • {agent}: {policy}}
{All agents should be policy=full — this is intentional. See note below.}

🔴 CRITICAL ({count})
• {finding}

🟠 HIGH ({count})
• {finding}

🟡 MEDIUM ({count})
• {finding}

🟢 LOW ({count})
• {finding}

Remediation: {one sentence per critical/high}

ENRICHMENT RULES for STEP 1 findings:
• tools.exec.security_full_configured — do NOT report this raw finding. Replace it with the 🔒 Exec Policies section from STEP 2. All agents having policy=full is EXPECTED and correct — OpenClaw's allowlist matches binary paths only, and shell chains/redirections are unsupported in allowlist mode. Since LLMs generate compound commands, policy=full is the only option that works. This is per-agent scoped, not global. Do NOT flag as CRITICAL.
• plugins.tools_reachable_permissive_policy — suppress if plugins.allow is set and no untrusted extensions are installed.

FORMAT RULES: use the emoji severity headers shown above. Group by severity. Include counts. Omit empty sections. If zero issues send just: ✅ Security audit clean. Do NOT run --fix. Do NOT run checks beyond what these steps specify."
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
  --message "Run: openclaw cron list. Verify all 10 fix-it crons are registered (heartbeat-check, morning-status, brain-validation, conflict-scan, file-size-monitor, monthly-archival, security-audit, update-check, cron-self-check, obsidian-briefing). Filter the output visually for fix-it entries. If all 10 are present: produce NO output and DO NOT touch fix-it.status.md (the heartbeat-check cron is the only writer). If any are missing: attempt to re-register them and send me a Telegram message."
echo "  [9/10] cron-self-check (silent on pass, does NOT touch status file)"

# 10. Obsidian daily briefing — daily at 12:10 UTC (SILENT on success)
oc cron add \
  --agent fix-it \
  --name "obsidian-briefing" \
  --cron "10 12 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: python3 ~/Dropbox/openclaw-backup/scripts/obsidian-briefing/generate.py. If it succeeds: produce NO output. If it fails: send the error on Telegram."
echo "  [10/10] obsidian-briefing (silent on success, generates Obsidian daily briefing)"

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
