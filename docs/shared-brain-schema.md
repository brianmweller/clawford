# OpenClaw Shared Brain Schema

**Version:** 0.1.0
**Date:** 2026-04-02
**Status:** Design spec — not yet implemented

---

## Overview

The shared brain is a lightweight, file-based knowledge layer that all OpenClaw agents can read from and write to. It stores structured information about people, facts, commitments, tasks, and agent health. It is designed to be reliable and simple — no database, no dependencies, just markdown files with strict conventions.

**Storage:** The shared brain lives inside a Dropbox-synced folder on the VPS. This provides automatic offsite backup (if the VPS dies, the brain survives), access from any device via Dropbox for manual inspection, and larger archival storage capacity. Agents read and write to the local filesystem path; Dropbox handles sync transparently.

**Future upgrade path:** As Flux matures, individual capabilities (e.g., people context, fact retrieval, meeting continuity) can replace their shared-brain equivalents one tool at a time. Agent SOUL files reference the *interface* they need, not the implementation.

---

## Directory Structure

```
~/Dropbox/openclaw-brain/
├── people/              # One file per person (identity, circles, contact info)
│   ├── _template.md     # Template for new person files
│   └── jane-doe.md      # Example: one file per person
├── facts/               # Append-only monthly fact logs
│   └── 2026-04.md       # One file per month
├── commitments/         # Active commitment tracking
│   └── active.md        # All open commitments
├── tasks/               # Unified task queue
│   └── queue.md         # All tasks, appended by any agent
├── notes/               # Manual inputs awaiting triage
│   └── inbox.md         # Raw notes from Workflowy/Post-its
└── agents/              # Per-agent status and reference data
    ├── family-calendar.status.md
    ├── family-calendar.rules.md
    ├── meetings-coach.status.md
    ├── shopping.status.md
    ├── shopping.rules.md
    ├── news-digest.status.md
    ├── fix-it.status.md
    └── connector.status.md
```

---

## Core Data Primitives

The shared brain has three core data types: **Facts**, **Commitments**, and **Notes**. Tasks are a fourth structure but are also mirrored to Google Calendar Tasks.

---

### 1. Facts

Facts are the primary knowledge unit. They are append-only, confidence-scored, and decay over time based on category.

**Schema:**

```markdown
- **id:** connector-2026-04-02-001
- **content:** Alice's daughter is starting kindergarten in September
- **subject:** alice-chen
- **source_type:** direct
- **source_detail:** Mentioned during coffee chat on 3/28
- **source_agent:** connector
- **confidence:** 0.9
- **category:** situation
- **recorded_at:** 2026-04-02T10:30:00Z
- **expires_at:** —
```

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| id | Yes | Unique. Format: `{agent}-{date}-{seq}` (e.g., `connector-2026-04-02-001`) |
| content | Yes | The fact itself, one sentence |
| subject | Yes | Person slug (links to `/people/{slug}.md`) or a topic tag |
| source_type | Yes | `direct` (told you), `observed` (inferred from email/calendar), `reported` (secondhand), `assumed` (agent guess) |
| source_detail | Yes | How/where this was learned |
| source_agent | Yes | Which OpenClaw agent recorded it |
| confidence | Yes | 0.0–1.0 at time of recording |
| category | Yes | Determines decay half-life (see table below) |
| recorded_at | Yes | ISO 8601 timestamp |
| expires_at | No | Hard expiry date, if applicable |

**Categories and half-lives:**

| Category | Half-Life | Examples |
|----------|-----------|---------|
| identity | Never decays | "Jane is my sister-in-law", "Bob's birthday is March 5" |
| established | 365 days | "Bob works at Google", "Alice has two kids" |
| situation | 90 days | "Uncle is going through chemo", "friend is job hunting" |
| preference | 180 days | "Wife prefers oat milk", "mom doesn't like spicy food" |
| plan | 30 days | "College friend is visiting in April" |
| logistics | 7 days | "Nanny can't come Thursday", "school half-day Friday" |
| rumor | 14 days | "Heard they might be moving" |

**Decay formula:**

```
effective_confidence = original_confidence × 0.5 ^ (days_elapsed / half_life)
```

A fact with effective confidence below **0.2** is flagged as **stale**. Stale facts are not deleted — agents can choose to re-verify, ignore, or archive them.

