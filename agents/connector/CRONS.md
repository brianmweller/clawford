# CRONS.md — Huckle Cat Cron Schedule

All times in UTC.
Agent: connector
Workspace: .openclaw/connector-workspace/

---

## Morning Relationship Nudge — Daily at 11:55 UTC (4:55 AM PT), deliver at 12:00 UTC (5:00 AM PT)

**Schedule:** `55 11 * * *`
**Command:** Scan people files, compute check-in status, deliver nudge at 5:00 AM PT sharp.

1. Run `python3 ~/.openclaw/connector-workspace/scripts/people-scan.py`
   - Reads all people files, loads cadence config, computes overdue/approaching
   - Outputs JSON with overdue, approaching, healthy arrays and summary
2. Read the JSON output.
3. Format the morning nudge:

```
🐱🤝 Relationship Check — {Weekday}, {Month} {Day}

👋 OVERDUE
  {name} ({relationship}) — {N} days since last contact
  via {preferred_channel}

  {name} ({relationship}) — {N} days since last contact
  via {preferred_channel}

⏳ APPROACHING
  {name} ({relationship}) — due in {N} days

🐱🤝 {overdue} overdue · {approaching} approaching · {total} tracked
```

If no overdue or approaching:
```
🐱🤝 Everyone's accounted for. No overdue check-ins today. 🐱🤝
```

4. Write formatted output to `cache/morning-nudge.txt`
5. Run `python3 ~/.openclaw/connector-workspace/scripts/timed-deliver.py cache/morning-nudge.txt --token-env CONNECTOR_BOT_TOKEN`
6. Update your status file.

**On success:** Update status file with heartbeat, overdue count, approaching count.
**On failure (file error or script error):**
1. Write the error to cache/morning-nudge.txt
2. Deliver error message: "🐱🤝 Couldn't check the address book this morning — something's off. Need you to take a look."
3. Update status file with error.

**Telegram output:** Always (via timed-deliver.py at :00).

---

## Notes Triage — Twice Daily (08:00 and 20:00 UTC)

**Schedule:** `0 8,20 * * *`
**Command:** Read inbox, present untriaged notes for categorization.

1. Run `python3 ~/.openclaw/connector-workspace/scripts/notes-triage.py`
   - Reads `inbox.md`, parses entries, filters for `triaged: false`
   - Outputs JSON with untriaged notes array
2. Read the JSON output.
3. If no untriaged notes: produce NO output. Silent.
4. If untriaged notes exist:
   a. Read `pending-triage.json` to skip notes already presented but not yet confirmed.
   b. For each new untriaged note, categorize it using your LLM reasoning:
      - `fact` — information about a person or situation
      - `commitment` — someone promised something, or Sam committed to something
      - `task` — something Sam needs to do
      - `shopping` — an item to buy (route to Hilda Hippo)
      - `unclear` — can't determine; ask Sam
   c. Present up to 10 notes on Telegram:

```
🐱🤝 Notes to triage — {count} new

📝 1. "{note content}"
   → fact about {person} (situation)

📝 2. "{note content}"
   → commitment: Sam → {person} by {date}

📝 3. "{note content}"
   → task for Sam

/confirm to save · /dismiss N to skip item N
```

   d. If more than 10: show footer "...and {N} more. /triage more for the next batch."
   e. Record presented note IDs in `pending-triage.json` with timestamp.

**On `/confirm`:** For each confirmed note:
- fact → append to `~/Dropbox/openclaw-backup/facts/YYYY-MM.md` using fact schema
- commitment → append to `~/Dropbox/openclaw-backup/commitments/active.md` using commitment schema
- task → append to `~/Dropbox/openclaw-backup/tasks/queue.md` using task schema
- shopping → append to `~/Dropbox/openclaw-backup/notes/inbox.md` with tag `triaged_to: shopping`
- Mark the original note as `triaged: true` with `triaged_to: {destination}` in `inbox.md`

**On `/dismiss N`:** Remove item N from pending triage. Do not write to brain.

**Telegram output:** Only when untriaged notes exist. Silent otherwise.

---

## Heartbeat — Every 30 Minutes

**Schedule:** `*/30 * * * *`
**Command:** Update your own status file with current heartbeat timestamp.

Write current UTC time to `last_heartbeat` in `~/Dropbox/openclaw-backup/agents/connector.status.md`. Verify connector-config.json and pending-triage.json exist. Prune pending-triage.json entries older than 48 hours and cache files older than 14 days. Produce NO output if everything is normal.

**On success:** Silent.
**On failure:** Alert on Telegram: "❌ connector heartbeat issue: {description}"

**Telegram output:** Silent on success. Alert on failure only.

---

## Weekly Relationship Review — Sunday at 00:00 UTC (5:00 PM PT Saturday)

**Schedule:** `0 0 * * 0`
**Command:** Generate a weekly relationship review.

1. Run `python3 ~/.openclaw/connector-workspace/scripts/people-scan.py` to get current state.
2. Read `checkin-log.json` for this week's check-ins.
3. Count notes triaged this week (from `pending-triage.json` history or brain writes).
4. Compile summary:

```
🐱🤝 Weekly Review — Week of {Month} {Day}

📊 STATS
  {N} check-ins recorded this week
  {N} notes triaged
  {N} facts added to brain
  {N} people tracked

👋 STILL OVERDUE
  {name} ({relationship}) — {N} days overdue
  {name} ({relationship}) — {N} days overdue

🐱🤝
```

If no overdue: replace STILL OVERDUE section with "Everyone's accounted for."

5. Deliver to Telegram.

**Telegram output:** Always (Saturday evening).

---

## Summary Table

| Cron | Frequency | Telegram | Auto-action |
|------|-----------|----------|-------------|
| Morning nudge | Daily 11:55 UTC (deliver at 12:00) | Always | Scan people, timed-deliver |
| Notes triage | Twice daily (08:00, 20:00 UTC) | On untriaged found | Read inbox, present for /confirm |
| Heartbeat | Every 30 min | On failure only | Write heartbeat, prune stale data |
| Weekly review | Sunday 00:00 UTC | Always | Compile week summary |
