![Clawford](../assets/Clawford2.png)

# Unsorted operator lessons

*Last updated: 2026-04-15 · Reading time: ~15 min · Difficulty: operational*

**TL;DR**

- This chapter is a holding pen, not a finished lesson set. Each entry below came out of a real session where something went wrong, got diagnosed, got fixed, and left behind a rule worth remembering. The entries are deliberately in a raw format — a short pain description, a rule, a how-to-apply — so a future edit pass can lift the strongest ones into their proper chapters.
- Lessons land here straight from a fresh incident, then graduate into the chapter that's their natural home. Graduated so far: Lesson A → [Ch 07 — Intro to agents](07-intro-to-agents.md#how-a-deploy-actually-moves-code-to-production) (the deploy path), Lessons B / E / F → [Ch 06 — Infra setup](06-infra-setup.md#the-host-cron-runtime) (the host-cron exec bit, the full-agent-id discipline, and the file-based opt-out pattern), all on 2026-04-15. The remaining entries (C, D, G) are still raw.
- If you're reading this chapter as a learner, pick the rules that match the problem you're currently chasing. If you're reading it as an editor, the best homes are called out at the end of each entry.

---

## C. TDD is non-negotiable for anything that mutates shared state

**The rule.** Installers, deploy tools, cron editors, cookie managers, and anything else that touches the VPS crontab, filesystem, or Dropbox state must be built test-first. Write a behavioral test that exercises the real script as a subprocess against a stubbed environment, watch it fail for the right reason, implement the minimum code to make it pass, and only then move to the next case. Structural tests (grep the script for a forbidden substring) are cheap and useful but do not substitute for running the real thing.

**The pain.** The disabled-agents mechanism I shipped on 2026-04-15 was a clean TDD run and it caught two latent bugs the design alone would have missed. The first was a TOCTOU race in the test harness itself — see lesson F below. The second was a stale Phase 6.5 path in a pre-existing test (`test_install_host_cron.py` seeded crontab lines that referenced `/home/node/.openclaw/...`, a deprecated container path that Phase 6.5 replaced with `/home/openclaw/.openclaw/...`). The pre-existing test had a `win32` skipif on it, so it never ran locally on my Windows dev box, and apparently never ran on the VPS either after Phase 6.5 landed. Both bugs would have shipped silently and only surfaced later, in contexts where the root cause would have been far harder to trace. The TDD loop caught them because the *first* thing I did was run the existing test harness against the real installer on the VPS — not because the design review noticed anything.

**How to apply.** Before writing a single line of installer code, write a pytest case that subprocess-runs the installer against a stubbed environment (stubbed `crontab`, stubbed `\$HOME`, stubbed env vars) and asserts the observable behavior. Watch it fail. Read the failure reason and confirm it's the right one (the code does not exist yet, or returns the wrong value, or skips the safety check). Then write the code. Re-run, watch it pass. Move to the next case. If the test is hard to write, the code is hard to trust — refactor for testability before shoveling more code at the problem. And any time you touch an infra file, run its *neighbors'* tests too, because pre-existing bugs hide behind skipifs and stale comments the same way they did in Phase 6.5.

**Best home.** A short dedicated chapter on testing the infra layer, or a section inside `06-infra-setup.md` titled "How tests work here". The existing feedback memory on TDD covers the *rule*; the guide should cover the *harness pattern* — stub `crontab`, stub `\$HOME`, subprocess the real script, inspect the state file.

---

## D. Windows autocrlf breaks bash scripts over SSH and SCP

**The rule.** A Windows development box with default `core.autocrlf=true` flips shell scripts to CRLF on checkout while keeping the index clean as LF. The working copy is CRLF. SCP'ing a CRLF `.sh` file directly to the VPS makes it unrunnable — bash reads `set -euo pipefail\r` as `set -euo pipefail` followed by a stray `\r` token and dies with the cryptic error `set: pipefail: invalid option name`. Normal `git push` + VPS `git pull` is unaffected because the wire format is always LF, but ad-hoc file transfers trip on this.

