# The shared brain

*Last updated: 2026-04-18 · Reading time: ~10 min · Difficulty: moderate*

**TL;DR**

- The shared brain is what turns a *pile of agents* into a *fleet*. It's a directory of plain markdown files with a small structured schema on top — no database, no vendor, no API.
- Four core primitives: **facts** (knowledge that decays), **commitments** (promises that resolve), **tasks** (action items), **notes** (raw inputs awaiting triage). Plus per-person profile files and per-agent status/rules files.
- Two halves, two sync mechanisms. `ops/brain/*` is **git-tracked** and flows local → VPS via `deploy.py`. `~/Dropbox/clawford-backup/*` is **Dropbox-synced bidirectionally**. The split is enforced structurally by `agents/shared/brain.py`.
- All writes are appends. Every entry carries an agent ID and a timestamp; the file is its own changelog. Multiple agents writing the same file simultaneously is a designed-for case, not a bug.
- This is the single most underrated piece of infrastructure in the whole fleet. It survived the migration off the OpenClaw platform untouched, because it never depended on the platform — it's just files on disk.

## Why the brain matters

Before the brain existed, every agent in the fleet was an amnesiac. Each cron fired a fresh LLM session, loaded its workspace files, reasoned from scratch, and wrote nothing another agent could read. When the news agent learned the household would be travelling next week, the fact lived in the news agent's cron output and nowhere else. When the shopping agent later wondered whether to hold a delivery that would arrive while the household was out of town, it had no way to know. Two agents, same human, no shared context — and the human got to play messenger between them.

The brain solves that. It is where cross-agent state lives — facts, people, commitments, queues, per-agent status files, fleet-health snapshots. It persists across sessions, across deploys, across crashes. It's what lets one agent remember who somebody is so another agent can remind you to follow up with them.

I almost left it off the list of things a personal fleet actually needs, because once you have it, it stops feeling like infrastructure. If you'd asked me on day one whether the brain was part of the OpenClaw platform or part of Clawford, I'd have said OpenClaw. It isn't. It's just files on disk, synced through Dropbox and git, with schemas I defined and helpers in `agents/shared/brain.py`. It survives every migration the runtime has been through, because it never depended on the runtime.

## The two halves

The brain has two halves that live in different places and sync through different mechanisms.

**The git-tracked half — `ops/brain/*` in the Clawford repo.** Configuration: the canonical schema `README.md`, the seed `_template.md` for new person files, per-agent rules scaffolds, validation scripts. Flows *one-way*: local git → VPS via `deploy.py`. An agent edit never writes back here. If the schema or a rules file needs updating, edit locally, commit, push, redeploy.

