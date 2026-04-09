# CRONS.md — Sergeant Murphy Cron Schedule

All times in UTC.
Agent: meetings-coach
Workspace: .openclaw/meetings-coach-workspace/

---

## Morning Meeting Brief — Daily at 11:55 UTC (4:55 AM PT), deliver at 12:00 UTC (5:00 AM PT)

**Schedule:** `55 11 * * *`
**Command:** Fetch today's meetings, generate prep, sync Workflowy, deliver brief at 5:00 AM PT sharp.

1. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py --days 2`
   - Fetches today + tomorrow from Sam's professional calendar
   - Outputs JSON with events, attendees, conference links, is_real_meeting flags
2. Filter to real meetings only (events with attendees or video links).
3. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/person-bootstrap.py --from-events cache/events-YYYY-MM-DD.json`
   - Creates person files for any new attendees not yet in shared brain
4. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/workflowy-sync.py --create-nodes`
   - Creates Workflowy meeting nodes for events that don't have one yet
5. For each real meeting, run `python3 ~/.openclaw/meetings-coach-workspace/scripts/meeting-prep.py --meeting-id EVENT_ID`
   - Assembles context (attendees, facts, commitments) as JSON
   - Read the output for open commitments to include in the brief
6. Format the morning brief — factual schedule only, no talking points:

```
🐷🔍 Meeting Brief — {Weekday}, {Month} {Day}

{count} meeting(s) today

━━━━━━━━━━━━━━━
📋 {HH:MM} — {Meeting Title}
   👥 {Attendee names}
   📍 {Location / Meet link}
   📌 Open with {person}: {commitment summary}
━━━━━━━━━━━━━━━

📋 TOMORROW PREVIEW
  {one-liner per meeting or "No meetings"}

🐷🔍 {count} meetings · {open_items} open items
```

If no meetings today:
```
🐷🔍 All quiet on the calendar. No meetings today.

📋 TOMORROW PREVIEW
  {one-liner per meeting or "No meetings"}

🐷🔍
```

**Important:** The morning brief is factual only — schedule, attendees, location, open commitments. Do NOT generate or include talking points. Talking points belong in the pre-meeting alert (see below), and only when sourced from real Workflowy agenda items.

5. Write formatted output to `cache/morning-meeting-brief.txt`
6. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/timed-deliver.py cache/morning-meeting-brief.txt --token-env MEETINGS_BOT_TOKEN`
7. Update your status file.

**On success:** Update status file with heartbeat, meeting count, prep count.
**On failure (token expired or API error):**
1. Write the error to cache/morning-meeting-brief.txt
2. Deliver error message: "🐷🔍 Couldn't fetch calendar this morning — I've lost my connection. Need you to log me back in."
3. Update status file with error.

**Telegram output:** Always (via timed-deliver.py at :00).

---

## Pre-Meeting Alert — Every 30 Minutes

**Schedule:** `*/30 * * * *`
**Command:** Check for upcoming meetings and send prep alerts.

1. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py`
   - Fetches events for the next 2 hours
2. Filter to real meetings starting in 15–45 minutes.
3. Read `sent-alerts.json` to skip already-alerted meetings.
4. For meetings not yet alerted:
   a. Run `meeting-prep.py --meeting-id EVENT_ID` for context (attendees, commitments).
   b. Run `workflowy-sync.py --read-agenda EVENT_ID` to get real Workflowy agenda items.
   c. Send a Telegram message with factual content only:
      "🐷🔍 Heads up — meeting in {N} min\n📋 {Meeting Title}\n👥 {Attendees}\n📌 {Open commitments if any}"
      If Workflowy agenda items exist, include them under "📔 Agenda:" — these are Sam's own prep notes.
      Do NOT generate or invent talking points. Only surface what already exists.
5. Record alerted meeting IDs in `sent-alerts.json`.
6. If no meetings approaching: produce NO output.

**On success:** Silent if no alerts. Alert per meeting if any.
**On failure:** Alert on Telegram: "🐷🔍 Pre-meeting check failed — {error}"

**Telegram output:** Only when meetings are approaching. Silent otherwise.

---

## Post-Meeting Scan — Every 30 Minutes (offset)

**Schedule:** `15,45 * * * *`
**Command:** Check for recently-ended meetings and scan for Krisp transcripts.

1. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py` and identify meetings that ended in the last 60 minutes.
2. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/transcript-scan.py`
   - Fetches new transcripts from Krisp MCP API
   - Matches transcripts to recently-ended meetings by fuzzy scoring
   - Extracts action items, decisions, follow-ups via your own LLM reasoning
   - Stages results in `cache/pending-debrief-{EVENT_ID}.json`
3. For each matched transcript with extracted items:
   Send a Telegram message:
   ```
   🐷🔍 Debrief ready — {Meeting Title}

   🎯 ACTION ITEMS:
   1. {who}: {what} (by {when})
   2. {who}: {what}

   📌 DECISIONS:
   • {decision}

   🔄 FOLLOW-UPS:
   • {follow-up}

   /confirm to save to brain · /dismiss N to skip item N
   ```
4. If no transcripts or no matches: produce NO output.
5. Update status file.

**On `/confirm`:** Write commitments to `commitments/active.md`, facts to `facts/YYYY-MM.md`, following the shared brain schema.
**On `/dismiss N`:** Remove item N from the pending debrief. If all items dismissed, delete the pending file.

**Telegram output:** Only when new transcripts are processed. Silent otherwise.

---

## Commitment Follow-Up — Daily at 16:00 UTC (9:00 AM PT)

**Schedule:** `0 16 * * *`
**Command:** Check open commitments and flag overdue items.

1. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/commitment-tracker.py`
   - Reads `commitments/active.md` from shared brain
   - Filters for `source_agent: meetings-coach`
   - Flags overdue items (past `by_when`) and approaching items (within 48 hours)
2. If there are actionable items, send a Telegram message:
   ```
   🐷🔍 Open Items Check

   ⏰ OVERDUE:
   • {who} → {what} (due {date}, {N} days overdue)

   ⚠️ APPROACHING:
   • {who} → {what} (due {date}, {N} days left)

   📋 {total} open · {overdue} overdue · {approaching} approaching
   🐷🔍
   ```
3. If no overdue or approaching items: produce NO output.
4. Update status file.

**Telegram output:** Only when there are overdue or approaching items. Silent if all clear.

---

## Heartbeat — Every 30 Minutes

**Schedule:** `*/30 * * * *`
**Command:** Update your own status file with current heartbeat timestamp.

Write current UTC time to `last_heartbeat` in `~/Dropbox/openclaw-backup/agents/meetings-coach.status.md`. Verify meeting-config.json, token.json, and sent-alerts.json exist. Prune sent-alerts.json entries older than 48 hours and cache files older than 14 days. Produce NO output if everything is normal.

**On success:** Silent.
**On failure:** Alert on Telegram: "❌ meetings-coach heartbeat issue: {description}"

**Telegram output:** Silent on success. Alert on failure only.

---

## Weekly Review — Friday at 00:00 UTC (5:00 PM PT Thursday)

**Schedule:** `0 0 * * 5`
**Command:** Generate a weekly meeting review.

1. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py --days 7` (look back at this week)
2. Run `python3 ~/.openclaw/meetings-coach-workspace/scripts/commitment-tracker.py`
3. Compile summary:
   ```
   🐷🔍 Weekly Review — Week of {Month} {Day}

   📊 STATS:
   • {N} meetings held
   • {N} prep briefs generated
   • {N} transcripts processed
   • {N} commitments created · {N} resolved · {N} overdue

   📌 UNRESOLVED COMMITMENTS:
   • {who} → {what} (due {date})
   • {who} → {what} (overdue by {N} days)

   🐷🔍
   ```
4. Deliver to Telegram.

**Telegram output:** Always (Friday evening).

---

## Summary Table

| Cron | Frequency | Telegram | Auto-action |
|------|-----------|----------|-------------|
| Morning meeting brief | Daily 11:55 UTC (deliver at 12:00) | Always | Fetch calendar, prep meetings, Workflowy sync, timed-deliver |
| Pre-meeting alert | Every 30 min | On meetings approaching | Check for upcoming, generate/send prep |
| Post-meeting scan | Every 30 min (:15, :45) | On transcripts found | Scan Krisp, extract items, present for /confirm |
| Commitment follow-up | Daily 16:00 UTC | On items found | Check overdue, alert |
| Heartbeat | Every 30 min | On failure only | Write heartbeat, prune stale data |
| Weekly review | Friday 00:00 UTC | Always | Compile week summary |
