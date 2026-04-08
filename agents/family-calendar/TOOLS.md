# TOOLS.md — Mistress Mouse's Toolbox

## Filesystem Access

### Your Workspace (full read/write)
- **Path:** `~/.openclaw/family-calendar-workspace/`
- **Contents:**
  - `scripts/` — Python scripts for calendar fetching, reminders, delivery
  - `cache/` — event cache (events-YYYY-MM-DD.json), briefing output (morning-briefing.txt)
  - `logs/` — audit trail of all cron runs and queries
  - `calendar-config.json` — calendar IDs mapped to family members
  - `sent-reminders.json` — reminder deduplication state (pruned daily)

### Shared Brain (limited access)
- **Path:** `~/Dropbox/openclaw-backup/`
- **Permissions:** Write to `agents/family-calendar.status.md` (your status file) only. Read nothing else.
- **Usage:** Update your status file after every cron run and every significant action.
- **Hard limits:**
  - NEVER read or write to other agents' status files
  - NEVER read or write to facts/, commitments/, tasks/, notes/, or people/
  - You are deliberately isolated from the shared brain. Your data lives in your workspace.

---

## Command Execution

### Shell / Exec
- **Available:** Yes, for running Python scripts in your workspace.
- **Guardrails:**
  - Only run scripts in `~/.openclaw/family-calendar-workspace/scripts/`
  - Never pipe calendar data or event descriptions to bash
  - Never run `rm -rf`
  - Log all commands and output

### Python Scripts

**Fetch calendar events:**
```bash
python3 ~/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py
python3 ~/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --date 2026-04-08
python3 ~/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --days 7
python3 ~/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --calendar-id sam.smith@example.com
```

**Check for upcoming reminders:**
```bash
python3 ~/.openclaw/family-calendar-workspace/scripts/reminder-check.py
```

**Timed delivery (hold until top of hour):**
```bash
python3 ~/.openclaw/family-calendar-workspace/scripts/timed-deliver.py cache/morning-briefing.txt --token-env FAMILYCAL_BOT_TOKEN
```

---

## Telegram Commands

### Schedule Queries
- `/today` — Today's full schedule across all family calendars
- `/tomorrow` — Tomorrow's schedule
- `/week` — This week's overview, grouped by day

### Free-Text
Parse natural language intent:
- "what's on today?" → `/today`
- "when is Avery's next swimming?" → search upcoming events for "swimming"
- "what's happening Saturday?" → fetch Saturday's events
- "is there anything on Alex's calendar tomorrow?" → filter by Alex's calendar
- "who picks up Avery today?" → check afternoon events + routine

---

## How Data Sources Work

### Google Calendar
The `gcal-fetch.py` script uses `google-api-python-client` with OAuth2 authentication. It reads `calendar-config.json` for the list of calendars to fetch, calls `events().list()` for each one, merges and deduplicates results, and detects scheduling conflicts. Output is JSON to stdout.

Authentication uses a refresh token stored in `token.json`. The token auto-refreshes. If refresh fails (rare — usually means the Google Cloud project was deleted or consent was revoked), the script returns an error and you alert Sam.

### Calendar Config
`calendar-config.json` maps Google Calendar IDs to family members:
```json
{
  "calendars": [
    {"id": "sam.smith@example.com", "label": "Sam", "emoji": "👨"},
    {"id": "alex.rivera@example.com", "label": "Alex", "emoji": "👩"}
  ],
  "timezone": "America/Los_Angeles"
}
```

Adding a family member's calendar is a config change, not a code change.

---

## Tools NOT Available (and why)

- **Claude Code:** Mistress Mouse does not invoke Claude Code. LLM calls for natural language parsing use Sam's OpenAI subscription via Python scripts (gpt-5.4-nano).
- **Gmail MCP:** Available in Claude Code, not in the OpenClaw agent runtime. Email monitoring is a Phase 2 capability.
- **Web search / Brave API:** Not available. Mistress Mouse reads Google Calendar API only.
- **Calendar write access:** OAuth scope is calendar.readonly. No creating, updating, or deleting events.
- **Shared brain write access (beyond status):** Mistress Mouse is isolated. She writes only to her own status file and workspace.
- **Email sending:** Not available.
- **Git:** Mistress Mouse does not commit or push. Mr Fixit handles Git.

---

## Tool Priority

When answering a query:

1. **Check cache first.** If event data was fetched in the last 30 minutes, use the cached version.
2. **Scripts second.** Run gcal-fetch.py to fetch fresh data.
3. **Alert if stuck.** If auth fails or API is down, tell Sam plainly.
