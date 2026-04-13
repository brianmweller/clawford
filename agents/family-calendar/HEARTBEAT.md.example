# HEARTBEAT.md — 30-Minute Cycle

Every 30 minutes, run this lightweight checklist.

## Steps

1. **Update status file.** Write the current UTC timestamp to `last_heartbeat` in `~/Dropbox/openclaw-backup/agents/family-calendar.status.md`.

2. **Quick health check.** Verify:
   - `calendar-config.json` exists and is valid JSON
   - `token.json` exists (do NOT read or log its contents)
   - `sent-reminders.json` exists and is valid JSON

3. **Prune reminders.** If `sent-reminders.json` has entries older than 48 hours, remove them.

## Output

- **All healthy:** Update status file silently. Produce NO output. Do not send any Telegram message.
- **Any issue found:** Send a Telegram message describing what's wrong.
