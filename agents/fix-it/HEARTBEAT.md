# HEARTBEAT.md — Mr Fixit's 30-Minute Checklist

This file is read by OpenClaw every 30 minutes during the heartbeat cycle.
Only include lightweight, fast checks here. Heavy operations go in crons.

## Heartbeat vs Morning Status — scope split

Heartbeat (this file, every 30 min): **lightweight, read-only, heartbeat-age only**.
Flag agents whose `last_heartbeat` is older than 90 minutes. Do NOT read
KNOWN_ISSUES.md. Do NOT classify by `status` field or `error_log` content. Do
NOT re-verify or invoke scripts. If something looks stale, ping Telegram with
just the heartbeat-age finding. Deeper classification happens in
morning-status.

Morning status (CRONS.md "Morning Status Report", daily 06:00 UTC):
**structured classification** with known-issue suppression, staleness
detection, per-alert verification, and the 4-bucket report format. See the
morning-status cron definition in deploy.sh for the full 5-step prompt.

The split exists because heartbeat runs every 30 min (48x/day) and must be
near-instant, while morning-status runs once and can take 60+ seconds to
read KNOWN_ISSUES.md, verify tokens, and cross-check state.

## Every Heartbeat (30 min)

1. **Update your status file** with the current timestamp:
   - Write `last_heartbeat` to `/home/node/Dropbox/openclaw-backup/agents/fix-it.status.md`

2. **Quick health scan:**
   - Read all `*.status.md` files in `/home/node/Dropbox/openclaw-backup/agents/`
   - Cross-reference with `openclaw agents list`
   - Only flag agents that are BOTH registered locally AND have a stale heartbeat (>90 min)
   - Ignore placeholder status files for undeployed agents
   - Ignore `main` (OpenClaw internal default, no status file expected)
   - Do NOT flag based on `status` field or `error_log` content — that's the morning-status job

3. **If all registered agents healthy:** Update status file silently. No output. No Telegram message.

4. **If a registered agent has a stale heartbeat:** Send a Telegram message naming the agent and the heartbeat age. Do not speculate about cause — the morning-status report does that.

## Do NOT do during heartbeat

- Do NOT read KNOWN_ISSUES.md (morning-status only)
- Do NOT classify agents as "degraded" based on error_log or status field (morning-status only)
- Do NOT run validate.py (separate 6-hour cron)
- Do NOT check for Dropbox conflicts (separate 2-hour cron)
- Do NOT check file sizes (daily cron)
- Do NOT run git operations (only when asked or on the push cron)
- Do NOT modify any config files
- Do NOT invoke Claude Code
