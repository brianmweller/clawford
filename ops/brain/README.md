# OpenClaw Shared Brain

A file-based knowledge layer for the OpenClaw multi-agent system. No database, no dependencies — just markdown files in a Dropbox-synced folder with strict conventions.

Seven agents read and write to this brain: **Fix-It** (🦊🔧), **Rudolf Von Flugel** (✈️🐕), **Family Calendar** (🐭📅), **Meetings Coach** (🐷🔍), **Shopping** (🦛🛒), **News Digest** (🐛📰), and **Connector** (🐱🤝).

---

## Directory Structure

```
~/Dropbox/openclaw-backup/
├── people/              # One file per person (identity, circles, contact info)
│   └── _template.md     # Template for new person files
├── facts/               # Append-only monthly fact logs (YYYY-MM.md)
├── commitments/         # Active commitment tracking (active.md)
├── tasks/               # Unified task queue (queue.md)
├── notes/               # Raw inputs awaiting triage (inbox.md)
├── agents/              # Per-agent status + rules files
├── archive/             # Monthly archival of completed/stale data
└── scripts/             # Validation and maintenance scripts
```

---

## Core Data Primitives

### 1. Facts

Facts are the primary knowledge unit. Append-only, confidence-scored, and they decay over time based on category.

**Location:** `facts/YYYY-MM.md` (one file per month)

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| id | Yes | `{agent}-{YYYY-MM-DD}-{seq}` |
| content | Yes | The fact itself, one sentence |
| subject | Yes | Person slug or topic tag |
| source_type | Yes | `direct`, `observed`, `reported`, or `assumed` |
| source_detail | Yes | How/where this was learned |
| source_agent | Yes | Which agent recorded it |
| confidence | Yes | 0.0–1.0 at time of recording |
| category | Yes | Determines decay half-life |
| recorded_at | Yes | ISO 8601 timestamp |
| expires_at | No | Hard expiry date, if applicable |

**Category half-lives:**

| Category | Half-Life | Examples |
|----------|-----------|---------|
| identity | Never | "Jane is my sister-in-law", "Bob's birthday is March 5" |
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

Facts with effective confidence below **0.2** are flagged as **stale**. Stale facts are not deleted — agents can re-verify, ignore, or archive them.

---

### 2. Commitments

Commitments track promises and obligations — things that resolve rather than decay.

**Location:** `commitments/active.md`

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| id | Yes | `{agent}-{YYYY-MM-DD}-{seq}` |
| who | Yes | Who made the commitment |
| to_whom | Yes | Who it was made to |
| what | Yes | What was committed |
| by_when | No | Deadline, if one exists |
| status | Yes | `open`, `completed`, `overdue`, `cancelled` |
| source_detail | Yes | Where this was captured |
| source_agent | Yes | Which agent recorded it |
| created_at | Yes | ISO 8601 timestamp |
| resolved_at | No | When status changed from open |
| resolution_note | No | How it was resolved |

**Status lifecycle:**

```
open ──→ completed
  │
  ├──→ overdue ──→ completed
  │        │
  └──→ cancelled ←─┘
```

- Overdue is auto-detected: any agent reading a commitment checks `by_when` against the current date.
- Only the source agent or the user may resolve a commitment.

---

### 3. Notes

Notes are raw human inputs — things jotted down in Workflowy or on a Post-it. They await triage.

**Location:** `notes/inbox.md`

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| id | Yes | `note-{YYYY-MM-DD}-{seq}` |
| content | Yes | The raw note |
| source | Yes | Where it came from (workflowy, post-it, voice, etc.) |
| captured_at | Yes | ISO 8601 timestamp |
| triaged | Yes | `true` or `false` |
| triaged_to | No | Where it was routed after triage |

**Triage destinations:** fact (`/facts/`), commitment (`/commitments/active.md`), task (`/tasks/queue.md`), shopping item (Shopping agent), or flagged for human clarification via Telegram.

---

### 4. Tasks

Tasks are action items assigned to a person or agent.

