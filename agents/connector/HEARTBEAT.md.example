# HEARTBEAT.md — 30-Minute Cycle

Every 30 minutes, run this lightweight checklist.

## Steps

1. **Update status file.** Write the current UTC timestamp to `last_heartbeat` in `~/Dropbox/openclaw-backup/agents/connector.status.md`.

2. **Quick health check.** Verify:
   - `connector-config.json` exists and is valid JSON
   - `pending-triage.json` exists and is valid JSON
   - `cache/` directory exists
   - `~/Dropbox/openclaw-backup/people/` directory has at least one `.md` file

3. **Prune stale data.** If `pending-triage.json` has entries older than 48 hours, remove them. If `cache/` has files older than 14 days, remove them.

## Output

- **All healthy:** Update status file silently. Produce NO output. Do not send any Telegram message.
- **Any issue found:** Send a Telegram message describing what's wrong.
