#!/usr/bin/env bash
# OpenClaw Shared Brain — Setup Script
# Paste this into your SSH session on the VPS.
# It creates the full directory structure and all 16 seed files.

set -euo pipefail

BRAIN="$HOME/Dropbox/openclaw-backup"

echo "Creating directories..."
mkdir -p "$BRAIN"/{people,facts,commitments,tasks,notes,agents,archive,scripts}

echo "Writing README.md..."
cat > "$BRAIN/README.md" << 'ENDOFFILE'
# OpenClaw Shared Brain

A file-based knowledge layer for the OpenClaw multi-agent system. No database, no dependencies — just markdown files in a Dropbox-synced folder with strict conventions.

Six agents read and write to this brain: **Family Calendar**, **Meetings Coach**, **Shopping**, **News Digest**, **Fix-It**, and **Connector**.

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
ENDOFFILE

echo "Writing people/_template.md..."
cat > "$BRAIN/people/_template.md" << 'ENDOFFILE'
# {Full Name}

- **slug:** {slug}
- **circles:** {comma-separated: family-inner, family-extended, friends-close, professional-inner, professional-outer, holiday-card}
- **relationship:** {relationship to user}
- **google_contact_id:** {if synced}
- **email:** {primary email}
- **phone:** {primary phone}
- **platforms:** {WhatsApp, WeChat, email, iMessage, etc.}
- **preferred_channel:** {how they prefer to be reached}
- **last_interaction:** {YYYY-MM-DD, updated by agents}
- **notes:** {any static notes}
ENDOFFILE

echo "Writing facts/2026-04.md..."
cat > "$BRAIN/facts/2026-04.md" << 'ENDOFFILE'
# Facts — April 2026

> **Schema:** Each entry is a structured fact with the following fields.
>
> | Field | Required | Description |
> |-------|----------|-------------|
> | id | Yes | `{agent}-{YYYY-MM-DD}-{seq}` |
> | content | Yes | The fact itself, one sentence |
> | subject | Yes | Person slug or topic tag |
> | source_type | Yes | `direct`, `observed`, `reported`, or `assumed` |
> | source_detail | Yes | How/where this was learned |
> | source_agent | Yes | Which agent recorded it |
> | confidence | Yes | 0.0–1.0 at time of recording |
> | category | Yes | Determines decay half-life |
> | recorded_at | Yes | ISO 8601 timestamp |
> | expires_at | No | Hard expiry date, if applicable |
>
> **Category half-lives:** identity=never, established=365d, situation=90d, preference=180d, plan=30d, logistics=7d, rumor=14d
>
> **Decay:** `effective_confidence = original_confidence × 0.5^(days_elapsed / half_life)` — stale below 0.2
>
> **Convention:** Append-only. Entries separated by blank line + `---` divider.

---
ENDOFFILE

echo "Writing commitments/active.md..."
cat > "$BRAIN/commitments/active.md" << 'ENDOFFILE'
# Commitments — Active

> **Schema:** Each entry tracks a promise or obligation.
>
> | Field | Required | Description |
> |-------|----------|-------------|
> | id | Yes | `{agent}-{YYYY-MM-DD}-{seq}` |
> | who | Yes | Who made the commitment |
> | to_whom | Yes | Who it was made to |
> | what | Yes | What was committed |
> | by_when | No | Deadline, if one exists |
> | status | Yes | `open`, `completed`, `overdue`, `cancelled` |
> | source_detail | Yes | Where this was captured |
> | source_agent | Yes | Which agent recorded it |
> | created_at | Yes | ISO 8601 timestamp |
> | resolved_at | No | When status changed from open |
> | resolution_note | No | How it was resolved |
>
> **Status transitions:** open→completed, open→overdue (auto), open→cancelled, overdue→completed, overdue→cancelled
>
> **Convention:** Only the source agent or user may resolve. In-place edits allowed (sole exception to append-only).

---
ENDOFFILE

echo "Writing tasks/queue.md..."
cat > "$BRAIN/tasks/queue.md" << 'ENDOFFILE'
# Tasks — Queue

> **Schema:** Each entry is an action item.
>
> | Field | Required | Description |
> |-------|----------|-------------|
> | id | Yes | `{agent}-{YYYY-MM-DD}-{seq}` |
> | description | Yes | What needs to be done |
> | assignee | Yes | `me`, `wife`, or an agent name |
> | status | Yes | `open`, `done`, `cancelled` |
> | due_date | No | When it's due |
> | source_agent | Yes | Which agent created it |
> | created_at | Yes | ISO 8601 timestamp |
> | completed_at | No | When marked done |
>
> **Convention:** Append-only. Tasks assigned to "me" are synced to Google Calendar Tasks. Completed tasks archived monthly by Fix-It.

---
ENDOFFILE

echo "Writing notes/inbox.md..."
cat > "$BRAIN/notes/inbox.md" << 'ENDOFFILE'
# Notes — Inbox

