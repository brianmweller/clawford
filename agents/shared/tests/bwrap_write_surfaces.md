# bwrap write-surface audit — `~/Dropbox/openclaw-backup/`

Audit date: 2026-04-21. Triggered by `calendar-index-build` EROFS
regression (`[Errno 30] Read-only file system:
'/home/openclaw/Dropbox/openclaw-backup/status/calendar-index.json.tmp'`)
which surfaced on the 2026-04-20 bwrap rollout. Original whitelist
(`facts, people, commitments, queues`) undercounted real write
surfaces; this file is the audit that drives the widened whitelist in
`agents/shared/isolation.py::bwrap_command`.

## Methodology

1. `grep -rE "openclaw-backup/[A-Za-z0-9_-]+" agents/ ops/` for literal
   path references.
2. `grep -r "dropbox_brain_root\(\)\s*/\s*\"[a-z_-]+\""` for
   helper-resolved paths.
3. For each hit classify: write-op? which subdir? owning agent in
   `ops/bwrap-allowlist.default.txt`?

## Write targets under `~/Dropbox/openclaw-backup/`

Legend — **IsolStatus**: ✅ in whitelist prior to this audit; ❌ not in
whitelist (EROFS under bwrap); 🛡️ agent is ISOLATION_EXEMPT
(fix-it) — bwrap doesn't apply.

| Subpath | Writer(s) | Writer cron(s) in allowlist | IsolStatus |
|---------|-----------|------------------------------|------------|
| `status/` | `agents/shared/scripts/calendar-brain-build.py` (`_atomic_dump_json`, double-writes legacy `calendar-index.json` during migration from the retired `family-calendar/scripts/calendar-index-build.py`) | `calendar-brain-build` | ❌ **active regression** |
| `tasks/` | `agents/shared/brain_tasks.py` (`QUEUE_RELPATH = "tasks/queue.md"`, `append_task`, in-place edits), `agents/family-calendar/scripts/gcal-tasks-sync.py` | `family-calendar-tasks-sync` | ❌ **imminent regression** |
| `people/` | `agents/connector/scripts/daily-refresh.py::update_last_interaction`, `people-seed.py`, `person-bootstrap.py`, `birthday-miner.py`, `voice-profile-build.py`, `holiday-card-reclassify.py`, `agents/shared/brain.py::create_person_file` | `connector-gmessages-mine`, `connector-birthday-miner`, `connector-inbox-triage` (indirectly), others | ✅ |
| `facts/` | `agents/shared/facts.py`, `agents/shared/fact_extraction.py`, `agents/connector/scripts/birthday-miner.py`, `agents/connector/scripts/facts-scope-augment.py`, `agents/fix-it/scripts/brain-index-rebuild.py` | `connector-gmail-facts-mine`, `connector-workflowy-facts-mine`, `meetings-coach-krisp-facts-mine`, `connector-birthday-miner` | ✅ |
| `commitments/` | `agents/meetings-coach/scripts/commitment-tracker.py`, `post-meeting-scan.py`, `agents/connector/scripts/commitment-scan.py` | `meetings-coach-*`, `connector-*` (post-meeting, auto-compose) | ✅ |
| `queues/` | `agents/shared/tests/test_brain.py` (test-only — `queues/events.log`). No production writer located. | — | ✅ (already bound; keep for defensive compatibility) |
| `notes/` | `agents/shared/brain.py::append_inbox_note`, `agents/connector/scripts/notes-triage.py` (`BRAIN_INBOX = "notes/inbox.md"`) | `connector-inbox-triage` (writes via dispatcher tools flow) | ❌ **add** |
| `workspace-snapshots/` | `agents/shared/workspace-snapshot.py` | Scheduled via fix-it orchestration; fix-it is ISOLATION_EXEMPT | 🛡️ no-op for bwrap |
| `deploy-backups/` | `agents/shared/deploy.py` | Deploy invoked by human over SSH, not through bwrap-wrapped cron | n/a |
| `archive/YYYY-MM/` | `agents/fix-it/scripts/monthly-archival.py` | fix-it is ISOLATION_EXEMPT | 🛡️ no-op for bwrap |
| `fix-it/*` (probation.md, KNOWN_ISSUES.md, drift-audit.md) | fix-it scripts | fix-it is ISOLATION_EXEMPT | 🛡️ no-op for bwrap |
| `tmp/gmessages-2fa.png` | `agents/connector/scripts/gmessages-auth.py` | Manual-run auth bootstrap (not scheduled); not cron | n/a |
| `fleet-health.json` | `ops/scripts/fleet-health.py` | Host cron wrapper (`script-contract-host.sh`); not currently allowlisted for bwrap | ✅ unaffected — file-level bind added defensively below |
| `agents/<id>/` | per-agent status files, MEMORY.md, drift manifests | every agent | ✅ (RW via `agent_brain` bind at line 190–191 of isolation.py) |
| `agents/<id>.status.md` | `agents/fix-it/scripts/heartbeat.py` references; legacy path, verify still live | fix-it — exempt | 🛡️ no-op for bwrap |

## Conclusion — additions needed

Add to `agents/shared/isolation.py::bwrap_command` RW whitelist:

| Subpath | Reason |
|---------|--------|
| `status/` | Fix calendar-index-build EROFS regression. |
| `tasks/` | Fix imminent gcal-tasks-sync regression; cover all brain_tasks.py callers. |
| `notes/` | Cover append_inbox_note and notes-triage write flow. |

Leave in place (already bound):
- `facts`, `people`, `commitments`, `queues`.

Add file-level RW bind:
- `fleet-health.json` — defensive. Not currently broken because the
  host-cron writer isn't under bwrap, but if a future agent cron reads
  *and updates* the file (partial writes, status annotations), it
  would fail silently. File-level bind is cheap.

Not needed right now:
- `workspace-snapshots/`, `deploy-backups/`, `archive/`, `fix-it/`,
  `tmp/` — either fix-it-only (exempt) or human-invoked paths.

## Follow-up bwrap-allowlist.default.txt doc fix

The header comment currently says:

> Writes to other brain paths (`brain/memory/`, `brain/status/`, etc.)
> still hit EROFS — by design.

After this audit lands, update the comment to:

> Writes to `brain/memory/` still hit EROFS — by design. Other brain
> subdirs (`facts`, `people`, `commitments`, `queues`, `status`,
> `tasks`, `notes`) are RW-bound. Audit: `agents/shared/tests/
> bwrap_write_surfaces.md`.
