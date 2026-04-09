# MEMORY.md — Persistent Knowledge

## Calendar

- Sam's professional calendar: sam.smith@example.com
- Calendar scope: Sam's meetings only. Family events (Alex, kids, Jamie) are Mistress Mouse's domain.

## Meeting Detection Heuristics

A "real meeting" has attendees OR a video conferencing link (Google Meet, Zoom, etc.). Events without either are likely:
- Focus time blocks
- Task reminders
- Personal appointments
- Lunch / break blocks

Skip titles matching: "Focus Time", "Lunch", "Block" (configurable in meeting-config.json).

## Integrations

- **Workflowy:** Meeting nodes organized by Year > Month > Date. Template: Pre/during > Agenda + Notes, Post-meeting > Takeaways.
- **Krisp:** Transcripts fetched via MCP OAuth API. Matched to meetings by date/title/attendee fuzzy scoring.
- **Shared Brain:** People files in `/people/`, facts in `/facts/YYYY-MM.md`, commitments in `/commitments/active.md`.

## Hard Constraints

- Git: commit to `agents/meetings-coach/` only, never push. Mr Fixit handles Git.
- Calendar is read-only. Mistress Mouse handles writes.
- LLM calls: you (the agent) do all reasoning directly. Scripts do I/O only — no external LLM API calls.
- Brain writes require Sam's `/confirm` before executing.
- All commitment IDs use format: `meetings-coach-YYYY-MM-DD-NNN`.
