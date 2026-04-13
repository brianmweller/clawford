# HEARTBEAT.md — 30-Minute Cycle

Every 30 minutes, run this lightweight checklist.

## Steps

1. **Update status file.** Write the current UTC timestamp to `last_heartbeat` in `~/Dropbox/openclaw-backup/agents/meetings-coach.status.md`.

2. **Quick health check.** Verify:
   - `meeting-config.json` exists and is valid JSON
   - `token.json` exists (do NOT read or log its contents)
   - `sent-alerts.json` exists and is valid JSON
   - `cache/` directory exists

3. **Prune stale data.** If `sent-alerts.json` has entries older than 48 hours, remove them. If `cache/` has prep files older than 14 days, remove them.

## Output

- **All healthy:** Update status file silently. Produce NO output. Do not send any Telegram message.
- **Any issue found:** Send a Telegram message describing what's wrong.
