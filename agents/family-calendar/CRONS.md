# CRONS.md — Mistress Mouse Cron Schedule

All times in UTC.
Agent: family-calendar
Workspace: .openclaw/family-calendar-workspace/

---

## Morning Briefing — Daily at 11:55 UTC (4:55 AM PT), deliver at 12:00 UTC (5:00 AM PT)

**Schedule:** `55 11 * * *`
**Command:** Fetch all family calendars, format briefing, deliver at 5:00 AM PT sharp.

1. Run `python3 ~/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --days 2`
   - Fetches today + tomorrow from all configured calendars
   - Outputs JSON with events, conflicts, and per-calendar labels
2. Format the morning briefing:
   - Group today's events by time blocks: ☀️ MORNING, 🌤️ AFTERNOON, 🌙 EVENING
   - Use family member emoji (👨 👩 🧒 👶 🏠) for each event
   - Flag conflicts with ⚠️ and ask "who's on it?"
   - Include 📋 TOMORROW PREVIEW (one-line per notable event)
   - If today is a standard routine day with no extra events: "Standard [weekday] — no exceptions"
   - Note the day's pickup arrangement (Mon = Sam + swimming, Fri = Sam, other = Jamie)
3. Write formatted output to `cache/morning-briefing.txt`
4. Run `python3 ~/.openclaw/family-calendar-workspace/scripts/timed-deliver.py cache/morning-briefing.txt --token-env FAMILYCAL_BOT_TOKEN`
5. Update your status file.

**Morning briefing format:**

```
🐭📅 Family Day — {Weekday}, {Month} {Day}

☀️ MORNING
━━━━━━━━━━━━━━━
  {HH:MM}  {emoji} {Person} — {Event} ({Location})

🌤️ AFTERNOON
━━━━━━━━━━━━━━━
  {HH:MM}  {emoji} {Person} — {Event}

⚠️ CONFLICT: {description} — who's on it?

🌙 EVENING
━━━━━━━━━━━━━━━
  {HH:MM}  {emoji} {Person} — {Event}

📋 TOMORROW PREVIEW
  {one-liner per notable event or "Standard weekday"}

🐭 {count} events · Pickup: {who} · {any notes}
```

If no non-routine events today:
```
🐭📅 Family Day — {Weekday}, {Month} {Day}

Standard weekday — no exceptions.
Pickup: {Jamie / Sam} at 3:30 PM.

📋 TOMORROW PREVIEW
  {one-liner or "Standard weekday"}

🐭
```

**On success:** Update status file with heartbeat, event count, fetch duration.
**On failure (token expired or API error):**
1. Write the error to cache/morning-briefing.txt
2. Deliver error message: "🐭 Couldn't fetch calendars this morning — I've lost my connection. Need you to log me back in."
3. Update status file with error.

**Telegram output:** Always (via timed-deliver.py at :00).

---

## Reminder Check — Every 5 Minutes

**Schedule:** `*/5 * * * *`
**Command:** Poll for upcoming events and send reminders.

1. Run `python3 ~/.openclaw/family-calendar-workspace/scripts/reminder-check.py`
   - Checks all calendars for events in the next 60 minutes
   - Reads `sent-reminders.json` to skip already-sent reminders
   - Returns JSON: list of reminders to send, or empty array
2. Read the JSON output.
3. For each reminder: send a Telegram message:
   - "🐭 Heads up — {emoji} {Person} {Event} in {N} min ({Location})"
4. If the output is empty: produce NO output. Do not send any Telegram message.
5. Update your status file only if reminders were sent.

**Reminder tiers:**
- **60 min:** Events with location containing "airport", "doctor", "dentist", "hospital", or travel distance keywords
- **30 min:** All other standard events
- **15 min:** Events with summary containing "pickup", "pick up", "drop off", "dropoff", "school pickup"

**On success:** Silent if no reminders. Alert per reminder if any.
**On failure:** Alert on Telegram: "🐭 Reminder check failed — {error}"

**Telegram output:** Only when reminders are triggered. Silent otherwise.

---

## Heartbeat — Every 30 Minutes

**Schedule:** `*/30 * * * *`
**Command:** Update your own status file with current heartbeat timestamp.

1. Write current UTC time to `last_heartbeat` in `~/Dropbox/openclaw-backup/agents/family-calendar.status.md`.
2. Set `status` to `ok` if calendar-config.json and token.json exist, else `degraded`.
3. **Prune `error_log`**: remove any error lines older than 24 hours. If no recent errors remain, set `error_log: none`.
4. Verify `sent-reminders.json` exists and prune entries older than 48 hours.
5. Produce NO output if everything is normal.

Stale errors in the status file can cause Mr Fixit to raise false alerts, so pruning is critical.

**On success:** Silent.
**On failure:** Alert on Telegram: "❌ family-calendar heartbeat issue: {description}"

**Telegram output:** Silent on success. Alert on failure only.

---

## Activity Email Check — Every 2 Hours

**Schedule:** `15 */2 * * *`
**Command:** Check Gmail for emails from Example Preschool, Example Swim School, and Example Ballet Studio.

1. Run `python3 ~/.openclaw/family-calendar-workspace/scripts/activity-email-check.py`
   - Searches Gmail for emails from activity providers (last 48 hours)
   - Uses gpt-5.4-nano to parse email content for schedule-relevant items
   - Outputs JSON array of action items, cancellations, closures
2. Read the JSON output.
3. For each item that is NOT "none": send a Telegram message:
   - Closures/cancellations (urgent): "🐭 ⚠️ {source}: {summary}"
   - Action items (Room 3 needs X): "🐭 📋 {source}: {summary} (by {date})"
   - Events/FYI: "🐭 📌 {source}: {summary}"
4. If the output is empty or all items are "none": produce NO output.
5. Update your status file.

**Telegram output:** Only when actionable items are found. Silent otherwise.

---

## Gmail Invite Check — Every 3 Hours

**Schedule:** `30 */3 * * *`
**Command:** Check Gmail for calendar invites.

1. Run `python3 ~/.openclaw/family-calendar-workspace/scripts/gmail-invite-check.py`
   - Searches for ICS attachments and Google Calendar notifications
   - Outputs JSON array of new invites
2. For each invite: send a Telegram message:
   "🐭 New invite: {subject} on {date} from {organizer}. Accept?"
3. If no new invites: produce NO output.

**Telegram output:** Only when new invites are found. Silent otherwise.

---

## Weekly Overview — Sunday at 01:00 UTC (6 PM PT)

**Schedule:** `0 1 * * 0`
**Command:** Generate a week-ahead schedule overview.

1. Run `python3 ~/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --days 7`
2. Format as a day-by-day overview with key events, conflicts, and pickup arrangements.
3. Deliver to Telegram.

**Telegram output:** Always (Sunday evening).

---

## Summary Table

| Cron | Frequency | Telegram | Auto-action |
|------|-----------|----------|-------------|
| Morning briefing | Daily 11:55 UTC (deliver at 12:00) | Always | Fetch calendars, format, timed-deliver |
| Reminder check | Every 5 min | On events found | Poll calendars, send per-event reminders |
| Heartbeat | Every 30 min | On failure only | Write heartbeat, prune stale reminders |
| Activity email check | Every 2 hours | On items found | Parse Example Preschool/Example Swim School/Tutu emails via LLM |
| Gmail invite check | Every 3 hours | On invites found | Parse ICS attachments, surface invites |
| Weekly overview | Sunday 01:00 UTC | Always | Full week-ahead schedule |