**The Dropbox-synced half — `~/Dropbox/clawford-backup/` on the VPS.** Runtime state: live facts, live people files (whose structure came from `_template.md` but whose content is populated by the connector agent and the human), commitments, tasks, notes, per-agent status, `fleet-health.json`, and cross-fleet indexes like `status/calendar-index.json` (built once per morning tick by Mouse's `calendar-index-build.py`; classifies every upcoming event as meeting vs event so Murphy and Mouse read one shared truth instead of independently re-classifying). Flows *bidirectionally*: agents write on the VPS, Dropbox syncs it off-VPS.

> Paths in the repository may still show `openclaw-backup` rather than `clawford-backup` at the time of writing. The rename is queued for a final cleanup pass. Treat the two names as interchangeable until then.

**Don't cross the streams.** Writing agent config to the Dropbox half means it's not in git and can't be versioned, tested, or rolled back. Writing runtime state to the git half means it gets committed to history and potentially leaked. `deploy.py`'s drift check ([Safeguard 4](19-security-and-hardening.md#defense-layer-3-the-deploy-tool-safeguards)) enforces this boundary from one side, and the pre-push hook catches the other.

## The module that codifies the pattern

Every agent used to touch the brain via raw filesystem I/O. That worked, but it meant the git/Dropbox split was enforced by *convention* — one careless `Path.write_text` in the wrong subdirectory could drop runtime state into a git-tracked location, or worse, the other way around. `agents/shared/brain.py` is the module that makes the split structural:

- `read_brain(relative_path) -> str | dict` — reads from either half based on the path prefix
- `write_brain(relative_path, content, *, append=False)` — refuses to write to any path under `ops/brain/*`, because those are git-tracked config that only the deploy tool should ever touch
- Helpers for common schemas: `fleet-health.json`, per-agent `status.md` files, append-only queue files

Every agent that touches the brain today does so through this module. The raw-filesystem fallback isn't gone — Python is Python — but the module is in the path of every reasonable code review.

## The append-only rule

The most important convention in the brain is that **no agent overwrites another agent's entries**. All writes are appends. This prevents collisions when two agents write to the same file simultaneously, and it makes the audit story trivial: every entry carries the agent ID and the timestamp, and the file *is* its own changelog. The only allowed in-place edits are resolving a commitment (source agent or human) and marking a task done (same rule). Everything else is `append >>`.

IDs are globally unique. Format: `<agent-name>-<YYYY-MM-DD>-<seq>`, where `seq` is a zero-padded three-digit counter per agent per day. A single agent can write at most 999 entries per day, which has never come close to being a real limit.

## The four primitives

Every entry in the brain falls into one of four shapes. The schema is intentionally narrow, because every additional shape is one more thing every agent has to know how to read.

- **Facts.** Things known to be true at a point in time. Each fact carries a `decay` field — `never` for identity facts (someone's name, their relationship to the household), `7d` for logistics (someone's travel plans, where a delivery is), `14d` for soft signal (a rumour, an inferred preference). The decay is a hint to readers, not a hard expiry — facts past their decay date are still readable but flagged as stale, and the agent that wrote the fact is responsible for refreshing it if it still applies.
- **Commitments.** Promises with a resolution date. "I told Sam I'd send the photos by Friday" is a commitment. Commitments have status `open`, `done`, or `dropped`. The agent that opened a commitment is responsible for resolving it, but anyone with the right ID can mark it done.
- **Tasks.** Action items the human needs to do. Lighter than a commitment — no external party promised, no resolution date required. Used by the meeting agent to surface follow-ups, by the news agent to flag things worth following up on, etc.
- **Notes.** Raw inputs that haven't been triaged into one of the above yet. The connector agent dumps everything here first, then promotes individual entries to facts/commitments/tasks during its triage pass.

The full schema with field tables, half-lives, and access matrix lives alongside the brain directory itself, with validators that enforce it. It only makes sense in the context of what an agent is trying to say — so write the agent first, then read the schema when you're about to write to the brain for the first time.

## What the brain is *not*

The brain is not a database. It will not scale to a million entries. It will not give you transactions, foreign keys, or a query planner. It is plain markdown files with a small append protocol, and the right mental model is "version-controlled scratchpad shared by six processes." When the fleet outgrows that, the answer is to introduce a real database for the parts that need it, not to add complexity to the brain.

The brain is also not a logging system. Cron output, exception traces, structured telemetry — none of that lives in the brain. Logs go to per-agent `logs/` directories on the VPS. The brain is for *facts the fleet needs to know*, not for the operational record of what each cron did.

## Where this is going

The current brain — markdown files, append-only, no schema enforcement past a YAML frontmatter on each entry — is the right shape for *this* fleet at *this* size. It also has obvious ceilings: no graph queries, no theory-of-mind reasoning across recipients, no formal epistemic-status tracking, no entity resolution across platforms.

In a parallel project I've been building a richer cross-agent cognition layer: PostgreSQL + Apache AGE for Cypher queries over the knowledge graph, a `FactAwareness` model that tracks who knows what (theory of mind), bi-temporal facts with valid-from / invalidated-at and a supersession chain, category-specific confidence decay (established facts decay slowly, rumors fast), entity resolution mapping the same person across messaging platforms, and a `CommunicationAct` layer of pragmatic context (politeness strategy, audience shape, sensitivity, expected response). It's all in production for a single user and an SDK exposes the social-cognition primitives as an MCP server.

Clawford hasn't ported any of that yet. When it does — and the trigger will be one of "I want to ask the fleet a graph question," "I want stale-belief detection on outbound drafts," or "I want a real shared memory across two parallel projects" — the brain in this chapter is what gets replaced. Until then, markdown files on disk are the right shape: legible, debuggable, durable, and zero infrastructure to keep alive.

## Pitfalls

A handful of brain-specific gotchas that bit me before they were obvious:

- **Dropbox conflicted-copy files.** When Dropbox can't reconcile two writes to the same file (rare, but happens during a brief netsplit), it leaves a file named `something (conflicted copy).md` next to the original. Agents must skip these on read, and a daily housekeeping cron sweeps them up. Don't let them accumulate — they'll silently dilute search results.
- **The append boundary on Windows.** A POSIX `open(file, 'a')` is atomic. On Windows (where dev happens), a Python append from one process during a Dropbox sync can briefly hold the file. The shared module retries with backoff; if you see PermissionError on a brain write from a local dev test, that's why.
- **Bootstrapping a new person file.** New person profiles are seeded from `ops/brain/_template.md`. Don't write a new person file by hand — copy the template, replace the placeholders, commit the structural file via `deploy.py`, and let the connector agent populate the live content on the VPS. Hand-edited files drift from the schema and break the validators.
- **The `_template.md` placeholder convention.** Templates use `<<NAME>>`-style placeholders that `deploy.py` will refuse to deploy until they're replaced. This is by design — it's the same Safeguard pattern used for SOUL.md / IDENTITY.md / USER.md sentinel comments described in [Ch 08](08-your-first-agent.md).

## See also

- [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md) — *§ "A durable shared brain"* — the conceptual framing in the migration story
- [Ch 06 — Infra setup](06-infra-setup.md) — where the brain sits in the broader runtime
- [Ch 19 — Security and hardening](19-security-and-hardening.md) — *§ "Defense layer 1"* — the OS-level immutability that protects identity files in the brain
