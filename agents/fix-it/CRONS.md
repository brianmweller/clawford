# CRONS.md — Mr Fixit Cron Schedule

All times in UTC. Adjust for your local timezone.
Agent: fix-it
Workspace: .openclaw/fix-it-workspace/

---

## Heartbeat Check — Every 30 Minutes

**Schedule:** `*/30 * * * *`
**Command:** Read all `~/Dropbox/openclaw-backup/agents/*.status.md` files. Compare `last_heartbeat` to current time. Any agent with a heartbeat older than 90 minutes is flagged as potentially unhealthy.

**On success:** Write your own heartbeat to `agents/fix-it.status.md`.
**On failure (agent unhealthy):**
1. Log the agent name and last heartbeat time
2. Check if the agent's gateway process is running (`openclaw agents status {name}`)
3. If process is down, attempt restart: `openclaw agents restart {name}`
4. If restart fails, alert human via Telegram: "⚠️ {agent} unresponsive. Last heartbeat: {time}. Restart failed."

**Telegram output:** Only on failures or after repairs. Silent on success unless this is the first run of the day (06:00 UTC check doubles as morning status).

---

## Morning Status Report — Daily at 06:00 UTC

**Schedule:** `0 6 * * *`
**Command:** Compile a summary of all agent statuses, last validation result, any open alerts, and token usage estimates.

**Telegram output:** Always. Format:

```
🦊🔧 Morning Status — {date}

Agents:
  ✅ family-calendar — last heartbeat 12m ago
  ✅ meetings-coach — last heartbeat 8m ago
  ✅ shopping — last heartbeat 22m ago
  ✅ news-digest — last heartbeat 5m ago
  ✅ connector — last heartbeat 15m ago

Brain: validation passed (last run 02:00 UTC)
Dropbox: no conflicts
Files >500KB: none
Archive: next run Apr 1

Open alerts: none
```

---

## Brain Validation — Every 6 Hours

**Schedule:** `0 */6 * * *`
**Command:** `python3 ~/Dropbox/openclaw-backup/scripts/validate.py`

**On success:** Log result to status file. No Telegram message.
**On failure:**
1. Log specific validation errors
2. Categorize: critical (missing dirs, corrupted structure) vs. minor (formatting, missing optional fields)
3. Critical → Telegram alert immediately
4. Minor → Include in next morning status report

---

## Dropbox Conflict Scan — Every 2 Hours

**Schedule:** `0 */2 * * *`
**Command:** `find ~/Dropbox/openclaw-backup/ -name "*conflicted copy*" -type f 2>/dev/null`

**On no conflicts:** Log "clean" to status file. Silent.
**On conflict detected:**
1. Telegram alert immediately: "❌ Dropbox conflict: {filename}. Do NOT edit until resolved. Awaiting your instructions."
2. Log the conflict filename, timestamp, and which directory it's in
3. Do NOT attempt auto-merge. Wait for human instructions.

---

## File Size Monitor — Daily at 12:00 UTC

**Schedule:** `0 12 * * *`
**Command:** `find ~/Dropbox/openclaw-backup/ -type f -size +500k -exec ls -lh {} \;`

**On no large files:** Silent.
**On detection:**
1. Log filename and size
2. If it's a `tasks/queue.md` or facts file → note that the monthly archival should resolve it
3. If it's unexpected → Telegram alert: "⚠️ Large file: {filename} ({size}). Recommend archival or review."

---

## Monthly Archival — 1st of Each Month at 03:00 UTC

**Schedule:** `0 3 1 * *`
**Command:** Custom archival routine:

1. Create `~/Dropbox/openclaw-backup/archive/{YYYY-MM}/` if it doesn't exist
2. **Stale facts:** Scan all `facts/*.md` files. For each fact:
   - Calculate `effective_confidence = original_confidence × 0.5 ^ (days_elapsed / half_life)`
   - If effective_confidence < 0.2 AND recorded_at > 90 days ago → move to archive
   - For facts with `expires_at` in the past → move to archive
3. **Completed tasks:** Scan `tasks/queue.md`. For each task:
   - If status is `done` AND completed_at > 90 days ago → move to archive
   - If status is `cancelled` AND created_at > 90 days ago → move to archive
4. Write archive manifest: `archive/{YYYY-MM}/manifest.md` listing everything moved, with counts.

**Telegram output:** Always. "📦 Monthly archival complete. Moved {n} stale facts and {m} completed tasks to archive/{YYYY-MM}/."

**Safety:** This is a MOVE operation, not a delete. Archived files are preserved in the archive directory, which benefits from Dropbox backup. If something was archived incorrectly, it can be restored.

---

## Security Audit — Weekly on Sundays at 04:00 UTC

**Schedule:** `0 4 * * 0`
**Command:** `openclaw security audit --deep`

**Telegram output:** Always. Summary of findings.
- If clean: "✅ Weekly security audit passed. No issues."
- If findings: List each finding with severity. "⚠️ Security audit found {n} issues: {summary}. Run `openclaw security audit --fix`? Awaiting confirmation."

**NEVER auto-apply fixes.** Always wait for human confirmation.

---

## Update Check — Weekly on Wednesdays at 04:00 UTC

**Schedule:** `0 4 * * 3`
**Command:** `openclaw update`

**Telegram output:** Always.
- If up to date: "✅ OpenClaw is current. Version: {version}."
- If update available: "🔄 Update available: {current} → {new}. Changelog: {summary}. Apply? Awaiting confirmation."

**NEVER auto-apply updates.** Always wait for human confirmation.

---

## Cron Health Self-Check — Daily at 00:00 UTC

**Schedule:** `0 0 * * *`
**Command:** Verify all of Mr Fixit's own crons are registered and running:

1. `openclaw cron list` (filter for fix-it agent entries)
2. Compare against this file's expected schedule (9 crons total)
3. If any cron is missing or misconfigured, attempt self-repair
4. If self-repair fails, Telegram alert

This is the "who watches the watchman" cron. If this one breaks, the human will notice because the morning status report stops arriving.

---

## Summary Table

| Cron | Frequency | Telegram | Auto-action |
|------|-----------|----------|-------------|
| Heartbeat check | Every 30 min | On failure only | Restart attempt |
| Morning status | Daily 06:00 | Always | None (report only) |
| Brain validation | Every 6 hours | On failure only | None (log + alert) |
| Dropbox conflicts | Every 2 hours | On detection | None (alert only) |
| File size monitor | Daily 12:00 | On detection | None (alert only) |
| Monthly archival | 1st of month 03:00 | Always | Move stale/done entries |
| Security audit | Weekly Sun 04:00 | Always | None (await confirmation) |
| Update check | Weekly Wed 04:00 | Always | None (await confirmation) |
| Self-check | Daily 00:00 | On failure only | Self-repair attempt |