> **Schema:** Each entry is a raw human input awaiting triage.
>
> | Field | Required | Description |
> |-------|----------|-------------|
> | id | Yes | `note-{YYYY-MM-DD}-{seq}` |
> | content | Yes | The raw note |
> | source | Yes | Where it came from (workflowy, post-it, voice, etc.) |
> | captured_at | Yes | ISO 8601 timestamp |
> | triaged | Yes | `true` or `false` |
> | triaged_to | No | Where it was routed after triage |
>
> **Triage destinations:** fact → `/facts/`, commitment → `/commitments/active.md`, task → `/tasks/queue.md`, shopping → Shopping agent, flagged → human via Telegram
>
> **Convention:** Append-only. Triage agent marks `triaged: true` and fills `triaged_to` in place.

---
ENDOFFILE

echo "Writing agent status files..."
for agent in family-calendar meetings-coach shopping news-digest fix-it connector; do
  # Convert slug to title case for the header
  case "$agent" in
    family-calendar) title="Family Calendar" ;;
    meetings-coach)  title="Meetings Coach" ;;
    shopping)        title="Shopping" ;;
    news-digest)     title="News Digest" ;;
    fix-it)          title="Fix-It" ;;
    connector)       title="Connector" ;;
  esac

  cat > "$BRAIN/agents/${agent}.status.md" << ENDOFFILE
# ${title} — Status

- **last_heartbeat:** —
- **status:** healthy
- **last_cron_run:** —
- **last_cron_result:** —
- **error_log:** —
- **token_usage_today:** 0
ENDOFFILE
done

echo "Writing agents/shopping.rules.md..."
cat > "$BRAIN/agents/shopping.rules.md" << 'ENDOFFILE'
# Shopping — Channel Routing Rules

> Rules for deciding which shopping channel to use for each item. The Shopping agent applies these rules when processing a shopping request.

## Amazon Subscribe & Save

- **Items:** {recurring non-perishables — toilet paper, tissue, diapers, detergent, etc.}
- **Cadence:** {monthly, bi-monthly, etc.}
- **Notes:** {preferred brands, size preferences, etc.}

## Costco

- **Items:** {bulk purchases — snacks, beverages, household supplies, etc.}
- **Cadence:** {ad hoc, monthly trip, etc.}
- **Notes:** {membership details, preferred location, etc.}

## Local Grocery