**The pain.** I hit this inside the TDD loop for the disabled-agents mechanism. I was SCPing iterations of `install-host-cron.sh` to the VPS to run pytest against them on a real Linux environment (MSYS bash on the dev box skips shell tests due to a deliberate `win32` skipif). Every iteration failed at install time with `set: pipefail: invalid option name`. The first error was confusing because the script runs fine locally under Git Bash. The clue was `file ops/scripts/install-host-cron.sh` reporting "CRLF line terminators" and `git ls-files --eol` showing `i/lf w/crlf` — index LF, working-copy CRLF. Ten seconds to fix once the diagnosis was in hand, two minutes to diagnose.

**How to apply.** For ad-hoc SCP of bash scripts from a Windows dev box to the VPS, strip CR in flight: `tr -d '\r' < ops/scripts/install-host-cron.sh | ssh openclaw@198.51.100.42 "cat > ~/repo/ops/scripts/install-host-cron.sh"`. The long-term fix is to add a `.gitattributes` rule pinning `*.sh text eol=lf` and then `git add --renormalize .` to rewrite the working copy. With that in place, Windows checkouts stay LF regardless of the global `autocrlf` setting, and the problem disappears. This is a one-commit change that would retire an entire class of future surprises.

**Best home.** Short caveat in `04-vps-setup.md` in the dev-box section. One paragraph plus the two workarounds.

---

## G. Test stubs for piped commands need atomic write, not bare redirect

**The rule.** When you write a pytest harness that stubs out a command the subject-under-test reads *and* writes to in the same pipeline — the canonical case is `crontab`, which has `crontab -l` for reads and `crontab -` for writes — the stub's writer path must buffer stdin into a temp file and atomically rename. A bare `cat > "\$STATE"` inside the stub will silently truncate `\$STATE` at pipeline setup time, before the reader-side process has drained the existing content. The subject then reads an empty file, writes out `{nothing + new lines}`, and every pre-existing line vanishes.

**The pain.** I caught this on 2026-04-15 while TDD'ing the disabled-agents mechanism, and it almost produced a false-green test. The eviction test seeded the fake crontab with two fix-it lines, set a disabled-agents file containing `fix-it`, ran the installer, and asserted the seed lines were gone. The assertion passed — but not because the new mechanism had evicted them. It passed because the installer's final-append pattern (`{ crontab -l; for l in "\${NEW_LINES[@]}"; do echo "\$l"; done } | crontab -`) triggered the shim's race: `crontab -` ran `cat > "\$STATE"`, which opened `\$STATE` for writing and truncated it at pipeline setup time, *before* `crontab -l`'s `cat "\$STATE"` could read the seeded content. The reader saw an empty file, the writer wrote `{empty + NEW_LINES}` back to `\$STATE`, and the seed was gone — looking exactly like successful eviction. A one-line fix in the shim — `tmp=\$(mktemp); cat > "\$tmp"; mv "\$tmp" "\$STATE"` — defers the rename until stdin has been fully drained and the race closes. The older `test_install_host_cron.py` has the same bug in its own copy of the shim but never hit it, because all of its tests go through drift-eviction paths where the seed is expected to disappear.

**How to apply.** Any time you write a test stub for a command that a subject might run in a read-then-write pipeline against itself, verify the writer side uses atomic replacement. A simple smoke test: `printf 'a\nb\n' > state; { crontab -l; echo c; } | crontab -; grep -q '^a\$' state` — if that fails, the stub has the bug. The same principle applies to any test shim for a file-mutating command that might be piped through itself. Long-term, the two crontab shims in the test suite should be consolidated into a single `conftest.py` fixture with the atomic-replace variant, so the next test author can't accidentally write the buggy form.

**Best home.** Section inside whatever chapter covers infra testing patterns. If no such chapter exists yet, this lesson is a candidate seed for one.

---

## Intake notes for the editor

- Every entry above maps cleanly to one existing guide-v3 chapter. Cross-reference the "Best home" line at the end of each entry when triaging.
- Entry C (TDD) partly duplicates existing guide discussion of test discipline. The new material is the *harness pattern* — stubbing `crontab` + `\$HOME`, subprocessing the real installer, inspecting state files. Graft, don't replace.
- Entries A, B, D, and F are all essentially "close a pit the Phase 6.5/7 migration left open" — there's a case for a one-time "Migration aftermath" section that collects them in one place, and another for distributing them across their natural homes.
- Nothing in this chapter references guide-v2 material, so no broken-link risk from the split.
- If more lessons land before the editor pass, add them as sections H, I, J… and keep this file's TL;DR fresh with a line count.
