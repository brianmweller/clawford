# HEARTBEAT.md — Mr Fixit's 30-Minute Checklist

> **⚠️ RETIRED as of R3+R6 (2026-04-13).** The LLM-native `heartbeat-check`
> cron that this file described was replaced by the host-side
> `fleet-health` cron (`ops/scripts/fleet-health.py`, every 15 min), which
> calls each agent's `probe()` function directly via `probe-agent.py`. Fix-it's
> own probe now lives in `scripts/heartbeat.py::probe` and monitors
> `~/Dropbox/openclaw-backup/fleet-health.json` freshness — it no longer
> scrapes per-agent `.status.md` files. Cross-agent alerting is handled by
> `fleet-health.py`'s `summarize()`. This document is kept for historical
> context only; do not run the procedures below.

This file WAS read by OpenClaw every 30 minutes during the heartbeat cycle.
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

## Platform Heartbeat (`openclaw status --deep`) — not used

OpenClaw has a built-in platform heartbeat feature visible via `openclaw status --deep`.
Busytown agents do **not** use it. The platform view showing "heartbeats disabled" or
partial/stale data is **expected behavior**, not a failure.

Source of truth for agent health is the cron-based status file system:
`~/Dropbox/openclaw-backup/agents/*.status.md`, checked every 30 minutes by the
heartbeat crons below. Do not attempt to enable or reconcile the platform heartbeat —
it would duplicate signals and risk conflicting with the cron-based model.

## Every Heartbeat (30 min)

1. **Quick health scan:**
   - Read all `*.status.md` files in `/home/node/Dropbox/openclaw-backup/agents/`
   - Cross-reference with `openclaw agents list`
   - Only flag agents that are BOTH registered locally AND have a stale heartbeat (>90 min)
   - Ignore placeholder status files for undeployed agents
   - Ignore `main` (OpenClaw internal default, no status file expected)
   - Do NOT flag based on `status` field or `error_log` content — that's the morning-status job

2. **OVERWRITE your status file** (truncate-write, NOT append):
   - Write a fresh 7-line snapshot to `/home/node/Dropbox/openclaw-backup/agents/fix-it.status.md`
   - The file content is EXACTLY this template, nothing more:
     ```
     # Fix-It — Status

     - **last_heartbeat:** {now in YYYY-MM-DD HH:MM UTC}
     - **status:** {healthy | degraded}
     - **last_cron_run:** heartbeat-check at {now}
     - **last_cron_result:** {one sentence}
     - **error_log:** {none | this-run findings only}
     - **token_usage_today:** —
     ```
   - Use python `open(file, 'w').write(...)` to write. Never `>>`. Never temp files.
   - Do NOT use `$(cat ...)` or any command substitution in the write command.
   - Do NOT preserve any old content. Do NOT include past errors in error_log.

3. **If all registered agents healthy:** Overwrite is silent. No Telegram message.

4. **If a registered agent has a stale heartbeat:** Status field = degraded. Send a Telegram message naming the agent and the heartbeat age. Do not speculate about cause — morning-status does that.

## Why overwrite, not append

Before 2026-04-09, every heartbeat (and conflict-scan, brain-validation, file-size-monitor, cron-self-check) was appending a verbose paragraph to fix-it.status.md. By the morning of 2026-04-09 the file had grown to 271 KB and was being re-read by the LLM on every cron tick — burning input tokens for ancient history that the morning-status report didn't even use. The fix: only `heartbeat-check` writes, and it always overwrites with a fresh snapshot. The other fix-it crons stay silent on success and alert via Telegram on failure (see deploy.sh prompts).

## Do NOT do during heartbeat

- Do NOT read KNOWN_ISSUES.md (morning-status only)
- Do NOT classify agents as "degraded" based on error_log or status field (morning-status only)
- Do NOT run validate.py (separate 6-hour cron)
- Do NOT check for Dropbox conflicts (separate 2-hour cron)
- Do NOT check file sizes (daily cron)
- Do NOT run git operations (only when asked or on the push cron)
- Do NOT modify any config files
- Do NOT invoke Claude Code
