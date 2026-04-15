#!/usr/bin/env bash
# install-host-cron.sh — idempotent install of all openclaw host crons.
#
# Adds (or verifies) crontab entries for the openclaw user that run
# work moved off the LLM dispatch queue onto plain host cron. Safe to
# re-run. Each entry is marked with a unique "# <marker>" comment so
# subsequent runs detect pre-existing installs.
#
# Registered entries (direct wrappers):
#   */5 * * * *  costco-token-refresh-host.sh            (every 5 min — Costco JWT)
#   0 12 * * *   morning-fleet-deliver-host.sh           (5:00 AM PDT — morning briefs)
#   30 10 * * *  news-digest-morning-edition-host.sh     (fetch-and-rank + LLM compose)
#
# Registered entries (via generic script-contract-host.sh wrapper):
#   */15 * * * *  fleet-health                    → TELEGRAM_BOT_TOKEN  (R3 — replaces per-agent heartbeats)
#   0 */6 * * *   linkedin-keepalive              → NEWSDIGEST_BOT_TOKEN
#   */5 * * * *   family-calendar-reminder-check  → FAMILYCAL_BOT_TOKEN
#   */5 * * * *   news-digest-engagement-poll     → NEWSDIGEST_BOT_TOKEN
#   0 23 * * *    news-digest-preference-update   → NEWSDIGEST_BOT_TOKEN  (Phase 3b — pure-Python, calls llm.infer internally)
#   30 10 * * *   shopping-delivery-digest        → SHOPPING_BOT_TOKEN   (Phase 4 — full daily digest, writes cache/morning-brief-ready.txt for 5 AM PT fleet delivery; appends monthly S&S section on the 1st)
#   30 10 * * *   family-calendar-morning-briefing → FAMILYCAL_BOT_TOKEN (Phase 4 — daily brief for 5 AM PT fleet; appends WEEK AHEAD section on Mondays)
#   15 */2 * * *  family-calendar-activity-email-alert → FAMILYCAL_BOT_TOKEN (Phase 4 — LLM classifies preschool/swim/ballet emails)
#   30 */3 * * *  family-calendar-gmail-invite-alert   → FAMILYCAL_BOT_TOKEN (Phase 4 — format + send per-invite Telegram alerts)
#   45 */2 * * *  family-calendar-whatsapp-chat-alert  → FAMILYCAL_BOT_TOKEN (Phase 4 — LLM classifies WhatsApp messages via Baileys session store)
#   0 12 * * *    family-calendar-whatsapp-schedule-post → FAMILYCAL_BOT_TOKEN (Phase 4 — compose daily summary for the operator to manually forward)
#   0 */2 * * *   connector-gmessages-mine              → CONNECTOR_BOT_TOKEN  (Phase 4 — Camoufox scrape of Google Messages Web)
#   0 10 * * *    connector-daily-refresh               → CONNECTOR_BOT_TOKEN  (Phase 4 — runs 30 min before morning-relationship-nudge at 30 10 UTC so people-scan sees fresh last_interaction)
#   30 10 * * *   connector-morning-relationship-nudge  → CONNECTOR_BOT_TOKEN  (Phase 4 — daily nudge for 5 AM PT fleet; Monday recap folds weekly-review)
#   0 8,20 * * *  connector-notes-triage-alert          → CONNECTOR_BOT_TOKEN  (Phase 4 — LLM classifies inbox notes twice daily)
#   30 10 * * *   meetings-coach-morning-meeting-brief  → MEETINGS_BOT_TOKEN   (Phase 4 — daily brief for 5 AM PT fleet; Monday fold replaces weekly-review)
#   */30 * * * *  meetings-coach-pre-meeting-alert      → MEETINGS_BOT_TOKEN   (Phase 4 — 15-45 min lookahead, sent-alerts.json dedup)
#   15,45 * * * * meetings-coach-post-meeting-scan      → MEETINGS_BOT_TOKEN   (Phase 4 — Krisp transcript scan + LLM coaching; preserves 74c726c idempotency)
#   0 16 * * *    meetings-coach-commitment-follow-up   → MEETINGS_BOT_TOKEN   (Phase 4 — overdue/approaching alert at 9 AM PT)
#   0 */6 * * *   fix-it-brain-validation               → TELEGRAM_BOT_TOKEN   (Phase 4 — wraps validate.py, alerts on schema failures)
#   0 */2 * * *   fix-it-conflict-scan                  → TELEGRAM_BOT_TOKEN   (Phase 4 — pure-Python Dropbox conflicted copy detector)
#   0 12 * * *    fix-it-file-size-monitor              → TELEGRAM_BOT_TOKEN   (Phase 4 — pure-Python large-file monitor)
#   0 4 * * 0     fix-it-security-audit-alert           → TELEGRAM_BOT_TOKEN   (Phase 4 — wraps security-audit.py, forwards report; degrades after Phase 6)
#   10 12 * * *   fix-it-obsidian-briefing-check        → TELEGRAM_BOT_TOKEN   (Phase 4 — wraps obsidian-briefing/generate.py)
#   30 3 * * *    fix-it-workspace-snapshot-check       → TELEGRAM_BOT_TOKEN   (Phase 4 — wraps workspace-snapshot.py)
#   0 0 * * *     fix-it-cron-self-check-host.sh        → TELEGRAM_BOT_TOKEN   (Phase 4 — DIRECT_ENTRIES; runs on host because it needs `crontab -l` and host-side install-host-cron.sh)
#   0 3 1 * *     fix-it-monthly-archival               → TELEGRAM_BOT_TOKEN   (Phase 4 — pure-Python confidence decay archival)
#   0 16 25 4 *   fix-it-probation-end-reminder         → TELEGRAM_BOT_TOKEN   (Phase 4 — annual one-shot, fires April 25)
# Retired in Phase 4 (no host cron):
#   fix-it:update-check  — `openclaw update` is meaningless after Phase 6 decommission
#
# Removed in R3 (replaced by fleet-health):
#   */30 * * * *  shopping-heartbeat              → covered by fleet-health
#   */30 * * * *  meetings-coach-heartbeat        → covered by fleet-health
#   */30 * * * *  fix-it-heartbeat-check          → covered by fleet-health
#
# Usage: ssh openclaw@198.51.100.42 "/home/openclaw/repo/ops/scripts/install-host-cron.sh"
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WRAPPER_DIR="$REPO_ROOT/ops/scripts"

