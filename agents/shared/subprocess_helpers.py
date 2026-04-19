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

# Keys that appear ONLY in a contract envelope — added by the
# __main__ tail in SCRIPT_CONTRACT-compliant scripts and by
# contract_wrap.py. A dict whose keys are a subset of this set carries
# no payload, just run metadata. When stdout contains both a data
# object and an envelope (the 2026-04-18 contract rollout's
# byproduct), the helper must prefer the data object.
_ENVELOPE_ONLY_KEYS = frozenset({
    "status",
    "error",
    "trace_id",
    "agent_id",
    "tool_name",
    "wrapped",
    "traceback",
})


def is_subprocess_error(result: Any) -> bool:
    """Return True iff `result` is the sentinel error dict produced
    by run_json_script() on failure."""
    return isinstance(result, dict) and SENTINEL_KEY in result


def _error(reason: str) -> dict:
    return {SENTINEL_KEY: reason}


def _extract_json_objects(stdout: str) -> list:
    """Walk stdout with json.JSONDecoder.raw_decode and return every
    top-level JSON object concatenated in the stream. Empty list if
    the stream doesn't start with a valid JSON value at all. Leading
    non-JSON text (debug lines before any JSON) terminates extraction
    rather than scanning ahead — the per-line fallback handles that
    case."""
    decoder = json.JSONDecoder()
    objects: list = []
    idx = 0
    n = len(stdout)
    while idx < n:
        while idx < n and stdout[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(stdout, idx)
        except json.JSONDecodeError:
            break
        objects.append(obj)
        idx = end
    return objects


def _select_payload(objects: list):
    """From a list of JSON objects extracted from stdout, pick the
    one most likely to be the script's data payload.

    Preference: the first dict with at least one key OUTSIDE
    _ENVELOPE_ONLY_KEYS (that's a data object). If every dict is an
    envelope-only shape (or the list contains non-dict values), fall
    back to the last object so envelope-only output still round-trips.
    """
    for obj in objects:
        if isinstance(obj, dict) and (set(obj.keys()) - _ENVELOPE_ONLY_KEYS):
            return obj
    return objects[-1] if objects else None


def parse_script_stdout(stdout: str):
    """Parse a contract-compliant script's stdout into a single JSON
    value. Handles three shapes:

      1. A single JSON object/array (pre-2026-04-18 scripts and
         scripts whose main() never prints anything of its own).
      2. A data object followed by a trailing `{"status": "ok"}`
         envelope (the SCRIPT_CONTRACT __main__ tail pattern).
      3. A few debug lines followed by a final JSON object (the
         pre-contract fallback the helper has always supported).

    Returns the parsed value on success, or None if stdout contains
    no parseable JSON. Exposed at module level so callers that can't
    use run_json_script (because they need custom subprocess plumbing,
    like fetch-and-rank.py's LinkedIn scraper call) can reuse the
    same parsing logic.
    """
    if not stdout:
        return None
    stripped = stdout.strip()
    if not stripped:
        return None

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    objects = _extract_json_objects(stripped)
    if objects:
        payload = _select_payload(objects)
        if payload is not None:
            return payload

    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines:
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            pass

    return None


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

    parsed = parse_script_stdout(stdout)
    if parsed is not None:
        return parsed

    return _error(f"{script_path} stdout not JSON: {stdout[:200]}")
