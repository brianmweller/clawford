"""agents/shared/subprocess_helpers.py — shared helpers for invoking
sibling scripts as subprocesses.

Consolidates the `_run_script` pattern duplicated across ~12 agent
scripts. The key thing the old pattern got wrong: it returned `None`
on any failure, which callers silently collapsed into "no data this
run" and reported `status: ok`. The 2026-04-15 incident was a 2.5-day
silent Gmail OAuth outage hidden behind exactly this pattern.

Contract:

  run_json_script(script_path, *args, timeout=90) -> dict | list
      Invoke the script, parse its stdout as JSON. Success returns
      the parsed value. Any failure (spawn error, timeout, non-zero
      exit, empty stdout, malformed JSON) returns
      {"__error__": "<human-readable reason>"}.

  is_subprocess_error(result) -> bool
      True only for the sentinel error dict. Any legitimate script
      output (including a dict with status=error from the script
      itself) returns False — only our injected sentinel key counts.

Callers should:
    result = run_json_script("foo.py")
    if is_subprocess_error(result):
        return {"status": "error", "error": result["__error__"]}
    # ... use result

The sentinel key is `__error__` specifically so it's unlikely to
collide with legitimate script output fields. Scripts that naturally
emit a `status` field (ok/error/degraded) keep those semantics — the
helper only covers the subprocess-level failure class.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any


DEFAULT_TIMEOUT_S = 90

SENTINEL_KEY = "__error__"


def is_subprocess_error(result: Any) -> bool:
    """Return True iff `result` is the sentinel error dict produced
    by run_json_script() on failure."""
    return isinstance(result, dict) and SENTINEL_KEY in result


def _error(reason: str) -> dict:
    return {SENTINEL_KEY: reason}


def run_json_script(
    script_path: str,
    *args: str,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> Any:
    """Run a Python script and parse its JSON stdout.

    Returns:
        On success: the parsed JSON value (dict, list, int, etc.)
        On failure: {"__error__": "<reason>"}. Check with
        is_subprocess_error() before using the result.

    Failure modes captured:
        - subprocess.TimeoutExpired → "<script> timed out after <N>s"
        - FileNotFoundError / OSError during spawn → "<script> spawn failed: <error>"
        - Non-zero exit → "<script> exit <N>: <stderr tail>"
        - Empty stdout → "<script> produced empty stdout"
        - Malformed JSON → "<script> stdout not JSON: <truncated>"

    The timeout parameter is forwarded to subprocess.run() directly;
    callers should size it to the script's expected worst-case
    runtime plus headroom.
    """
    cmd = [sys.executable, script_path, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _error(f"{script_path} timed out after {timeout}s")
    except FileNotFoundError as exc:
        return _error(f"{script_path} spawn failed: not found ({exc})")
    except OSError as exc:
        return _error(f"{script_path} spawn failed: {exc}")

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        return _error(f"{script_path} exit {result.returncode}: {stderr_tail[0][:200]}")

    stdout = (result.stdout or "").strip()
    if not stdout:
        return _error(f"{script_path} produced empty stdout")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    # Fallback: some scripts print debug lines before the final JSON.
    # Try parsing the last non-empty line.
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if lines:
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            pass

    return _error(f"{script_path} stdout not JSON: {stdout[:200]}")
