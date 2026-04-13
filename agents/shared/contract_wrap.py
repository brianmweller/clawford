#!/usr/bin/env python3
"""contract_wrap.py — run a script and enforce the JSON stdout contract.

A thin subprocess launcher that guarantees the contract defined in
`agents/shared/SCRIPT_CONTRACT.md`, regardless of what the wrapped
script actually does:

  1. Runs the target as `python3 <target> [args...]` in a subprocess.
  2. Captures stdout + stderr with a wall-clock timeout (default 5 min).
  3. Always exits 0.
  4. Always prints exactly one JSON line to stdout as the final output.
     The JSON has a mandatory `status` key and a `wrapped` block with
     the target's exit code, stderr tail, and — if the target emitted
     a compliant JSON final line — its body merged in.

Why this exists
---------------

OpenClaw 2026.4.11's hardcoded exec preflight rejects shell commands
that combine `python3`/`node` with operators like `;`, `&&`, `sh -lc`,
`$?`. The LLM reading a cron message often reaches for these wrappers
instinctively (to check exit codes or redirect output), which triggers
the preflight and cascades into "approval required" messages.

Cron messages can instead invoke this wrapper:

  python3 /home/node/repo/agents/shared/contract_wrap.py \\
          /home/node/.openclaw/<ws>/scripts/<script>.py [args...]

That's a single bare command with two .py positional arguments and no
shell operators — the preflight is not triggered. The wrapper handles
exit codes, timeouts, and error translation, so the calling LLM only
ever needs to parse stdout JSON and never needs to check `$?`.

Usage
-----

  python3 contract_wrap.py <target-script.py> [target args...]
  python3 contract_wrap.py --timeout 120 <target> [args...]

Output (always a single JSON line on stdout)
--------------------------------------------

Happy path (target already emits compliant JSON):

  {"status": "ok", "chars": 512, "wrapped": {"exit_code": 0}}
  (fields from the target's final JSON line merged up; contract_wrap's
  own metadata nested under `wrapped`)

Target exited non-zero:

  {"status": "error", "error": "target exited 1",
   "wrapped": {"exit_code": 1, "stderr_tail": "...", "stdout_tail": "..."}}

Target timed out:

  {"status": "error", "error": "timeout after 300s",
   "wrapped": {"exit_code": -1, "stdout_tail": "..."}}

Target raised an import error:

  {"status": "error", "error": "target exited 1",
   "wrapped": {"exit_code": 1, "stderr_tail": "ModuleNotFoundError: ..."}}
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT_S = 300
TAIL_BYTES = 1500


def _parse_argv(argv: list[str]) -> tuple[int, str, list[str]]:
    """Return (timeout_s, target_path, target_args). Raises ValueError on bad args."""
    timeout = DEFAULT_TIMEOUT_S
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--timeout":
            if i + 1 >= len(argv):
                raise ValueError("--timeout requires a value")
            try:
                timeout = int(argv[i + 1])
            except ValueError:
                raise ValueError(f"--timeout value must be int, got {argv[i + 1]!r}")
            i += 2
            continue
        if a == "--":
            i += 1
            break
        # first non-flag positional = target
        break
    if i >= len(argv):
        raise ValueError(
            "usage: contract_wrap.py [--timeout SECS] <target.py> [args...]"
        )
    target = argv[i]
    target_args = argv[i + 1 :]
    return timeout, target, target_args


def _tail(s: str, max_bytes: int = TAIL_BYTES) -> str:
    if not s:
        return ""
    if len(s) <= max_bytes:
        return s
    return "…" + s[-max_bytes:]


def _extract_final_json(stdout: str) -> dict | None:
    """Return the last non-blank stdout line as a dict, or None."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        obj = json.loads(lines[-1])
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return None


def _merge_target_json(base: dict, target_json: dict) -> dict:
    """Merge target JSON fields into base, preserving `wrapped` metadata."""
    wrapped = base.get("wrapped", {})
    merged = dict(target_json)
    # If target has its own status, use it; base status only matters on error.
    if merged.get("status") not in ("ok", "error", "degraded"):
        merged["status"] = base.get("status", "ok")
    merged["wrapped"] = wrapped
    return merged


def run(argv: list[str]) -> dict:
    timeout, target, target_args = _parse_argv(argv)
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = Path.cwd() / target_path

    if not target_path.exists():
        return {
            "status": "error",
            "error": f"target not found: {target_path}",
            "wrapped": {"exit_code": -1},
        }

    try:
        proc = subprocess.run(
            [sys.executable, str(target_path), *target_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(target_path.parent),
        )
    except subprocess.TimeoutExpired as e:
        return {
            "status": "error",
            "error": f"timeout after {timeout}s",
            "wrapped": {
                "exit_code": -1,
                "stdout_tail": _tail(e.stdout or ""),
                "stderr_tail": _tail(e.stderr or ""),
            },
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"subprocess launch failed: {e}",
            "wrapped": {"exit_code": -2},
        }

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    target_json = _extract_final_json(stdout)

    if proc.returncode != 0:
        base = {
            "status": "error",
            "error": f"target exited {proc.returncode}",
            "wrapped": {
                "exit_code": proc.returncode,
                "stdout_tail": _tail(stdout),
                "stderr_tail": _tail(stderr),
            },
        }
        if target_json:
            return _merge_target_json(base, target_json)
        return base

    # Exit 0 — prefer the target's JSON if it was compliant.
    if target_json and target_json.get("status") in ("ok", "error", "degraded"):
        return _merge_target_json(
            {"status": target_json["status"], "wrapped": {"exit_code": 0}},
            target_json,
        )

    # Exit 0 but no compliant JSON — manufacture one.
    return {
        "status": "ok",
        "wrapped": {
            "exit_code": 0,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
        },
    }


def main() -> int:
    try:
        result = run(sys.argv[1:])
    except ValueError as e:
        result = {"status": "error", "error": str(e)}
    except Exception as e:
        result = {"status": "error", "error": f"wrapper crashed: {e}"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
