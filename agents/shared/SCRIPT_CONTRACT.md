# Agent Script Contract

Every script referenced by a cron in a manifest's `scripts` list MUST
conform to this contract. Conformance is enforced at test time by
`agents/shared/tests/test_script_contract.py` and at deploy time by
deploy.py Safeguard 9.

## Why this exists

OpenClaw 2026.4.11 added a hardcoded `exec preflight` check that
rejects any command matching `python3`/`node` + shell operators
(`;`, `&&`, redirects, `sh -lc`, `$?` capture). When an LLM running
a cron session wants to "also check the exit code" it reflexively
wraps the command as `python3 script.py; printf "EXIT:%s" $?` — and
that command gets hard-rejected. This cascaded the entire 6-agent
fleet into "approval required" messages on 2026-04-13.

The durable fix has two sides:

1. **Remove the LLM's reason to wrap**: scripts self-report their
   status in a structured stdout line. The LLM can read that line
   without ever needing `$?`.
2. **Remove the LLM's temptation to wrap**: cron messages tell the
   LLM to run a bare command, parse JSON from stdout, and never
   check exit codes.

This document defines side 1. Side 2 lives in the cron message
conventions in each agent's `manifest.json`.

## The contract

### 1. Stdout

The **final non-blank line** of stdout MUST be a single valid JSON
object. Earlier stdout lines are permitted but discouraged — they
risk confusing the LLM that reads the output. If you need to emit
progress, use stderr instead.

### 2. JSON shape

The JSON object MUST include at minimum:

```json
{"status": "ok" | "error" | "degraded"}
```

When `status` is `error` or `degraded`, the JSON SHOULD also include:

```json
{"status": "error",
 "error": "human-readable one-line explanation",
 "details": {...}}
```

```json
{"status": "degraded",
 "alert": "what's wrong in human terms, suitable for a Telegram message",
 "metrics": {...}}
```

All other fields are script-specific. Use snake_case keys.

### 2a. Forensic envelope fields (added in P0.3, 2026-04-15)

When a script is invoked via `contract_wrap.py` (the canonical path for
every cron), the wrapper auto-injects three forensic fields into the
output envelope:

```json
{"trace_id": "a2b6-...-uuid",
 "agent_id": "shopping",
 "tool_name": "costco-orders"}
```

- **`trace_id`** — UUID per cron invocation. Propagates to the
  subprocess as the `CLAWFORD_TRACE_ID` env var so every LLM call
  inside the script tags its stderr log with the same id. `grep
  trace=<id>` across cron logs reconstructs the full chain of a
  single invocation (envelope + every `infer()` call inside it).
  If the wrapper itself is invoked with `CLAWFORD_TRACE_ID` already
  set in its env, it preserves that value — letting a parent cron
  thread its id through child scripts.
- **`agent_id`** — derived from the script's path
  (`.../agents/<agent>/scripts/…`). A target script can override by
  emitting its own `agent_id` field in its JSON.
- **`tool_name`** — the script's stem (e.g., `costco-orders.py` →
  `costco-orders`). A target script can override by emitting its own
  `tool_name` field in its JSON.

A script that does not use the wrapper (the "native compliance" path
in the test suite) is not required to emit these fields — the
wrapper is the enforcement point. Scripts written after 2026-04-15
that want to be useful for the rate limiter (P1.3) and Doctor Agent
(P0.2) SHOULD attach one additional optional field when they perform
mutations:

```json
{"status": "ok",
 "actions": [
   {"kind": "telegram_send",
    "target": "@operator",
    "parameters_hash": "3f1c8b...sha256hex"}
 ]}
```

Each action's `parameters_hash` is the SHA-256 of the canonical JSON
of whatever identifies that action (Telegram recipient + body,
Gmail message-id + recipient list, Calendar event id + changed
fields, etc.). Use the `parameters_hash` helper exported from
`contract_wrap.py`:

```python
from agents.shared.contract_wrap import parameters_hash
h = parameters_hash({"to": "@operator", "body": message})
```

