# Chapter 2: Shared Brain

Six agents, one brain, no database. Just markdown files with strict rules.

---

## What the shared brain is

The shared brain is a directory of markdown files that all agents read from and write to. It lives at `~/Dropbox/openclaw-backup/` on your VPS and gets synced to the cloud via Dropbox (Chapter 3). No database, no dependencies — just files with conventions.

```
~/Dropbox/openclaw-backup/
├── people/              # One file per person (identity, circles, contact info)
├── facts/               # Append-only monthly fact logs (YYYY-MM.md)
├── commitments/         # Promise/obligation tracking (active.md)
├── tasks/               # Action items (queue.md)
├── notes/               # Raw inputs awaiting triage (inbox.md)
├── agents/              # Per-agent status + rules files
├── archive/             # Monthly archival of completed/stale data
└── scripts/             # Validation and maintenance scripts
```

## The three core data primitives

### 1. Facts — knowledge that decays

Facts are the primary knowledge unit. They are append-only, confidence-scored, and decay over time based on category.

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
```

**Confidence decay formula:**

```
effective_confidence = original_confidence × 0.5 ^ (days_elapsed / half_life)
```

Facts with effective confidence below **0.2** are flagged as stale and archived monthly by Fix-It.

**Category half-lives:**

| Category | Half-Life | Examples |
|----------|-----------|---------|
| identity | Never | "Jane is my sister-in-law" |
| established | 365 days | "Bob works at Google" |
| situation | 90 days | "Uncle is going through chemo" |
| preference | 180 days | "Wife prefers oat milk" |
| plan | 30 days | "College friend visiting in April" |
| logistics | 7 days | "Nanny can't come Thursday" |
| rumor | 14 days | "Heard they might be moving" |

### 2. Commitments — promises that resolve

Commitments track promises and obligations. They don't decay — they resolve.

```markdown
- **id:** meetings-coach-2026-04-02-001
- **who:** Bob Martinez
- **to_whom:** me
- **what:** Send the revised proposal
- **by_when:** 2026-04-05
- **status:** open
```

Status lifecycle: `open` → `completed` / `overdue` / `cancelled`. Any agent reading a commitment checks `by_when` against the current date to auto-detect overdue.

### 3. Notes — raw inputs awaiting triage

Notes are things you jot down in Workflowy or on a Post-it. A designated agent triages them into facts, commitments, tasks, or shopping items.

```markdown
- **id:** note-2026-04-02-001
- **content:** Need to call Dr. Patel about the baby's checkup
- **source:** workflowy
- **captured_at:** 2026-04-02T08:15:00Z
- **triaged:** false
```

### 4. Tasks — action items

Tasks are assigned to a person or agent, with optional due dates. Tasks assigned to "me" can be synced to Google Calendar Tasks.

## The append-only convention

This is the most important rule in the brain: **no agent overwrites another agent's entries.** All writes are appends. This prevents collisions when multiple agents write to the same file simultaneously.

The one exception: `commitments/active.md` allows in-place edits for resolving commitments — but only by the source agent or the human.

## IDs are globally unique

Format: `{agent-name}-{YYYY-MM-DD}-{seq}` where seq is a zero-padded three-digit counter per agent per day. Example: `fix-it-2026-04-02-003`.

## Setting up the brain

Run the setup script on your VPS:

```bash
bash /path/to/setup-brain.sh
```

This creates all directories, seed files (with schema headers), status files for all six agents, rules file placeholders, and the validation script.

Verify:

```bash
python3 ~/Dropbox/openclaw-backup/scripts/validate.py
```

All 24 checks should pass.

## Agent access matrix

| Resource | Family Calendar | Meetings Coach | Shopping | News Digest | Fix-It | Connector |
|----------|:-:|:-:|:-:|:-:|:-:|:-:|
| `/people/` | R/W | R/W | R | — | R | R/W |
| `/facts/` | R/W | R/W | R/W | R/W | R | R/W |
| `/commitments/` | R/W | R/W | — | — | R | R/W |
| `/tasks/` | W | W | W | W | R/W | W |
| `/notes/` | R | R | R | — | R | R/W |
| `/agents/` | W (own) | W (own) | W (own) | W (own) | R/W (all) | W (own) |

Fix-It is the only agent with read access to all agent status files.

## Mount the brain in Docker

The brain lives on the host filesystem (synced by Dropbox), but agents run inside Docker. Add this volume mount to your `docker-compose.yml`:

```yaml
volumes:
  - /home/openclaw/Dropbox/openclaw-backup:/home/node/Dropbox/openclaw-backup
```

Without this mount, agents cannot read or write to the brain.

---

Next: [Chapter 3 — Dropbox Sync](03-dropbox-sync.md)