**Write conventions:**

- All writes are **appends** to the monthly file (`facts/YYYY-MM.md`)
- No agent ever edits or deletes another agent's facts
- Each fact is separated by a blank line and a `---` divider
- Facts with `expires_at` in the past are treated as stale regardless of decay

---

### 2. Commitments

Commitments track promises and obligations — things that resolve rather than decay.

**Schema:**

```markdown
- **id:** meetings-coach-2026-04-02-001
- **who:** Bob Martinez
- **to_whom:** me
- **what:** Send the revised proposal
- **by_when:** 2026-04-05
- **status:** open
- **source_detail:** Agreed during 1:1 on 4/2, per Krisp transcript
- **source_agent:** meetings-coach
- **created_at:** 2026-04-02T15:00:00Z
- **resolved_at:** —
- **resolution_note:** —
```

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| id | Yes | Unique. Format: `{agent}-{date}-{seq}` |
| who | Yes | Who made the commitment (person name or "me") |
| to_whom | Yes | Who it was made to (person name or "me") |
| what | Yes | What was committed |
| by_when | No | Deadline, if one exists |
| status | Yes | `open`, `completed`, `overdue`, `cancelled` |
| source_detail | Yes | Where this was captured |
| source_agent | Yes | Which agent recorded it |
| created_at | Yes | ISO 8601 timestamp |
| resolved_at | No | When status changed from open |
| resolution_note | No | How it was resolved |

**Status transitions:**

- `open` → `completed` (done)
- `open` → `overdue` (past `by_when`, auto-set by any reading agent)
- `open` → `cancelled` (no longer relevant)
- `overdue` → `completed` (done late)
- `overdue` → `cancelled`

**Write conventions:**

- All open commitments live in `commitments/active.md`
- When resolved, the commitment entry is updated in place (this is the one exception to append-only — but only the original recording agent or the user may resolve it)
- Any agent can *create* a commitment; only the source agent or the user can *resolve* it
- Overdue detection is passive: any agent reading a commitment checks `by_when` against current date

---

### 3. Notes

Notes are raw human inputs — the unstructured things you jot down in Workflowy or on a Post-it. They await triage by a designated agent.

**Schema:**

```markdown
- **id:** note-2026-04-02-001
- **content:** Need to call Dr. Patel about the baby's 9-month checkup
- **source:** workflowy
- **captured_at:** 2026-04-02T08:15:00Z
- **triaged:** false
- **triaged_to:** —
```

**Triage process:**

A designated agent (TBD — likely the Connector or Family Calendar) runs a periodic cron that reads untriaged notes and converts them into the appropriate type:
- A fact → appended to `/facts/`
- A commitment → appended to `/commitments/active.md`
- A task → appended to `/tasks/queue.md`
- A shopping item → routed to the Shopping agent
- Ambiguous → flagged for human clarification via Telegram

Once triaged, the note is marked `triaged: true` with `triaged_to` indicating where it went.

---

### 4. Tasks

Tasks are action items assigned to a person or an agent.

**Schema:**

```markdown
- **id:** family-calendar-2026-04-02-001
- **description:** Confirm nanny availability for Thursday
- **assignee:** me
- **status:** open
- **due_date:** 2026-04-03
- **source_agent:** family-calendar
- **created_at:** 2026-04-02T06:00:00Z
- **completed_at:** —
```

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| id | Yes | Unique. Format: `{agent}-{date}-{seq}` |
| description | Yes | What needs to be done |
| assignee | Yes | `me`, `wife`, or an agent name |
| status | Yes | `open`, `done`, `cancelled` |
| due_date | No | When it's due |
| source_agent | Yes | Which agent created it |
| created_at | Yes | ISO 8601 timestamp |
| completed_at | No | When marked done |

**Write conventions:**

- All tasks live in `tasks/queue.md`
- Tasks are appended with a timestamp and agent ID prefix
- Tasks assigned to "me" are also synced to Google Calendar Tasks by the creating agent
- Completed tasks stay in the file (marked `done`) for audit trail; archived monthly by Fix-It

---

## People Files

Each person gets a file in `/people/` with identity and circle information. Dynamic knowledge lives in `/facts/` — person files are relatively static.

**Template (`people/_template.md`):**

