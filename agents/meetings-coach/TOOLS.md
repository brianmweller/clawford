# TOOLS.md — Sergeant Murphy's Toolbox

## Filesystem Access

### Your Workspace (full read/write)
- **Path:** `~/.openclaw/meetings-coach-workspace/`
- **Contents:**
  - `scripts/` — Python scripts for calendar fetching, meeting prep, transcript processing, Workflowy sync
  - `cache/` — event cache (events-YYYY-MM-DD.json), prep briefs (prep-{EVENT_ID}-{date}.json), debrief staging (pending-debrief-{EVENT_ID}.json), Workflowy node mappings (workflowy-links.json)
  - `logs/` — audit trail of all cron runs, brain writes, and queries
  - `meeting-config.json` — calendar IDs, timezone, meeting filters, Workflowy config
  - `sent-alerts.json` — pre-meeting alert deduplication state (pruned every 48 hours)
  - `processed-transcripts.json` — transcript processing deduplication

### Shared Brain (structured access)
- **Path:** `~/Dropbox/openclaw-backup/`
- **Permissions:**
  - `agents/meetings-coach.status.md` — Write (your status file)
  - `people/*.md` — Read and Write (look up attendees, create new person entries)
  - `facts/YYYY-MM.md` — Read and Write (read prior facts, write meeting-derived facts after /confirm)
  - `commitments/active.md` — Read and Write (read open items, write new commitments after /confirm)
  - `tasks/queue.md` — Write (append tasks from confirmed meeting action items)
  - `notes/inbox.md` — Read (check for meeting-relevant notes)
- **Hard limits:**
  - NEVER read or write to other agents' status files
  - NEVER write facts or commitments without Sam's `/confirm`
  - All writes include `source_agent: meetings-coach` and a timestamp
  - Commitment IDs use format: `meetings-coach-YYYY-MM-DD-NNN`

---

## Command Execution

### Shell / Exec
- **Available:** Yes, for running Python scripts in your workspace.
- **Guardrails:**
  - Only run scripts in `~/.openclaw/meetings-coach-workspace/scripts/`
  - Never pipe transcript content or event descriptions to bash
  - Never run `rm -rf`
  - Log all commands and output

### Python Scripts

**Fetch calendar events:**
```bash
python3 ~/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py
python3 ~/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py --date 2026-04-08
python3 ~/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py --days 7
```

**Generate meeting prep:**
```bash
python3 ~/.openclaw/meetings-coach-workspace/scripts/meeting-prep.py --meeting-id EVENT_ID
python3 ~/.openclaw/meetings-coach-workspace/scripts/meeting-prep.py --all-today
python3 ~/.openclaw/meetings-coach-workspace/scripts/meeting-prep.py --all-today --force
```

**Workflowy sync:**
```bash
python3 ~/.openclaw/meetings-coach-workspace/scripts/workflowy-sync.py --create-nodes
python3 ~/.openclaw/meetings-coach-workspace/scripts/workflowy-sync.py --push-bullets EVENT_ID
python3 ~/.openclaw/meetings-coach-workspace/scripts/workflowy-sync.py --read-agenda EVENT_ID
python3 ~/.openclaw/meetings-coach-workspace/scripts/workflowy-sync.py --sync
```

**Scan Krisp transcripts:**
```bash
python3 ~/.openclaw/meetings-coach-workspace/scripts/transcript-scan.py
python3 ~/.openclaw/meetings-coach-workspace/scripts/transcript-scan.py --match EVENT_ID
```

**Track commitments:**
```bash
python3 ~/.openclaw/meetings-coach-workspace/scripts/commitment-tracker.py
python3 ~/.openclaw/meetings-coach-workspace/scripts/commitment-tracker.py --person SLUG
python3 ~/.openclaw/meetings-coach-workspace/scripts/commitment-tracker.py --overdue-only
```

**Bootstrap person files:**
```bash
python3 ~/.openclaw/meetings-coach-workspace/scripts/person-bootstrap.py --from-events cache/events-2026-04-08.json
```

**Timed delivery (hold until top of hour):**
```bash
python3 ~/.openclaw/meetings-coach-workspace/scripts/timed-deliver.py cache/morning-meeting-brief.txt --token-env MEETINGS_BOT_TOKEN
```

---

## Telegram Commands

### Meeting Queries
- `/today` — Today's meetings with prep status and key talking points
- `/week` — This week's meeting overview, grouped by day
- `/prep [meeting name or index]` — Generate or refresh prep for a specific meeting
- `/debrief [meeting name or index]` — Force debrief processing for a meeting

### Commitment Management
- `/commitments` — List all open commitments from meetings
- `/confirm` — Approve extracted action items and write to shared brain
- `/dismiss N` — Dismiss extracted item N (don't commit to brain)

### Free-Text
Parse natural language intent:
- "what's my next meeting?" → find next upcoming, show prep
- "prep me for the 2pm" → generate/refresh prep for 2 PM meeting
- "what did I commit to with [person]?" → search commitments by person
- "anything from yesterday's standup?" → search recent debriefs
- "who's in the product sync?" → show attendees for that meeting

---

## How Data Sources Work

### Google Calendar
The `gcal-fetch.py` script uses `google-api-python-client` with OAuth2 authentication. It reads `meeting-config.json` for Sam's calendar ID, calls `events().list()`, filters to real meetings (has attendees or video link), and enriches with attendee details and conference data. Output is JSON to stdout.

Authentication uses a refresh token stored in `token.json`. The token auto-refreshes. If refresh fails, alert Sam.

### Workflowy
The `workflowy-sync.py` script uses the Workflowy REST API (`https://workflowy.com/api/v1`) with bearer token authentication. It creates meeting nodes in a Year > Month > Date hierarchy, reads existing agendas, and pushes AI-generated bullets to Agenda sections. API key stored in `WORKFLOWY_API_KEY` env var.

### Krisp Transcripts
The `transcript-scan.py` script fetches transcripts via Krisp's MCP OAuth 2.1 API (`https://mcp.krisp.ai/mcp`). It matches transcripts to calendar events using fuzzy scoring (date proximity, title similarity, participant overlap). OAuth tokens stored in `cache/krisp-token.json`.

### Meeting Config
`meeting-config.json` configures the calendar, filters, and integrations:
```json
{
  "calendars": [{"id": "sam.smith@example.com", "label": "Sam (Work)"}],
  "timezone": "America/Los_Angeles",
  "meeting_filters": {"skip_titles": ["Focus Time", "Lunch", "Block"]},
  "workflowy": {"enabled": true, "ai_bullet_prefix": "[AI]"}
}
```

---

## Tools NOT Available (and why)

- **Claude Code:** Sergeant Murphy does not invoke Claude Code. LLM calls for meeting prep and transcript extraction use Sam's OpenAI subscription via Python scripts (gpt-5.4-nano).
- **Calendar write access:** Murphy is read-only on Google Calendar. Mistress Mouse owns calendar modifications.
- **Gmail access:** Not available. Murphy reads calendar and transcripts, not email.
- **Web search / Brave API:** Not available.
- **Git:** Sergeant Murphy does not commit or push. Mr Fixit handles Git.

---

## Tool Priority

When answering a query:

1. **Check cache first.** If event data or prep briefs were generated today, use the cached version.
2. **Scripts second.** Run gcal-fetch.py for fresh data, meeting-prep.py for new prep.
3. **Brain read third.** Look up people files, facts, and commitments for context.
4. **Alert if stuck.** If auth fails or API is down, tell Sam plainly.
