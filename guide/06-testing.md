# Chapter 6: Testing

Agents are not deterministic. A cron can be registered but fail silently. Testing is how you know.

---

## Why test agents

An LLM agent receives natural language instructions and decides what to do. It might:
- Interpret your cron message differently than you intended
- Fail to run a command because exec approvals are misconfigured
- Read the right files but write to the wrong ones
- Report success on Telegram while actually doing nothing

The test harness creates deliberate failures, triggers the agent, and verifies it responds correctly.

## Installing the test harness

Transfer and run the setup script:

```bash
scp -i ~/.ssh/id_ed25519 tests/setup-tests.sh openclaw@{server_ip}:/tmp/
ssh -i ~/.ssh/id_ed25519 openclaw@{server_ip} "bash /tmp/setup-tests.sh"
```

This creates `~/openclaw-tests/` with:
- `test-lib.sh` — shared test functions (fixtures, polling, assertions)
- `test-agent.sh` — the test runner
- `tests/fix-it/T1-T6` — six test scripts for Mr Fixit

## Calibration

Before running tests, verify the harness can talk to OpenClaw:

```bash
bash ~/openclaw-tests/test-agent.sh fix-it --calibrate
```

This triggers one cron and shows the raw output. If calibration fails, the gateway isn't accessible from the test harness (check that the `oc()` function works).

## The six tests

### T1: File Edit — Can the agent write to files?

Corrupts `fix-it.status.md`, asks Mr Fixit to repair it, verifies the file was fixed.

**If this fails:** The agent cannot write to files. All crons that modify state are broken. Check exec approvals and Docker volume mounts.

> **Note (post-R3+R6):** `fix-it.status.md` is a legacy artifact —
> `fleet-health.json` is now the live source of fleet state, and no
> cron writes the per-agent `.status.md` files on a schedule anymore.
> T1 still exercises the "can fix-it edit a file it owns" capability,
> but the file it's editing is no longer load-bearing. Retarget T1 at
> any file fix-it is allowed to write (e.g. a scratch file under
> `fix-it-workspace/`) if you update the harness.

### T2: Dropbox Conflict Detection

Creates a fake conflict file (`*conflicted copy*`), triggers the conflict-scan cron, verifies the agent detected it.

**If this fails:** The `find` command may be blocked by exec approvals, or the brain directory isn't mounted in Docker.

### T3: Large File Detection

Creates a 600KB file, triggers file-size-monitor, verifies the agent reported it.

### T4: Brain Validation Failure

Hides `people/_template.md`, triggers brain-validation, verifies the agent detected the missing file.

**If this fails:** Python3 may not be installed in the Docker container. Check: `docker compose exec openclaw-gateway which python3`.

### T5: Monthly Archival

Injects a stale fact (recorded 122 days ago, effective confidence below 0.2) into the active facts file, triggers monthly-archival, verifies the fact was removed from the active file and moved to the archive.

### T6: Boundary Enforcement

Creates a fake agent workspace with a SOUL.md, asks Mr Fixit to edit it, verifies the file was NOT modified.

**If this fails:** `chattr +i` was not applied to the test file (or to SOUL.md in the real workspace). See [Chapter 7](07-hardening.md).

## Running tests

```bash
# Run all tests
bash ~/openclaw-tests/test-agent.sh fix-it

# Run specific tests
bash ~/openclaw-tests/test-agent.sh fix-it T1 T6

# T2-T5 are skipped if T1 fails (they depend on write capability)
```

Expected output:

```
Summary: 6 tests | 6 passed | 0 failed | 0 skipped
Status: ALL PASSING
```

## The `__TEST__` convention

All test fixtures use `__TEST__` in filenames and IDs. This makes cleanup safe — any file containing `__TEST__` in the brain can be safely deleted. The test harness cleans up automatically via `trap EXIT`.

## Adding tests for new agents

Create a directory for the agent and add test scripts:

```bash
mkdir -p ~/openclaw-tests/tests/{agent-name}/
```

Each test script sources `test-lib.sh` and uses the shared functions:
- `inject_test_file` / `restore_test_file` — safely modify brain files
- `trigger_cron` / `send_direct_message` — trigger agent actions
- `wait_for_cron_completion` — poll for results
- `assert_file_contains` / `assert_cron_output_contains` — verify outcomes
- `test_pass` / `test_fail` — report results

---

Next: [Chapter 7 — Hardening](07-hardening.md)
