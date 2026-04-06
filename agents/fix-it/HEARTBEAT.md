# HEARTBEAT.md — Mr Fixit's 30-Minute Checklist

This file is read by OpenClaw every 30 minutes during the heartbeat cycle.
Only include lightweight, fast checks here. Heavy operations go in crons.

## Every Heartbeat (30 min)

1. **Update your status file** with the current timestamp:
   - Write `last_heartbeat` to `/home/node/Dropbox/openclaw-backup/agents/fix-it.status.md`

2. **Quick health scan:**
   - Read all `*.status.md` files in `/home/node/Dropbox/openclaw-backup/agents/`
   - Cross-reference with `openclaw agents list`
   - Only flag agents that are BOTH registered locally AND have a stale heartbeat (>90 min)
   - Ignore placeholder status files for undeployed agents
   - Ignore `main` (OpenClaw internal default, no status file expected)

3. **If all registered agents healthy:** Update status file silently. No output. No Telegram message.

4. **If a registered agent is unhealthy:** Send a Telegram message identifying which agent and what's wrong.

## Do NOT do during heartbeat

- Do NOT run validate.py (that's a separate 6-hour cron)
- Do NOT check for Dropbox conflicts (that's a separate 2-hour cron)
- Do NOT check file sizes (that's a daily cron)
- Do NOT run git operations (only when asked or on the push cron)
- Do NOT modify any config files
- Do NOT invoke Claude Code
