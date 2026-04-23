# The shared brain

*Last updated: 2026-04-23 · Reading time: ~25 min · Difficulty: moderate*

**TL;DR**

- The shared brain is what turns a *pile of agents* into a *fleet*. It's a directory of plain markdown files with a small structured schema on top — no database, no vendor, no API.
- Four core primitives: **facts** (knowledge that decays), **commitments** (promises that resolve), **tasks** (action items), **notes** (raw inputs awaiting triage). Plus per-person profile files and per-agent status/rules files.
- A parallel [`self/` subtree](#the-self-layer-a-brain-about-the-operator) mirrors the shape for a single subject — the operator — with a four-layer synthesis pipeline (classifier → archives → structured facts → narrative profile) that feeds Huckle's cold-recruiter drafting AND Murphy's recruiter-meeting prep.
- A brain-adjacent [**calendar brain**](#the-calendar-brain-one-writer-two-readers) at `~/.clawford/calendar-brain/` — one canonical events file, fed by a 60s syncToken-polling listener plus a daily full rebuild, consumed by both Murphy and Mistress Mouse via owner filter. Born from a cache-clobber outage in April 2026 where two independent fetchers kept overwriting each other's `events-{today}.json`.
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

**The Dropbox-synced half — `~/Dropbox/clawford-backup/` on the VPS.** Runtime state: live facts, live people files (whose structure came from `_template.md` but whose content is populated by the connector agent and the human), commitments, tasks, notes, per-agent status, `fleet-health.json`, and a legacy thin-index `status/calendar-index.json` that older readers still consume (double-written by the daily `calendar-brain-build.py` during migration; see [§ The calendar brain](#the-calendar-brain-one-writer-two-readers) for the canonical replacement, which lives at `~/.clawford/calendar-brain/` outside Dropbox). Flows *bidirectionally*: agents write on the VPS, Dropbox syncs it off-VPS.

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

- **Facts.** Things known to be true at a point in time. Each fact carries a `decay` field — `never` for identity facts (someone's name, their relationship to the household), `7d` for logistics (someone's travel plans, where a delivery is), `14d` for soft signal (a rumour, an inferred preference). The decay is a hint to readers, not a hard expiry — facts past their decay date are still readable but flagged as stale, and the agent that wrote the fact is responsible for refreshing it if it still applies. Facts also carry an `audience_scope` field (see below) that gates which recipient circles a fact can surface in — a family fact never reaches a professional draft. Since 2026-04-20, facts also carry an optional `last_reinforced_at` timestamp; a re-observation of the same idempotency key bumps this value and nudges `confidence` up by 0.05 (capped at 0.95) rather than silently skipping the second write. Absent the field, readers treat it as equal to `recorded_at` (the fact has never been reinforced). Since 2026-04-21, facts also carry an optional `mention_slugs` list — other people named in the fact who aren't its subject — which the retrieval path unions with the subject match so "facts about Arthur" can surface a fact whose subject is Arthur's mother; and an optional `known_by` list of slugs believed to have been told the fact (thread participants, meeting attendees), used by the draft composer to skip presenting already-known information as new.
- **Commitments.** Promises with a resolution date. "I told Sam I'd send the photos by Friday" is a commitment. Commitments have status `open`, `done`, or `dropped`. The agent that opened a commitment is responsible for resolving it, but anyone with the right ID can mark it done.
- **Tasks.** Action items the human needs to do. Lighter than a commitment — no external party promised, no resolution date required. Used by the meeting agent to surface follow-ups, by the news agent to flag things worth following up on, etc.
- **Notes.** Raw inputs that haven't been triaged into one of the above yet. The connector agent dumps everything here first, then promotes individual entries to facts/commitments/tasks during its triage pass.

The full schema with field tables, half-lives, and access matrix lives alongside the brain directory itself, with validators that enforce it. It only makes sense in the context of what an agent is trying to say — so write the agent first, then read the schema when you're about to write to the brain for the first time.

## The subject index — a hint, not a source of truth

At 600 facts across three monthly `facts/YYYY-MM.md` files, the brain is still small enough that `load_facts_for_subject("sarah-chen")` can scan every file on every call without feeling it. Past a thousand facts the re-parse starts to show up in compose latency, and the 6h miner cadence is going to push past a thousand before the end of the quarter.

The brain grew a sidecar for this: `facts/_index.json`, a subject → `[[month, fact_id], …]` map that a nightly cron rebuilds. Readers that want per-subject facts consult the index, open only the monthly files that actually contain the subject's entries, and skip the rest. The underscore prefix sorts the file to the top of a directory listing, the same convention `_pending_review.md` already uses.

The index is a **hint**, not a source of truth. Three invariants keep it honest:

1. **Staleness detection.** The loader compares the index's `built_at_epoch` against the mtime of every monthly `*.md` file. If any monthly file has been modified since the index was built — which is the common case during the day, between nightly rebuilds — the loader silently falls back to the full-scan path for that call.
2. **Corruption tolerance.** A malformed `_index.json` returns `None` from the loader instead of crashing. The full-scan path kicks in; the nightly rebuild heals the file.
3. **Writer independence.** Miners and other fact writers never update the index in place. The authoritative shape of the brain is always the `*.md` files; the index derives from them. If the index ever diverges, deleting it is a safe operation — the next rebuild produces a fresh one.

The rebuild runs at 2:45 AM PT under Mr Fixit, slotted between the last pre-dawn miner firing and the morning brief-gen so readers during the compose cycle hit a freshly-rebuilt index. It's a Mr Fixit cron because he's exempt from bubblewrap and already owns fleet brain hygiene (`monthly-archival`, `workspace-snapshot-check`, `brain-validation-check`). Rebuild time for 600 facts is about 6 ms; the whole file is a few KB and checks in to Dropbox like any other brain artifact.

## Audience scope on facts

Every fact carries an `audience_scope` field — a list of up to three tags drawn from `{professional, personal, family, friends, academic, financial, legal, genealogy, internal, public}`. A fact tagged `["family"]` surfaces in drafts to family-circle recipients only. A fact tagged `["personal", "family"]` is visible to both. Absence of a scope means "visible to all," which is the conservative default for facts imported before the tagging system existed.

The motivating story is in [Ch 14 — The correspondence layer](14-huckle-cat.md#audience-scope-the-thing-that-makes-drafts-read-like-me): a colleague's draft accidentally cited a family-medical fact pulled from an unrelated thread, and the fix was to gate fact visibility at the recipient circle rather than trusting prompt-engineering alone. The implementation is two-phase: a one-time LLM batch pass retroactively tagged 257 pre-existing facts via `facts-scope-augment.py`, and every miner that writes a new fact now emits an `audience_scope` at write time through `upsert_fact()`. The scope-augment script stays as a belt-and-suspenders re-tagger for facts that slip through unscoped.

The vocabulary interoperates across projects — a parallel cognitive-exoskeleton project uses the same tags on 7,090 facts, and a one-time importer reads those facts through `upsert_fact()` with scope preserved, so 352 pre-tagged facts landed in the Huckle brain on day one with no translation layer. Scope-at-write-time is the durable pattern; retroactive tagging is the escape hatch.

## Mentioned-but-not-addressed people

The mining pipeline's anti-hallucination rule is that a fact's `subject_slug` has to be in the message's candidate set — derived from the email's From/To/Cc or the meeting's attendee list. That rule keeps the LLM from inventing subjects to satisfy its JSON schema, but it has a failure mode: people who get *mentioned* in a message without being *addressed* can't be fact subjects at all. A mother emails about her two young children; the children have no email, so they have no slug in the candidate set; the fact lands on the mother's slug and there's no way to retrieve "facts that mention child A" at draft time.

The 2026-04-21 fix keeps the subject-candidate gate intact and adds an orthogonal mechanism: every fact can carry an optional `mention_slugs` list. Producers (the miner, mostly) populate it with other known people named in the fact. Consumers (`load_facts_for_subject`, the fast-path index in `brain_index.py`) union facts-whose-subject-is-X with facts-whose-mention_slugs-contains-X. The subject is still the authoritative owner; mention is a retrieval alias.

Person records can carry an optional `parent_slug:` field, which is what lets the miner even know that a minor child belongs to a primary candidate. `build_parent_to_children_map()` builds the reverse index at miner startup, and the extraction prompt surfaces the mention candidates to the LLM with an explicit rule: *cite these in `mention_slugs`, MAY NOT use as `subject_slug`*. Single parent only in v1; depth-1 traversal; no step-parent support yet. The mechanism is deliberately narrow — the parent-slug graph gets wider treatment if and when the need shows up, not preemptively.

## Decay-weighted retrieval

Stored `confidence` is the epistemic value the miner wrote at mint time. Effective confidence — what the retrieval path actually uses — is that number discounted by age through `0.5^(age_days / half_life)`, with per-category half-lives in `agents/shared/decay.py`. Identity, relationship, and birthdate facts have `half_life = None` and don't decay at all; role and employer (365 d), preference (180 d), event and health (90 d), and logistics / rumour / guess (30 d) cover the rest. A fact past one half-life has its confidence halved; past two, quartered; and at the default 0.6 composer threshold, a 90-day-old event at raw 0.8 drops out of compose context automatically.

`last_reinforced_at` takes precedence over `recorded_at` in the age calculation. Reinforcement *is* a re-observation and resets the decay clock — it's the mechanism by which an older-but-still-true fact stays above the retrieval threshold instead of aging out on a fixed schedule. Identity facts mined months before the reinforcement pattern existed are intentionally insulated: their `half_life = None` means the reinforcement mechanic is moot for them.

The curve runs at query time, not at write time. Nothing gets mutated by reading; `load_facts_for_subject()` just annotates each returned fact with `effective_confidence` and sorts the list descending, so callers that truncate hit the strongest-signal facts first. Malformed or missing timestamps degrade open: the formula returns raw confidence rather than penalizing a fact for a bad date. Per-category tuning is a one-line edit to `HALF_LIFE_DAYS`; the formula is centralized so every retrieval path gets the same weighting.

## Theory of mind — known_by

The early composer made a specific, repeatable mistake. A recipient would write the operator a paragraph about a career exploration, the miner would faithfully extract *"Jane has started exploring new roles,"* and three days later, drafting back to the same recipient, the composer would proudly cite the fact in the reply — telling Jane what Jane had just told the operator. *As you know, you're exploring new roles.* Every re-observation of the miner strengthened the fact; none of them knew Jane had *put it there in the first place*.

The fix keeps the fact model unchanged and adds an optional list field: `known_by` on every fact names the slugs believed to have been told it. Gmail miner populates it from the primary candidate set of the source message (From + To + Cc minus the operator). Krisp would populate from attendees. Workflowy stays empty — the operator's private notes have no participants. When another source is added, the miner for that source decides what "was told" means in its world and fills the list accordingly.

At retrieval time, `build_recipient_context()` tags each fact with a `recipient_knows: bool` based on whether the current recipient's slug is in `known_by`. The draft composer renders a `[RECIPIENT KNOWS]` marker on those facts in the WHAT YOU MAY REFERENCE block, and the prompt carries a rule: *don't present these as new information; cite sparingly only when reinforcing*. The LLM sees the tag and adjusts framing — the difference between *"as I mentioned Tuesday"* (shared context) and *"you're exploring new roles"* (stale confabulation).

`known_by` is forward-compatible the same way `audience_scope` was: facts that don't carry it are treated as "no one tracked has been told," which is the conservative default — every draft surfaces them as new-to-recipient. Operator tooling (`backfill-known-by.py`) closed the gap for pre-existing facts by mining Flux's message database and the Gmail API for provenance; see [Ch 14 — Huckle Cat](14-huckle-cat.md) for the backfill walkthrough. The next source to plumb through is the meetings-coach debrief files — Krisp attendee lists carry the same semantic and just haven't been wired yet.

## Person cards as a surfacing layer for facts

People cards (`brain/people/<slug>.md`) are the human-readable summary of who a person is — slug, circles, relationship, `last_interaction`, tone, short context notes. They're what [Ch 14 — Huckle Cat](14-huckle-cat.md) reads when composing a reply, and what the morning relationship nudge reads when deciding who's overdue.

The cards drifted apart from the fact stream for most of the first year — facts accumulated in `brain/facts/YYYY-MM.md` and nothing surfaced them on the corresponding card. A draft would cite a fact the model pulled from the monthly file, but a glance at the person's card gave no indication the fact existed. The fix is an append-only `## Recent observations` section on each card, populated by the miners themselves. A fact mined at confidence ≥ 0.7 triggers a one-line bullet on the subject's card with the date, the content, and the source pointer (`gmail:<msg-id>`, `krisp:<event-id>`, `workflowy:<node-id>`). The section is trimmed to the ten most recent bullets so the card stays scannable.

The rule of thumb is the same one that governs the `voice-profiles/` cache: **don't duplicate durable state.** The fact file is authoritative; the card's recent-observations section is a surfacing layer that exists because scanning three monthly files to answer "what's new about Sarah" is friction nobody should pay. Delete the section and the worst outcome is that the card stops surfacing recent signal — the facts themselves stay intact.

Miners skip the append silently when the target card doesn't exist. Creating a new card from a single mined fact is the wrong default — the fact extractor's confidence floor is there precisely so a single observation isn't authoritative evidence a new subject should enter the brain.

## Voice profiles — brain-adjacent, not brain-native

Huckle's correspondence layer mines the operator's sent mail to produce per-circle voice fingerprints — opening phrase patterns, closing phrase patterns, typical sentence length, emoji density, signature block — for five of the six circles defined in the connector's cadence config (`family-inner`, `family-extended`, `friends-close`, `professional-inner`, `professional-outer`; the sixth, `holiday-card`, didn't meet the minimum-sample floor and falls back to `family-extended` voice). The profiles are consumed by `draft-compose.py` to calibrate the register + politeness for each new reply.

They live in `cache/voice-profiles/` in the connector's workspace rather than in the shared brain proper. Two reasons. First, they're large enough — a per-circle profile is a few KB of extracted style features — that Dropbox sync costs are meaningful if they churn. Second, voice profiles are a Huckle-specific asset today; no other agent reads or writes them. Holding them as agent-workspace state rather than brain state lets Huckle rebuild or invalidate them without coordinating with the rest of the fleet.

That's the rule of thumb for the brain/workspace boundary: **share state that two or more agents need to agree on. Keep agent-private state in the agent's workspace.** Voice profiles are derivative — they're computed from sent mail — so recomputation is cheap and cross-agent agreement isn't required. Facts are authoritative truth about people, so they live in the brain.

## The calendar brain — one writer, two readers

Meeting awareness started out as two independent fetchers. Sergeant Murphy pulled the professional calendar; Mistress Mouse pulled the household ones. Each wrote `events-{today}.json` into its own workspace. Inside each agent, four or five crons called the fetch script with different `--days` windows — morning-brief with `--days 2`, post-meeting-scan with `--days 7`, pre-meeting-alert originally with `--days 1` — and the last writer won the shared filename. For most of April 2026, the writers happened to agree, so the bug never surfaced.

The evening that stopped the pretending was one where I asked Murphy to prep a recruiter interview for the next day and he swore there was nothing on the calendar. The actual failure mode was embarrassingly plain: the pre-meeting-alert cron was running every thirty minutes with `--days 1` and erasing tomorrow's invites every time, overwriting the morning brief's wider pull. Each writer was correct in isolation. The concurrency model was not. Whether Murphy could see tomorrow depended on which cron had most recently clobbered the shared cache.

The calendar brain is what came out of that evening. One canonical JSON file, one writer, both agents read. It lives at `~/.clawford/calendar-brain/calendar-brain.json` — outside the Dropbox-synced brain on purpose, because the listener rewrites it every 60 seconds and sync thrash would be brutal. Every event is normalised once at write time — attendees, conference link, `is_meeting`, `owner`, Murphy's `is_real_meeting` display flag — and readers filter by owner rather than reclassifying. [Mistress Mouse](12-mistress-mouse.md) reads `owner == "mistress-mouse"`. [Sergeant Murphy](13-sergeant-murphy.md) reads `owner == "sergeant-murphy"` plus the `is_real_meeting` display filter. Neither agent calls the Google Calendar API anymore.

Two processes write. A **listener daemon** (`clawford-calendar-brain.service`, user-scope systemd) polls every 60 seconds via Google Calendar's incremental sync API — `events.list(calendarId, syncToken)` returns the delta since the last tick, the daemon applies it, atomic-writes the new copy. A 410 GONE on the sync token triggers a bounded full fetch to re-seed. A transient 401 on one calendar is isolated to that calendar's token column, so the other calendars' deltas still apply. A **daily rebuild** at 10:25 UTC — `calendar-brain-build.py`, running five minutes before the fleet morning brief — pulls the full 8-day window, renormalises everything, and resets every sync token. It's the belt-and-suspenders under the listener, and it's also what writes the legacy `status/calendar-index.json` thin-index shape that some older readers still consume during the tail of the migration.

The natural question is why polling and not webhooks. Google Calendar's native push is an HTTPS webhook, which needs a public endpoint with its own TLS rotation, retry semantics, and firewall hole. At the fleet's scale — two calendars, ~30 events in the lookahead window — 60-second polling hits sub-2-minute latency for essentially free: `N × 1440` calls per day against a 1,000,000/day quota. Gmail uses Pub/Sub pull because Gmail supports it; Calendar doesn't, so polling is the cheap equivalent. Revisit if an "I just created an invite, the agent missed it" gap starts biting; until then, polling is enough.

Rollback is a single marker file. `touch ~/.clawford/calendar-brain-disabled` makes the systemd unit refuse to start — the `ExecStartPre` checks for the marker, systemd obeys, and the daily rebuild keeps the brain fresh enough to survive. When the marker comes off the listener resumes from wherever it left.

> 🧨 **Pitfall.** Writing a second cron that produces `events-{today}.json`. **Why:** that was the April 2026 cache-clobber bug — a second writer with a narrower `--days` window silently erased forward-looking events between writes, and every caller in the fleet paid the price the next time the window flipped. **How to avoid:** every caller reads the brain through `_run_gcal_fetch` in `tools.py`, or through the hollowed `gcal-fetch.py` shim both agents now ship. There is no second writer.

## The self/ layer — a brain about the operator

The brain so far has been a brain about *other people*: ~400 people files, ~600 facts scoped by audience. It has nothing about the operator themselves. That asymmetry is not ideology — it's convenience — and it started biting the moment Huckle's compose loop was asked to draft a reply to a cold recruiter with no counterpart people file and no historical context. A second subtree under `clawford-backup/` now carries the mirror: everything the brain knows about the operator's professional history, active target list, leadership style, level & scope bar, and pipeline status. Same shape as the rest of the brain — markdown + JSON on disk, regenerate-from-below semantics, no database — but with a single subject.

The self-brain lives at `~/Dropbox/clawford-backup/self/` and fans out across eight artifacts:

| Artifact | Shape | Source |
|----------|-------|--------|
| `profile.md` | Narrative markdown — track record, strengths, leadership style, level & scope bar, target-role shape | Stage 1.2 synthesis over the structured facts + priority raw docs |
| `archives/<role>.md` | Per-role synthesised extracts — scope, OKRs owned, tenets authored, major accomplishments, feedback themes | Stage 3 synthesis grouped by role or search round |
| `archive-index.json` | Per-file classification — role, class, signal score, chronological date | Stage 1 classifier over `Personal/Bio/`, `Personal/Job Search/`, `Archive/<employer>/`, Workflowy |
| `facts/{employer,major_accomplishment,target_company,tenet_authored,strength_theme}.json` | Typed fact tables with schemas | Stage 4 structured extraction from archives |
| `facts/active_search_stages.json` | Per-company stage entries for in-flight interviews | `search-status-build.py` over Gmail + Workflowy |
| `role-timeline.md` | Hand-editable chronology — start/end dates per employment span | Operator authors once; downstream stages route by `chronological_date` |
| `search-timeline.md` | Hand-editable chronology — start/end dates per round of job search | Operator authors once; same routing pattern |
| `linkedin-profile-current.pdf` | Authoritative bio input for profile synthesis | Operator exports from LinkedIn periodically |

The architecture deliberately mirrors the four-primitive brain on the other side. People files → `profile.md`. `brain/facts/YYYY-MM.md` → `self/facts/*.json`. `brain/people/<slug>.md`'s `## Recent observations` section → `self/archives/<role>.md`. The boundary between the two subtrees is strict: anything about other people goes in the main brain; anything about the operator goes in `self/`. The Huckle-specific importer pipeline that builds `self/` is covered in [Ch 14 — The professional brain](14-huckle-cat.md#the-professional-brain); this section is the architectural slot. Murphy now consumes the same subtree for [recruiter-meeting prep](13-sergeant-murphy.md#professional-meeting-prep) — demonstrating the pattern works cleanly under a second reader without either agent knowing about the other.

Three design rules carry over from the main brain and three are unique to this subtree.

**Carry-over rules.** Writes are regenerable from the layer below. Raw source material (the employer archives, the LinkedIn PDF) is referenced by path, never copied into the brain. Confidentiality is enforced by the synthesis prompt, which speaks in terms of the operator's scope and outcomes rather than quoting proprietary strategy verbatim.

**Rules unique to `self/`.** Chronological tagging is load-bearing: every record in `archive-index.json` carries a `chronological_date` and a `date_source` so the per-role synthesis can route to the right bucket. A class-based override routes anything tagged `job_search_material` to the matching search round regardless of the date heuristic, because a decision-framework doc authored during employment at one company but about looking at a different company belongs to the search round, not the employer archive. And the timeline files are hand-edited, not synthesised — the operator's role spans are ground truth, and making the synthesiser infer them from filenames would re-introduce the drift the classifier is trying to eliminate.

The self-brain is new enough that it carries no named incidents of its own yet; the scar tissue lives entirely in the four-iteration calibration arc documented in Ch 14's [§ The calibration draft](14-huckle-cat.md#the-calibration-draft). The architectural lesson that survives that arc is that **a compose path needs to know both sides of the relationship.** The people-and-facts brain covers the recipient side. The self-brain covers the operator side. Both are necessary to draft a reply that reads like the operator wrote it — which is the actual product goal of the whole correspondence layer.

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
