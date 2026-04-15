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

# Direct wrappers — each with its own dedicated script.
# Format: "<schedule>|<wrapper_basename>|<marker>"
DIRECT_ENTRIES=(
  "*/5 * * * *|costco-token-refresh-host.sh|# costco-token-refresh-host"
  "0 12 * * *|morning-fleet-deliver-host.sh|# morning-fleet-deliver-host"
  "*/15 * * * *|fleet-health-host.sh|# fleet-health-host"
  "30 10 * * *|morning-status-host.sh|# morning-status-host"
  "30 10 * * *|news-digest-morning-edition-host.sh|# news-digest-morning-edition-host"
)

# Generic contract wrappers — use script-contract-host.sh with args.
# Format: "<schedule>|<logname>|<container-script-path>|<bot-token-env>|<timeout-s>"
# Marker is derived from logname: "# script-contract-<logname>"
CONTRACT_ENTRIES=(
  "0 */6 * * *|linkedin-keepalive|/home/node/.openclaw/news-digest-workspace/scripts/linkedin-keepalive.py|NEWSDIGEST_BOT_TOKEN|300"
  "*/5 * * * *|family-calendar-reminder-check|/home/node/.openclaw/family-calendar-workspace/scripts/reminder-check.py|FAMILYCAL_BOT_TOKEN|90"
  "*/5 * * * *|news-digest-engagement-poll|/home/node/.openclaw/news-digest-workspace/scripts/engagement-poller.py|NEWSDIGEST_BOT_TOKEN|60"
  "0 23 * * *|news-digest-preference-update|/home/node/.openclaw/news-digest-workspace/scripts/update-preferences.py|NEWSDIGEST_BOT_TOKEN|300"
  "30 10 * * *|shopping-delivery-digest|/home/node/.openclaw/shopping-workspace/scripts/delivery-digest.py|SHOPPING_BOT_TOKEN|900"
  "30 10 * * *|family-calendar-morning-briefing|/home/node/.openclaw/family-calendar-workspace/scripts/morning-briefing.py|FAMILYCAL_BOT_TOKEN|300"
  "15 */2 * * *|family-calendar-activity-email-alert|/home/node/.openclaw/family-calendar-workspace/scripts/activity-email-alert.py|FAMILYCAL_BOT_TOKEN|300"
  "30 */3 * * *|family-calendar-gmail-invite-alert|/home/node/.openclaw/family-calendar-workspace/scripts/gmail-invite-alert.py|FAMILYCAL_BOT_TOKEN|120"
  "45 */2 * * *|family-calendar-whatsapp-chat-alert|/home/node/.openclaw/family-calendar-workspace/scripts/whatsapp-chat-alert.py|FAMILYCAL_BOT_TOKEN|180"
  "0 12 * * *|family-calendar-whatsapp-schedule-post|/home/node/.openclaw/family-calendar-workspace/scripts/whatsapp-schedule-post.py|FAMILYCAL_BOT_TOKEN|120"
  "0 */2 * * *|connector-gmessages-mine|/home/node/.openclaw/connector-workspace/scripts/gmessages-mine.py|CONNECTOR_BOT_TOKEN|300"
  "0 10 * * *|connector-daily-refresh|/home/node/.openclaw/connector-workspace/scripts/daily-refresh.py|CONNECTOR_BOT_TOKEN|600"
  "30 10 * * *|connector-morning-relationship-nudge|/home/node/.openclaw/connector-workspace/scripts/morning-relationship-nudge.py|CONNECTOR_BOT_TOKEN|300"
  "0 8,20 * * *|connector-notes-triage-alert|/home/node/.openclaw/connector-workspace/scripts/notes-triage-alert.py|CONNECTOR_BOT_TOKEN|180"
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