# Disabled-agents mechanism. Operator writes one agent id per line into
# $HOME/.clawford/disabled-agents.txt (or override via DISABLED_AGENTS_FILE
# env var). Any DIRECT_ENTRIES marker or CONTRACT_ENTRIES logname that
# equals an entry in that file — or starts with "<entry>-" — is skipped
# on install AND evicted from the live crontab if present. Short/blank
# lines and `#` comments in the file are ignored.
#
# Matching rule: full agent id with hyphen boundary. "fix-it" disables
# "fix-it" and anything starting with "fix-it-", but NOT "fix-itchy".
# "fix" alone does NOT match "fix-it" — the operator must spell out
# the full agent id.
DISABLED_AGENTS_FILE="${DISABLED_AGENTS_FILE:-$HOME/.clawford/disabled-agents.txt}"
DISABLED_AGENTS=()
if [[ -f "$DISABLED_AGENTS_FILE" ]]; then
  while IFS= read -r raw; do
    # Strip `#` comment to end-of-line, then trim whitespace.
    stripped="${raw%%#*}"
    # shellcheck disable=SC2001  # sed is clearer than ${//} for this
    stripped=$(echo "$stripped" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    [[ -n "$stripped" ]] && DISABLED_AGENTS+=("$stripped")
  done < "$DISABLED_AGENTS_FILE"
fi

# Returns 0 (success) if `$1` is owned by a disabled agent, 1 otherwise.
# Exact match or prefix-with-hyphen match only — see the file-format
# comment above.
is_disabled_entry() {
  local name="$1"
  local agent
  for agent in "${DISABLED_AGENTS[@]}"; do
    if [[ "$name" == "$agent" ]] || [[ "$name" == "$agent-"* ]]; then
      return 0
    fi
  done
  return 1
}

# Direct wrappers — each with its own dedicated script.
# Format: "<schedule>|<wrapper_basename>|<marker>"
DIRECT_ENTRIES=(
  "*/5 * * * *|costco-token-refresh-host.sh|# costco-token-refresh-host"
  "0 12 * * *|morning-fleet-deliver-host.sh|# morning-fleet-deliver-host"
  "*/15 * * * *|fleet-health-host.sh|# fleet-health-host"
  "30 10 * * *|morning-status-host.sh|# morning-status-host"
  "30 10 * * *|news-digest-morning-edition-host.sh|# news-digest-morning-edition-host"
  "0 0 * * *|fix-it-cron-self-check-host.sh|# fix-it-cron-self-check-host"
)

# Generic contract wrappers — use script-contract-host.sh with args.
# Format: "<schedule>|<logname>|<container-script-path>|<bot-token-env>|<timeout-s>"
# Marker is derived from logname: "# script-contract-<logname>"
CONTRACT_ENTRIES=(
  "0 */6 * * *|linkedin-keepalive|/home/openclaw/.clawford/news-digest-workspace/scripts/linkedin-keepalive.py|NEWSDIGEST_BOT_TOKEN|300"
  "*/5 * * * *|family-calendar-reminder-check|/home/openclaw/.clawford/family-calendar-workspace/scripts/reminder-check.py|FAMILYCAL_BOT_TOKEN|90"
  "*/5 * * * *|news-digest-engagement-poll|/home/openclaw/.clawford/news-digest-workspace/scripts/engagement-poller.py|NEWSDIGEST_BOT_TOKEN|60"
  "0 23 * * *|news-digest-preference-update|/home/openclaw/.clawford/news-digest-workspace/scripts/update-preferences.py|NEWSDIGEST_BOT_TOKEN|300"
  "30 10 * * *|shopping-delivery-digest|/home/openclaw/.clawford/shopping-workspace/scripts/delivery-digest.py|SHOPPING_BOT_TOKEN|900"
  "30 10 * * *|family-calendar-morning-briefing|/home/openclaw/.clawford/family-calendar-workspace/scripts/morning-briefing.py|FAMILYCAL_BOT_TOKEN|300"
  "15 */2 * * *|family-calendar-activity-email-alert|/home/openclaw/.clawford/family-calendar-workspace/scripts/activity-email-alert.py|FAMILYCAL_BOT_TOKEN|300"
  "30 */3 * * *|family-calendar-gmail-invite-alert|/home/openclaw/.clawford/family-calendar-workspace/scripts/gmail-invite-alert.py|FAMILYCAL_BOT_TOKEN|120"
  "45 */2 * * *|family-calendar-whatsapp-chat-alert|/home/openclaw/.clawford/family-calendar-workspace/scripts/whatsapp-chat-alert.py|FAMILYCAL_BOT_TOKEN|180"
  "0 12 * * *|family-calendar-whatsapp-schedule-post|/home/openclaw/.clawford/family-calendar-workspace/scripts/whatsapp-schedule-post.py|FAMILYCAL_BOT_TOKEN|120"
  "0 */2 * * *|connector-gmessages-mine|/home/openclaw/.clawford/connector-workspace/scripts/gmessages-mine.py|CONNECTOR_BOT_TOKEN|300"
  "0 10 * * *|connector-daily-refresh|/home/openclaw/.clawford/connector-workspace/scripts/daily-refresh.py|CONNECTOR_BOT_TOKEN|600"
  "30 10 * * *|connector-morning-relationship-nudge|/home/openclaw/.clawford/connector-workspace/scripts/morning-relationship-nudge.py|CONNECTOR_BOT_TOKEN|300"
  "0 8,20 * * *|connector-notes-triage-alert|/home/openclaw/.clawford/connector-workspace/scripts/notes-triage-alert.py|CONNECTOR_BOT_TOKEN|180"
  "30 10 * * *|meetings-coach-morning-meeting-brief|/home/openclaw/.clawford/meetings-coach-workspace/scripts/morning-meeting-brief.py|MEETINGS_BOT_TOKEN|300"
  "*/30 * * * *|meetings-coach-pre-meeting-alert|/home/openclaw/.clawford/meetings-coach-workspace/scripts/pre-meeting-alert.py|MEETINGS_BOT_TOKEN|180"
  "15,45 * * * *|meetings-coach-post-meeting-scan|/home/openclaw/.clawford/meetings-coach-workspace/scripts/post-meeting-scan.py|MEETINGS_BOT_TOKEN|300"
  "0 16 * * *|meetings-coach-commitment-follow-up|/home/openclaw/.clawford/meetings-coach-workspace/scripts/commitment-follow-up.py|MEETINGS_BOT_TOKEN|120"
  "0 */6 * * *|fix-it-brain-validation|/home/openclaw/.clawford/fix-it-workspace/scripts/brain-validation-check.py|TELEGRAM_BOT_TOKEN|120"
  "0 */2 * * *|fix-it-conflict-scan|/home/openclaw/.clawford/fix-it-workspace/scripts/conflict-scan.py|TELEGRAM_BOT_TOKEN|120"
  "0 12 * * *|fix-it-file-size-monitor|/home/openclaw/.clawford/fix-it-workspace/scripts/file-size-monitor.py|TELEGRAM_BOT_TOKEN|120"
  "0 4 * * 0|fix-it-security-audit-alert|/home/openclaw/.clawford/fix-it-workspace/scripts/security-audit-alert.py|TELEGRAM_BOT_TOKEN|180"
  "10 12 * * *|fix-it-obsidian-briefing-check|/home/openclaw/.clawford/fix-it-workspace/scripts/obsidian-briefing-check.py|TELEGRAM_BOT_TOKEN|240"
  "30 3 * * *|fix-it-workspace-snapshot-check|/home/openclaw/.clawford/fix-it-workspace/scripts/workspace-snapshot-check.py|TELEGRAM_BOT_TOKEN|600"
  "0 3 1 * *|fix-it-monthly-archival|/home/openclaw/.clawford/fix-it-workspace/scripts/monthly-archival.py|TELEGRAM_BOT_TOKEN|600"
  "0 16 25 4 *|fix-it-probation-end-reminder|/home/openclaw/.clawford/fix-it-workspace/scripts/probation-end-reminder.py|TELEGRAM_BOT_TOKEN|60"
)

# Markers for old entries to REMOVE on next install run. Used by the
# remove-stale step below — any crontab line containing one of these
# markers is dropped before adding the new CONTRACT_ENTRIES. This
# closes the install-host-cron.sh "yo-yo" gap from the R3 transition.
STALE_MARKERS=(
  "# script-contract-shopping-heartbeat"
  "# script-contract-meetings-coach-heartbeat"
  "# script-contract-fix-it-heartbeat-check"
  "# script-contract-fleet-health"
  # Phase 4: shopping was first wired as three separate crons; replaced
  # by a single 30 10 * * * shopping-delivery-digest entry that writes
  # cache/morning-brief-ready.txt (fleet delivers at 5 AM PT) and
  # appends a monthly S&S section on the 1st.
  "# script-contract-shopping-morning-delivery-brief"
  "# script-contract-shopping-subscribe-save-review"
  # Phase 4d: fix-it cron-self-check moved from CONTRACT (in-container)
  # to DIRECT (host wrapper) because it needs `crontab -l` and the host
  # repo, both unreachable from inside the openclaw gateway container.
  "# script-contract-fix-it-cron-self-check"
)

NEW_LINES=()
# Markers for entries whose existing crontab line no longer matches
# what we'd install (schedule / wrapper path / script path / token env /
# timeout changed since last run). These are evicted in the same sweep
# as STALE_MARKERS below so the new line can be added fresh.
DRIFT_MARKERS=()

# Snapshot the current crontab ONCE up front so every drift-detection
# comparison reads the same starting state. Re-reading inside the loops
# would mask drift if another run modified the crontab mid-invocation.
CURRENT_CRONTAB=$(crontab -l 2>/dev/null || true)

# Helper: return the first existing crontab line whose literal content
# contains the given marker, or empty string if no match.
#
# Uses pure-bash substring matching instead of `grep -F | head -1`
# because under `set -euo pipefail`, grep's exit-1-on-no-match
# propagates through the pipeline and aborts the whole script on every
# "not yet installed" entry. The while-read-here-string form avoids
# pipes entirely.
existing_line_for_marker() {
  local marker="$1"
  local line=""
  if [[ -n "$CURRENT_CRONTAB" ]]; then
    while IFS= read -r candidate; do
      if [[ "$candidate" == *"$marker"* ]]; then
        line="$candidate"
        break
      fi
    done <<< "$CURRENT_CRONTAB"
  fi
  echo "$line"
}

# Process direct entries
for entry in "${DIRECT_ENTRIES[@]}"; do
  IFS='|' read -r schedule wrapper_name marker <<< "$entry"
  # marker is literally "# <name>" — strip the "# " prefix for the
  # disabled-agent check, which compares against bare names.
  marker_name="${marker#\# }"

  if is_disabled_entry "$marker_name"; then
    existing=$(existing_line_for_marker "$marker")
    if [[ -n "$existing" ]]; then
      echo "[install-host-cron] disabled agent — evicting: $marker_name"
      DRIFT_MARKERS+=("$marker")
    else
      echo "[install-host-cron] disabled agent — skipping: $marker_name"
    fi
    continue
  fi

  wrapper="$WRAPPER_DIR/$wrapper_name"

  if [[ ! -f "$wrapper" ]]; then
    echo "[install-host-cron] MISSING: $wrapper" >&2
    exit 1
  fi
  chmod +x "$wrapper"

  desired_line="$schedule $wrapper $marker"
  existing=$(existing_line_for_marker "$marker")

  if [[ "$existing" == "$desired_line" ]]; then
    echo "[install-host-cron] already installed: $existing"
    continue
  fi

  if [[ -n "$existing" ]]; then
    echo "[install-host-cron] drift detected, evicting stale: $existing"
    DRIFT_MARKERS+=("$marker")
  fi
  NEW_LINES+=("$desired_line")
done

# Process generic contract entries
CONTRACT_WRAPPER="$WRAPPER_DIR/script-contract-host.sh"
if [[ ! -f "$CONTRACT_WRAPPER" ]]; then
  echo "[install-host-cron] MISSING: $CONTRACT_WRAPPER" >&2
  exit 1
fi
chmod +x "$CONTRACT_WRAPPER"

for entry in "${CONTRACT_ENTRIES[@]}"; do
  IFS='|' read -r schedule logname script_path token_env timeout_s <<< "$entry"
  marker="# script-contract-$logname"

  if is_disabled_entry "$logname"; then
    existing=$(existing_line_for_marker "$marker")
    if [[ -n "$existing" ]]; then
      echo "[install-host-cron] disabled agent — evicting: $logname"
      DRIFT_MARKERS+=("$marker")
    else
      echo "[install-host-cron] disabled agent — skipping: $logname"
    fi
    continue
  fi

  desired_line="$schedule $CONTRACT_WRAPPER $logname $script_path $token_env $timeout_s $marker"
  existing=$(existing_line_for_marker "$marker")

  if [[ "$existing" == "$desired_line" ]]; then
    echo "[install-host-cron] already installed: $existing"
    continue
  fi

  if [[ -n "$existing" ]]; then
    echo "[install-host-cron] drift detected, evicting stale: $existing"
    DRIFT_MARKERS+=("$marker")
  fi
  NEW_LINES+=("$desired_line")
done

# Drop any stale crontab lines whose marker matches STALE_MARKERS
# (long-retired entries from past migrations) OR DRIFT_MARKERS (entries
# whose schedule/path/etc. changed in this run). STALE handles the
# "yo-yo" case from R3; DRIFT handles schedule edits to live entries
# (e.g. Phase 4's shopping-delivery-digest 0 14 → 30 10 move).
#
# Without this pass the operator would have to `crontab -e` manually to
# remove the old lines — and the "already installed" check would mask
# the need.
STALE_REMOVED=0
ALL_EVICT_MARKERS=("${STALE_MARKERS[@]}" "${DRIFT_MARKERS[@]}")
if [[ ${#ALL_EVICT_MARKERS[@]} -gt 0 ]] && [[ -n "$CURRENT_CRONTAB" ]]; then
  FILTERED="$CURRENT_CRONTAB"
  for marker in "${ALL_EVICT_MARKERS[@]}"; do
    if echo "$FILTERED" | grep -Fq -- "$marker"; then
      # `|| true` guards against the set -e trap that fires when
      # grep -vF matches NO lines (e.g. we just evicted the only
      # entry in FILTERED, leaving an empty result + exit 1).
      FILTERED=$(echo "$FILTERED" | grep -vF -- "$marker" || true)
      STALE_REMOVED=$((STALE_REMOVED + 1))
      echo "[install-host-cron] removed stale entry: $marker"
    fi
  done
  if [[ "$STALE_REMOVED" -gt 0 ]]; then
    echo "$FILTERED" | crontab -
    # Refresh snapshot so the append step below sees the post-sweep state.
    CURRENT_CRONTAB=$(crontab -l 2>/dev/null || true)
  fi
fi

if [[ ${#NEW_LINES[@]} -eq 0 ]] && [[ "$STALE_REMOVED" -eq 0 ]]; then
  echo "[install-host-cron] all entries already registered — nothing to do"
  exit 0
fi

if [[ ${#NEW_LINES[@]} -eq 0 ]]; then
  echo "[install-host-cron] removed $STALE_REMOVED stale entries; no new lines to add"
  exit 0
fi

# Append new entries to the existing crontab (preserving any unrelated
# lines). Uses existing | new ordering so we never lose state on a
# partial write.
{
  crontab -l 2>/dev/null || true
  for line in "${NEW_LINES[@]}"; do
    echo "$line"
  done
} | crontab -

for line in "${NEW_LINES[@]}"; do
  echo "[install-host-cron] installed: $line"
done