```markdown
# {Full Name}

- **slug:** {slug}
- **circles:** {comma-separated list}
- **relationship:** {relationship to user}
- **google_contact_id:** {if synced}
- **email:** {primary email}
- **phone:** {primary phone}
- **platforms:** {where you communicate: WhatsApp, WeChat, email, etc.}
- **preferred_channel:** {how they prefer to be reached}
- **last_interaction:** {date, updated by agents}
- **notes:** {any static notes}
```

**Circles:**

| Circle | Description | Check-in cadence |
|--------|-------------|-----------------|
| family-inner | Spouse, kids, nanny | Daily (via Family Calendar) |
| family-extended | In-laws, aunts, uncles | Weekly–monthly |
| friends-close | College friends, close friends | Monthly |
| professional-inner | Close colleagues, direct reports | Weekly |
| professional-outer | Broader network | Quarterly |
| holiday-card | Full holiday card list | Annually + life events |

A person can belong to multiple circles. The Connector uses circles to determine check-in cadence.

---

## Agent Status Files

Each agent writes its own status file to `/agents/{agent-name}.status.md`.

**Template:**

```markdown
# {Agent Name} — Status

- **last_heartbeat:** {ISO 8601}
- **status:** {healthy | degraded | error}
- **last_cron_run:** {ISO 8601}
- **last_cron_result:** {success | partial | failure}
- **error_log:** {last 5 errors, if any}
- **token_usage_today:** {approximate}
```

Fix-It reads these files on a cron to detect unhealthy agents.

Some agents also have a `.rules.md` file for reference data:
- `shopping.rules.md` — channel routing logic (Amazon vs. Costco vs. local)
- `family-calendar.rules.md` — pickup/dropoff defaults, school schedule, nanny schedule
- `news-digest.rules.md` — subscriptions, delivery schedule, topic preferences

---

## Global Conventions

1. **Append-only by default.** No agent overwrites another agent's entries. The only exception is resolving commitments and marking tasks done.
2. **Every write includes a timestamp and agent ID.** This makes it possible to audit who wrote what and when.
3. **IDs are globally unique.** Format: `{agent-name}-{YYYY-MM-DD}-{seq}` where seq is a zero-padded three-digit counter per agent per day.
4. **Monthly archival.** Fix-It archives completed tasks and stale facts older than 90 days into an `/archive/YYYY-MM/` directory.
5. **Collision avoidance.** Each agent only appends to shared files (never edits existing lines). For the `commitments/active.md` file where in-place updates are allowed, only the source agent or the user may modify an entry.
6. **File size monitoring.** Fix-It alerts if any file exceeds 500KB and triggers archival.
7. **Dropbox conflict detection.** Because the brain lives in a Dropbox-synced folder, simultaneous writes can occasionally produce conflict files (e.g., `active (conflicted copy 2026-04-02).md`). Fix-It runs a cron to detect these files, alert the user, and merge or resolve them. The append-only convention minimizes this risk, but `commitments/active.md` is the most likely candidate since it allows in-place edits.
8. **Archive is permanent.** The `/archive/` directory benefits from Dropbox's larger storage capacity. Archived data is never deleted — only moved out of the active working set.

---

## Agent Access Matrix

| Resource | Family Calendar | Meetings Coach | Shopping | News Digest | Fix-It | Connector |
|----------|:-:|:-:|:-:|:-:|:-:|:-:|
| `/people/` | R/W | R/W | R | — | R | R/W |
| `/facts/` | R/W | R/W | R/W | R/W | R | R/W |
| `/commitments/` | R/W | R/W | — | — | R | R/W |
| `/tasks/` | W | W | W | W | R/W | W |
| `/notes/` | R | R | R | — | R | R/W (triage) |
| `/agents/` | W (own) | W (own) | W (own) | W (own) | R/W (all) | W (own) |

R = Read, W = Write, R/W = Read and Write, — = No access

---

## Migration Path to Flux

When a Flux capability is vetted and stable:

1. The relevant MCP tool is exposed to the agent(s) that need it
2. The agent's SOUL is updated to prefer the Flux tool over the shared-brain file
3. The shared-brain file continues to exist as a fallback/backup
4. Once Flux has been stable for 30+ days for that capability, the shared-brain version is deprecated

This ensures no single Flux failure takes down the agent network.