The helper canonicalizes key ordering so `{"a":1,"b":2}` and
`{"b":2,"a":1}` hash identically.

### 2b. LLM call logs

`agents.shared.llm.infer()` reads `CLAWFORD_TRACE_ID` from the
environment (or accepts a `trace_id=` kwarg) and emits one structured
stderr line per call:

```
[llm trace=<id> ok=1 model=gpt-5.4 in=123 out=45]
```

This is not part of the stdout contract — it lands in stderr, which
the wrapper captures and surfaces on error as `stderr_tail`. The
format is grep-friendly on purpose.

### 3. Exit code

Scripts MUST always exit 0 from their main entry point. NEVER use
`sys.exit(N)` for N != 0 to signal errors. On failure, catch the
exception, print the JSON error object, and exit 0.

This matches openclaw 2026.4.x exec preflight expectations and,
more importantly, removes any reason for the calling LLM to check
`$?`.

### 4. Stderr

Stderr is free — use it liberally for debug logs, progress lines,
and diagnostics. OpenClaw does not parse stderr and it won't
interfere with the JSON stdout contract.

### 5. Invocation

Scripts MUST run cleanly as a bare `python3 <absolute-path>` with
no shell wrappers, pipes, redirects, or operators. Scripts SHOULD
tolerate missing optional environment (e.g., no credentials) by
emitting a structured error JSON rather than crashing.

## Skeleton

```python
#!/usr/bin/env python3
"""One-line description of what this script does."""
from __future__ import annotations

import json
import sys
import traceback


def run() -> dict:
    """Actual work. Raise exceptions on failure; they are caught in main()."""
    # ... the real logic ...
    return {"status": "ok", "count": 42}


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

## Migrating an existing script

Typical pre-contract pattern:

```python
def main():
    token = os.environ.get("FOO_TOKEN")
    if not token:
        print("ERROR: FOO_TOKEN missing", file=sys.stderr)
        sys.exit(1)
    # ... work ...
    print(json.dumps({"done": True}))
    return 0
```

Contract-compliant version:

```python
def run() -> dict:
    token = os.environ.get("FOO_TOKEN")
    if not token:
        raise RuntimeError("FOO_TOKEN missing")
    # ... work ...
    return {"status": "ok", "done": True}


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {"status": "error", "error": str(e)}
    print(json.dumps(result))
    return 0
```

Notes:
- The `raise` at the top moves error handling into the single
  try/except in `main()`. No more scattered `sys.exit(1)` calls.
- The original happy-path JSON `{"done": True}` gains a `status`
  key.
- Auth/credential errors become `status: error` rather than
  stderr + nonzero exit. The calling LLM sees the error in the
  JSON it's already parsing, not via an exit code.

## Allowed deviations

- **Scripts that print a stream of JSON lines**: a script can emit
  multiple JSON objects (progress events), as long as the FINAL
  non-blank line is the contract-shaped JSON object.
- **Scripts that need to exit non-zero for a supervisor**: none
  currently. If one appears in the future, it should live outside
  the cron `scripts` list and not be invoked by LLM sessions at all.

## Contract test

`agents/shared/tests/test_script_contract.py` enforces the contract:

1. **Static**: walks every manifest `crons[].message` and fails if
   any contain forbidden shell-operator patterns (`; echo $?`,
   `sh -lc`, redirects, etc.).
2. **Runtime**: imports each script via subprocess with an isolated
   HOME, asserts the process exits 0, asserts the last stdout line
   parses as JSON with a valid `status` field.

Scripts that can't be fully tested (external deps) should still
satisfy the test — their `run()` will raise early, the wrapper
catches it, and an `{"status": "error", ...}` JSON gets emitted.
That passes the contract.

## Related

- Plan: `C:/Users/Sam/.claude/plans/melodic-fluttering-blanket.md`
- Deploy tool: `agents/shared/deploy.py` Safeguard 9