- **Store:** {Trader Joe's, Safeway, etc.}
- **Items:** {perishables — milk, produce, bread, etc.}
- **Cadence:** {weekly, as needed}
- **Notes:** {delivery vs. pickup preferences, etc.}

## Routing Logic

Default rules (override per-item as needed):

1. **Amazon S&S** for recurring non-perishables with predictable consumption
2. **Costco** for bulk/value items or one-off large purchases
3. **Local grocery** for perishables or items needed within 48 hours
ENDOFFILE

echo "Writing agents/family-calendar.rules.md..."
cat > "$BRAIN/agents/family-calendar.rules.md" << 'ENDOFFILE'
# Family Calendar — Rules & Defaults

> Default schedules and logistics rules for the Family Calendar agent. Update these as schedules change.

## Pickup / Dropoff Defaults

- **Morning dropoff:** {who, time, location}
- **Afternoon pickup:** {who, time, location}
- **Backup contacts:** {name, phone, relationship}
- **Special instructions:** {car seat notes, ID requirements, etc.}

## School Schedule

- **School year:** {start date – end date}
- **Daily hours:** {start – end}
- **Half days:** {recurring schedule or specific dates}
- **Holidays / breaks:** {list of dates}
- **Dress code days:** {spirit week, picture day, etc.}

## Nanny Schedule

- **Regular days:** {e.g., Mon/Wed/Fri}
- **Hours:** {start – end}
- **Backup nanny:** {name, phone}
- **PTO / blackout dates:** {known dates off}
ENDOFFILE

echo "Writing agents/news-digest.rules.md..."
cat > "$BRAIN/agents/news-digest.rules.md" << 'ENDOFFILE'
# News Digest — Rules & Preferences

> Subscriptions, delivery preferences, and topic filters for the News Digest agent.

## Subscriptions

- **WSJ:** {topics, sections, or feeds}
- **NYT:** {topics, sections, or feeds}
- **WaPo:** {topics, sections, or feeds}
- **LinkedIn:** {feed preferences, people to follow}
- **Other:** {additional sources}

## Delivery Preferences

- **Morning briefing:** {time, channel (Telegram, email, etc.)}
- **Format:** {bullet summary, full article links, TL;DR, etc.}
- **Push alerts:** {criteria for breaking/urgent news}
- **Do not disturb:** {quiet hours}

## Topic Preferences

- **Priority topics:** {e.g., AI/ML, markets, geopolitics}
- **Excluded topics:** {e.g., celebrity gossip, sports scores}
- **People to watch:** {specific individuals whose news matters}
ENDOFFILE

echo "Writing scripts/validate.py..."
cat > "$BRAIN/scripts/validate.py" << 'ENDOFFILE'
#!/usr/bin/env python3
"""OpenClaw Shared Brain — Health Check

Validates directory structure, seed files, Dropbox conflicts, and file sizes.
Usage: python scripts/validate.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_DIRS = [
    "people",
    "facts",
    "commitments",
    "tasks",
    "notes",
    "agents",
    "archive",
]

# Maps relative file path to expected first line (header)
REQUIRED_FILES = {
    "README.md": "# OpenClaw Shared Brain",
    "people/_template.md": "# {Full Name}",
    "facts/2026-04.md": "# Facts \u2014 April 2026",
    "commitments/active.md": "# Commitments \u2014 Active",
    "tasks/queue.md": "# Tasks \u2014 Queue",
    "notes/inbox.md": "# Notes \u2014 Inbox",
    "agents/family-calendar.status.md": "# Family Calendar \u2014 Status",
    "agents/meetings-coach.status.md": "# Meetings Coach \u2014 Status",
    "agents/shopping.status.md": "# Shopping \u2014 Status",
    "agents/news-digest.status.md": "# News Digest \u2014 Status",
    "agents/fix-it.status.md": "# Fix-It \u2014 Status",
    "agents/connector.status.md": "# Connector \u2014 Status",
    "agents/shopping.rules.md": "# Shopping \u2014 Channel Routing Rules",
    "agents/family-calendar.rules.md": "# Family Calendar \u2014 Rules & Defaults",
    "agents/news-digest.rules.md": "# News Digest \u2014 Rules & Preferences",
}

MAX_FILE_SIZE_KB = 500


def check_directories(root):
    """Check that all required directories exist."""
    results = []
    for d in REQUIRED_DIRS:
        path = root / d
        if path.is_dir():
            results.append(("PASS", "Directory exists: {}".format(d), ""))
        else:
            results.append(("FAIL", "Missing directory: {}".format(d), str(path)))
    return results


def check_files(root):
    """Check that all required files exist and have valid headers."""
    results = []
    for rel_path, expected_header in REQUIRED_FILES.items():
        path = root / rel_path
        if not path.is_file():
            results.append(("FAIL", "Missing file: {}".format(rel_path), str(path)))
            continue
        try:
            first_line = path.read_text(encoding="utf-8").split("\n")[0].strip()
        except Exception as e:
            results.append(("FAIL", "Cannot read: {}".format(rel_path), str(e)))
            continue
        if first_line == expected_header:
            results.append(("PASS", "Valid header: {}".format(rel_path), ""))
        else:
            results.append((
                "FAIL",
                "Bad header: {}".format(rel_path),
                "expected: {!r}, got: {!r}".format(expected_header, first_line),
            ))
    return results


def check_conflicts(root):
    """Check for Dropbox conflict files."""
    conflicts = list(root.rglob("*conflicted copy*"))
    if not conflicts:
        return [("PASS", "No Dropbox conflict files found", "")]
    results = []
    for p in conflicts:
        rel = p.relative_to(root)
        results.append(("WARN", "Dropbox conflict: {}".format(rel), str(p)))
    return results


def check_file_sizes(root):
    """Flag files over the size threshold for archival."""
    large = []
    for p in root.rglob("*"):
        if p.is_file():
            size_kb = p.stat().st_size / 1024
            if size_kb > MAX_FILE_SIZE_KB:
                large.append((p, size_kb))
    if not large:
        return [("PASS", "No files over {}KB".format(MAX_FILE_SIZE_KB), "")]
    results = []
    for p, size_kb in large:
        rel = p.relative_to(root)
        results.append(("WARN", "Large file: {} ({:.0f}KB)".format(rel, size_kb), str(p)))
    return results


def print_report(all_results):
    """Print health report. Returns True if healthy (no failures)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("=" * 60)
    print("  OpenClaw Shared Brain \u2014 Health Report")
    print("  {}".format(now))
    print("  Root: {}".format(BRAIN_ROOT))
    print("=" * 60)

    counts = {"PASS": 0, "FAIL": 0, "WARN": 0}

    for section_name, results in all_results:
        print("\n## {}".format(section_name))
        for status, message, detail in results:
            counts[status] += 1
            icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]"}[status]
            print("  {} {}".format(icon, message))
            if detail:
                print("         {}".format(detail))

    print("\n" + "-" * 60)
    print("  PASS: {}  |  FAIL: {}  |  WARN: {}".format(
        counts["PASS"], counts["FAIL"], counts["WARN"]))

    if counts["FAIL"] > 0:
        print("  Status: UNHEALTHY")
        return False
    elif counts["WARN"] > 0:
        print("  Status: HEALTHY (with warnings)")
        return True
    else:
        print("  Status: HEALTHY")
        return True


def main():
    all_results = [
        ("Directories", check_directories(BRAIN_ROOT)),
        ("Files & Headers", check_files(BRAIN_ROOT)),
        ("Dropbox Conflicts", check_conflicts(BRAIN_ROOT)),
        ("File Sizes", check_file_sizes(BRAIN_ROOT)),
    ]
    healthy = print_report(all_results)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
ENDOFFILE

chmod +x "$BRAIN/scripts/validate.py"

echo ""
echo "============================================"
echo "  Setup complete! Running validation..."
echo "============================================"
echo ""

python3 "$BRAIN/scripts/validate.py"