**Location:** `tasks/queue.md`

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| id | Yes | `{agent}-{YYYY-MM-DD}-{seq}` |
| description | Yes | What needs to be done |
| assignee | Yes | `me`, `wife`, or an agent name |
| status | Yes | `open`, `done`, `cancelled` |
| due_date | No | When it's due |
| source_agent | Yes | Which agent created it |
| created_at | Yes | ISO 8601 timestamp |
| completed_at | No | When marked done |

Tasks assigned to "me" are synced to Google Calendar Tasks by the creating agent. Completed tasks are archived monthly by Fix-It.

---

## People Files

Each person gets a file in `/people/` with identity and circle information. Dynamic knowledge lives in `/facts/` — person files are relatively static.

See `people/_template.md` for the full template.

**Circles:**

| Circle | Description | Check-in Cadence |
|--------|-------------|-----------------|
| family-inner | Spouse, kids, nanny | Daily (via Family Calendar) |
| family-extended | In-laws, aunts, uncles | Weekly–monthly |
| friends-close | College friends, close friends | Monthly |
| professional-inner | Close colleagues, direct reports | Weekly |
| professional-outer | Broader network | Quarterly |
| holiday-card | Full holiday card list | Annually + life events |

A person can belong to multiple circles. The Connector uses circles to determine check-in cadence.

---

## Agent Files

### Status files (`agents/{name}.status.md`)

Each agent writes its own status file. Fix-It reads all of them on a cron to detect unhealthy agents.

**Fields:** last_heartbeat, status (`healthy`/`degraded`/`error`), last_cron_run, last_cron_result (`success`/`partial`/`failure`), error_log, token_usage_today.

### Rules files

- `agents/shopping.rules.md` — Channel routing logic (Amazon Subscribe & Save vs. Costco vs. local grocery)
- `agents/family-calendar.rules.md` — Pickup/dropoff defaults, school schedule, nanny schedule
- `agents/news-digest.rules.md` — Subscriptions, delivery schedule, topic preferences

---

## Global Conventions

1. **Append-only by default.** No agent overwrites another agent's entries. The only exceptions are resolving commitments and marking tasks done.
2. **Every write includes a timestamp and agent ID.** This makes it possible to audit who wrote what and when.
3. **IDs are globally unique.** Format: `{agent-name}-{YYYY-MM-DD}-{seq}` where seq is a zero-padded three-digit counter per agent per day.
4. **Monthly archival.** Fix-It archives completed tasks and stale facts older than 90 days into `/archive/YYYY-MM/`.
5. **Collision avoidance.** Each agent only appends to shared files. For `commitments/active.md` where in-place updates are allowed, only the source agent or the user may modify an entry.
6. **File size monitoring.** Fix-It alerts if any file exceeds 500KB and triggers archival.
7. **Dropbox conflict detection.** Simultaneous writes can produce conflict files (e.g., `active (conflicted copy 2026-04-02).md`). Fix-It detects, alerts, and resolves these.
8. **Archive is permanent.** Archived data is never deleted — only moved out of the active working set.

---

## Agent Access Matrix

| Resource | Fix-It | Rudolf | Family Calendar | Meetings Coach | Shopping | News Digest | Connector |
|----------|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `/people/` | R | — | R/W | R/W | R | — | R/W |
| `/facts/` | R | — | R/W | R/W | R/W | R/W | R/W |
| `/commitments/` | R | — | R/W | R/W | — | — | R/W |
| `/tasks/` | R/W | — | W | W | W | W | W |
| `/notes/` | R | — | R | R | R | — | R/W (triage) |
| `/agents/` | R/W (all) | W (own) | W (own) | W (own) | W (own) | W (own) | W (own) |

R = Read, W = Write, R/W = Read and Write, — = No access

---

## Validation

Run the health check:

```bash
python scripts/validate.py
```

Checks: required directories exist, seed files have valid headers, no Dropbox conflict files, no files over 500KB.

---

## Migration to Flux

When a Flux capability is vetted and stable:

1. The relevant MCP tool is exposed to the agent(s) that need it
2. The agent's SOUL is updated to prefer the Flux tool over the shared-brain file
3. The shared-brain file continues to exist as a fallback/backup
4. Once Flux has been stable for 30+ days, the shared-brain version is deprecated
