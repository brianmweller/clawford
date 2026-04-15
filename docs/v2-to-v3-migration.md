# Guide v2 → v3 migration checklist

*Last updated: 2026-04-15 (Phase 6)*

Per-chapter disposition for the migration from `guide-v2/` (OpenClaw-era) to `guide-v3/` (Clawford-native). Dispositions:

- **migrate** — port v2 content, strip OpenClaw scar tissue, keep the useful parts
- **rewrite** — v2 version is mostly OpenClaw-era scar tissue; start fresh in v3
- **net-new** — didn't exist in v2; drafted directly in v3
- **archive** — leave in v2 as historical record; don't carry forward

Status values: **pending**, **in_progress**, **done**.

## Checklist

| v2 Ch | v2 file | Disposition | v3 Ch | v3 file | Status | Phase | Notes |
|-------|---------|-------------|-------|---------|--------|-------|-------|
| 01 | `index.md` | migrate | 01 | `index.md` | pending | Phase 7 | Update "stitched together with OpenClaw" paragraph; refresh architecture diagram |
| — | — | net-new | 02 | `02-what-clawford-isnt.md` | **drafted** | Phase 0 → Phase 7 (receipts) | The manifesto chapter. Section 9 "What the migration looked like" is a placeholder until Phase 7. |
| 02 | `02-before-you-start.md` | migrate | 03 | `03-before-you-start.md` | pending | Phase 7 | Add one-line "this is the Clawford-native guide" note at top |
| 03 | `03-vps-setup.md` | migrate | 04 | `04-vps-setup.md` | **done** | Phase 6 | OpenClaw gateway install section replaced with `codex login` + SCP `~/.codex/auth.json` pattern. GHCR PAT step removed. Smoke test items replaced with `codex --version` / `codex infer 'say hi'`. Pre-liberation note flags the vestigial Docker install. |
| 04 | `04-dev-setup.md` | migrate | 05 | `05-dev-setup.md` | **done** | Phase 7d | Migrated with chapter cross-refs bumped (v2 Ch 02 → v3 Ch 03, v2 Ch 05 → v3 Ch 06, v2 Ch 06 → v3 Ch 07). OpenClaw-specific framing stripped: "OpenClaw 2026.4.11 exec preflight" reframed as "an earlier platform version's exec preflight" with a pointer to Ch 02's opening scene. Dead "agent test harness" section (T1-T6, ~/openclaw-tests/, tests/setup-tests.sh) replaced with a new "test surface" section describing the live three-layer pytest structure post-Phase-7. Deploy-tool test count bumped from "19 modules, 300+ cases" to 425+ with 9 safeguards. Grafted in Ch 99 Lesson D (Windows CRLF trap) as a new "Windows dev box" section, with both the short-term `tr -d '\r'` workaround and the long-term `.gitattributes *.sh text eol=lf` fix, plus a new Pitfall callout. |
| 05 | `05-infra-setup.md` | **rewrite** | 06 | `06-infra-setup.md` | **done** | Phase 5 (+ Phase 7d graft + Safeguard 8/11 retirement polish) | v3 describes the shared library (`agents/shared/*`), shared brain (git + Dropbox split), Clawford-native deploy.py with 9 active safeguards (Safeguards 8 and 11 both retired with tombstones), and the host-cron runtime. OpenClaw gateway framing dropped. 2026-04-15: grafted in Ch 99 Lesson B (wrapper exec bits in git) as an inline callout in the host-cron section, Lessons E + F (full-agent-id discipline + file-based opt-out pattern) as a new "Retiring an agent" subsection, and rewrote the Safeguard 8/11 section to reflect their retirement in Phases 5 and 7a. |
| 06 | `06-intro-to-agents.md` | **rewrite** | 07 | `07-intro-to-agents.md` | **done** | Phase 6 (+ Phase 7d graft) | "Two kinds of crons" collapsed into "host crons only + 5 AM PT fleet path." "Exec-approvals, and why mine are permissive" replaced with a three-layer defense-in-depth paragraph pointing at OS-level immutability, the script contract, and the `deploy.py` safeguards. Eight workspace files reframed as durable identity loaded on demand, not auto-loaded per cron tick. Script contract and LLM-vs-deterministic sections preserved. 600s-LLM-cron-budget and BOOTSTRAP.md-split-brain pitfalls dropped (both OpenClaw-specific). 2026-04-15: grafted in Ch 99 Lesson A as "How a deploy actually moves code to production" — the canonical SSH+pull+deploy path, the dead-laptop-mirror trap, dirty-tree handling, and Safeguard 4 drift recovery. |
| 07-0 | `07-0-your-first-agent.md` | migrate | 07-0 | `07-0-your-first-agent.md` | pending | Phase 4 | Update deploy steps for Clawford-native flow |
| 07-1 | `07-1-mr-fixit.md` | migrate | 07-1 | `07-1-mr-fixit.md` | pending | Phase 4 (fix-it sub-phase) | Update references to deploy.py + host crons; the `cron-self-check` story changes to "diff expected-crons.json against crontab -l" |
| 07-2a | `07-2a-lowly-worm-newsfeed.md` | migrate | 07-2a | `07-2a-lowly-worm-newsfeed.md` | pending | Phase 3 | Reference `agents.shared.llm.infer` instead of `openclaw infer`; describe the new host-cron + llm-cron-runner.py pattern |
| 07-2b | `07-2b-lowly-worm-social.md` | migrate | 07-2b | `07-2b-lowly-worm-social.md` | pending | Phase 3 | Same |
| 07-3 | — | net-new | 07-3 | `07-3-mistress-mouse.md` | pending | Phase 4 (family-calendar sub-phase) | Didn't exist in v2; draft directly |
| 07-4 | — | net-new | 07-4 | `07-4-sergeant-murphy.md` | pending | Phase 4 (meetings-coach sub-phase) | |
| 07-5 | — | net-new | 07-5 | `07-5-huckle-cat.md` | pending | Phase 4 (connector sub-phase) | |
| 07-6 | — | net-new | 07-6 | `07-6-hilda-hippo.md` | pending | Phase 4 (shopping sub-phase) | The Costco daemon story is a big chunk — lean on the Tier 3 `camoufox_proxy.py` + `retry_policy.py` library story |
| 07-7 | — | net-new | 07-7 | `07-7-auth-architectures.md` | pending | Phase 4 or 7 | Cross-cutting auth patterns: Google OAuth, Playwright persistent profile, Camoufox + residential proxy + sticky port |
| 08 | — | net-new | 08 | `08-security-and-hardening.md` | pending | Phase 7 | Attack surface, defense in depth (OS-level immutability on SOUL/IDENTITY, script contract, deterministic Python guards) |
| 09 | — | net-new | 09 | `09-scripts-and-configs.md` | pending | Phase 7 | Categorical + alphabetical reference |
| 10 | — | net-new | 10 | `10-cli-reference.md` | pending | Phase 7 | Remove `oc` / `oci` sections entirely — no OpenClaw CLI in v3 |
| 11 | — | net-new | 11 | `11-glossary.md` | pending | Phase 7 | |

## Notes

- **v2 stays frozen.** After Phase 7, a prominent frozen-notice at the top of `guide-v2/index.md` points readers to v3 as the live guide. v2 files themselves are not deleted — they are the historical record.
- **Rewrites cannibalize.** "Rewrite" means the v2 version isn't being ported wholesale, but its concrete facts, file paths, and scar-tissue incidents are fair game for the v3 version. The structural shape changes; the underlying knowledge survives.
- **Cross-references.** During migration, v3 chapters may need to link to v2 chapters that haven't been migrated yet, and vice versa. Prefer linking within v3 where possible; fall back to v2 only for chapters that are explicitly "to migrate later."
- **Update this table's Status column as each chapter completes.** The table is the canonical tracking doc for the migration.
