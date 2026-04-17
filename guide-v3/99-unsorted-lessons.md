# Unsorted operator lessons

*Last updated: 2026-04-15 (Phase 7d — holding pen fully drained) · Reading time: 2 min · Difficulty: none*

**TL;DR**

- This chapter was a holding pen for raw lessons awaiting triage into their natural chapters. As of Phase 7d, every lesson has been grafted into its natural home and the holding pen is empty. This file is preserved as a historical record of the triage process and a pointer to where each lesson lives now.

## Where the lessons went

- **Lesson A — How a deploy actually moves code to production** → [Ch 07 — Intro to agents § How a deploy actually moves code to production](07-intro-to-agents.md#how-a-deploy-actually-moves-code-to-production). Grafted 2026-04-15.
- **Lesson B — Host-cron exec bit in git** → [Ch 06 — Infra setup § The host-cron runtime](06-infra-setup.md#the-host-cron-runtime). Grafted 2026-04-15.
- **Lesson C — TDD is non-negotiable for anything that mutates shared state** → [Ch 06 — Infra setup § How tests work here](06-infra-setup.md#how-tests-work-here). Grafted 2026-04-15 (Phase 7d).
- **Lesson D — Windows CRLF trap on SSH/SCP** → [Ch 05 — Dev setup § Windows dev box — the CRLF line-ending trap](05-dev-setup.md#windows-dev-box--the-crlf-line-ending-trap). Grafted 2026-04-15.
- **Lesson E — Full agent IDs in disabled-agents, no prefixes** → [Ch 06 — Infra setup § Retiring an agent](06-infra-setup.md#retiring-an-agent--the-file-based-opt-out-pattern). Grafted 2026-04-15.
- **Lesson F — File-based opt-out pattern for agents** → [Ch 06 — Infra setup § Retiring an agent](06-infra-setup.md#retiring-an-agent--the-file-based-opt-out-pattern). Grafted 2026-04-15.
- **Lesson G — Test stubs for piped commands need atomic write, not bare redirect** → [Ch 06 — Infra setup § How tests work here](06-infra-setup.md#pitfalls-inside-the-harness-itself). Grafted 2026-04-15 (Phase 7d).

## Intake notes for the editor

- Every lesson that was raw in this file has been grafted into a proper chapter. If a new lesson lands, add it as a new section below this line and update the "Where the lessons went" list when it graduates.
- The holding-pen pattern is load-bearing — fresh incidents should land here first, in raw prose, and get promoted to their natural chapter during a deliberate edit pass rather than being grafted while the incident is still hot. Raw-first landing preserves the detail and the voice of the moment; the graft pass tightens the prose and fits it into the surrounding chapter structure.
