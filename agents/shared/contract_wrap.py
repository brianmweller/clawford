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

  python3 /home/openclaw/repo/agents/shared/contract_wrap.py \\
          /home/openclaw/.clawford/<ws>/scripts/<script>.py [args...]

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

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

DEFAULT_TIMEOUT_S = 300
TAIL_BYTES = 1500

TRACE_ID_ENV_VAR = "CLAWFORD_TRACE_ID"
AGENT_ID_ENV_VAR = "CLAWFORD_AGENT_ID"
# P1.2: per-agent isolation. Set CLAWFORD_ISOLATION_MODE=bwrap on the
# host cron line to wrap the subprocess in bubblewrap. Mr Fixit is
# always forced to "none" in isolation.resolve_isolation_mode().
ISOLATION_MODE_ENV_VAR = "CLAWFORD_ISOLATION_MODE"


def parameters_hash(payload: dict) -> str:
    """SHA-256 of canonical-JSON of `payload`, as lowercase hex.

    Stable across key orderings — `{"a":1,"b":2}` and `{"b":2,"a":1}`
    produce the same hash. Used by scripts to compute a reproducible
    identifier for any mutation payload (Telegram send, Gmail send,
    Calendar write, …) so the rate limiter (P1.3) can spot duplicates
    and the Doctor Agent (P0.2) can ask "same tool_name with the same
    parameters_hash more than N times in 24h?".
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _derive_agent_id(target_path: Path) -> str:
    """Infer the agent id from the script's path.

    Canonical layouts:
      - `.../agents/<agent>/scripts/<script>.py` → `<agent>`
      - `.../<agent>-workspace/scripts/<script>.py` → `<agent>`
        (this is how agents run on the VPS — out of
        ~/.clawford/<agent>-workspace/scripts/)

    Falls back to the script's parent-of-scripts directory name for
    other layouts (tests with fabricated dirs).
    """
    parts = target_path.resolve().parts
    for i, p in enumerate(parts):
        if p == "agents" and i + 1 < len(parts):
            return parts[i + 1]
    parent = target_path.parent
    if parent.name == "scripts":
        # VPS workspace: ~/.clawford/<agent>-workspace/scripts/foo.py
        # → strip the "-workspace" suffix to get the canonical id.
        candidate = parent.parent.name
        if candidate.endswith("-workspace"):
            return candidate[: -len("-workspace")]
        return candidate
    return parent.name


def _derive_tool_name(target_path: Path) -> str:
    """Script stem, e.g. `costco-orders.py` → `costco-orders`."""
    return target_path.stem


def _new_trace_id() -> str:
    return str(uuid.uuid4())


def _build_subprocess_argv(
    *,
    agent_id: str,
    target_path: Path,
    target_args: list[str],
) -> list[str]:
    """Compose the argv for the wrapped subprocess.

    Default: `[python3, target.py, *args]`.

    When CLAWFORD_ISOLATION_MODE=bwrap and the agent isn't on the
    exempt list AND bwrap is available, returns the bwrap prefix
    followed by the python invocation. Failures to set up isolation
    fall through to the unwrapped command and log a warning — never
    block the script.
    """
    base = [sys.executable, str(target_path), *target_args]
    raw_mode = os.environ.get(ISOLATION_MODE_ENV_VAR, "").strip().lower()
    if not raw_mode:
        return base
    try:
        # Lazy import: keeps contract_wrap loadable in environments
        # without the isolation module sitting next to it (e.g., when
        # the wrapper is invoked before sync_shared_library has run).
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from isolation import (  # type: ignore
            ISOLATION_BWRAP,
            is_bwrap_available,
            bwrap_command,
            resolve_isolation_mode,
        )
    except Exception as e:
        print(
            f"[contract_wrap] isolation import failed ({e}); "
            f"running unwrapped",
            file=sys.stderr,
        )
        return base

    effective = resolve_isolation_mode(
        agent_id=agent_id, manifest_mode=raw_mode,
    )
    if effective != ISOLATION_BWRAP:
        return base
    if not is_bwrap_available():
        print(
            "[contract_wrap] CLAWFORD_ISOLATION_MODE=bwrap requested but "
            "bwrap not on PATH; running unwrapped",
            file=sys.stderr,
        )
        return base

    workspace = _workspace_for(target_path)
    brain_root = _brain_root()
    repo_root = _repo_root()
    prefix = bwrap_command(
        agent_id=agent_id,
        workspace=workspace,
        brain_root=brain_root,
        repo_root=repo_root,
    )
    return prefix + base


def _workspace_for(target_path: Path) -> Path:
    """Derive the workspace dir for a target script. Mirrors the path
    convention used everywhere else: ~/.clawford/<agent>-workspace/."""
    parent = target_path.parent
    if parent.name == "scripts":
        return parent.parent
    return parent


def _brain_root() -> Path | None:
    p = Path(os.path.expanduser("~/Dropbox/openclaw-backup"))
    return p if p.exists() else None


def _repo_root() -> Path | None:
    # Walk up from this file looking for the repo root (the first
    # ancestor containing both `agents/` and `ops/`).
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "agents").is_dir() and (ancestor / "ops").is_dir():
            return ancestor
    return None


def _inject_forensic_fields(
    envelope: dict, target_path: Path, trace_id: str
) -> dict:
    """Ensure the envelope carries trace_id, agent_id, tool_name.

    Target-supplied values win over wrapper-derived ones so a caller
    can override (e.g. a parent cron that wants to propagate its own
    trace_id, or an agent whose directory name doesn't match its
    operator-facing identity).
    """
    envelope.setdefault("trace_id", trace_id)
    envelope.setdefault("agent_id", _derive_agent_id(target_path))
    envelope.setdefault("tool_name", _derive_tool_name(target_path))
    return envelope


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

    # Forensic envelope fields. trace_id propagates to the subprocess via
    # env var so LLM calls made inside the script can tag their own
    # stderr logs with the same id and the whole invocation threads.
    trace_id = os.environ.get(TRACE_ID_ENV_VAR) or _new_trace_id()
    agent_id = os.environ.get(AGENT_ID_ENV_VAR) or _derive_agent_id(target_path)
    subprocess_env = os.environ.copy()
    subprocess_env[TRACE_ID_ENV_VAR] = trace_id
    # Same propagation pattern as trace_id: shared modules running
    # inside the subprocess (telegram_api, llm, reviewer) can read
    # CLAWFORD_AGENT_ID to know which agent's behavior they're
    # mediating without every caller having to plumb agent_id through.
    subprocess_env[AGENT_ID_ENV_VAR] = agent_id

    # P1.2: build the subprocess argv. When CLAWFORD_ISOLATION_MODE=bwrap
    # AND bwrap is on PATH AND the agent isn't on the exempt list (Mr
    # Fixit is forced to "none"), prefix the python invocation with the
    # bwrap argv. Otherwise run unwrapped (back-compat default).
    cmd_argv = _build_subprocess_argv(
        agent_id=agent_id,
        target_path=target_path,
        target_args=target_args,
    )

    def _finish(envelope: dict) -> dict:
        return _inject_forensic_fields(envelope, target_path, trace_id)

    if not target_path.exists():
        return _finish({
            "status": "error",
            "error": f"target not found: {target_path}",
            "wrapped": {"exit_code": -1},
        })

    try:
        proc = subprocess.run(
            cmd_argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(target_path.parent),
            env=subprocess_env,
        )
    except subprocess.TimeoutExpired as e:
        return _finish({
            "status": "error",
            "error": f"timeout after {timeout}s",
            "wrapped": {
                "exit_code": -1,
                "stdout_tail": _tail(e.stdout or ""),
                "stderr_tail": _tail(e.stderr or ""),
            },
        })
    except Exception as e:
        return _finish({
            "status": "error",
            "error": f"subprocess launch failed: {e}",
            "wrapped": {"exit_code": -2},
        })

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
            return _finish(_merge_target_json(base, target_json))
        return _finish(base)

    # Exit 0 — prefer the target's JSON if it was compliant.
    if target_json and target_json.get("status") in ("ok", "error", "degraded"):
        return _finish(_merge_target_json(
            {"status": target_json["status"], "wrapped": {"exit_code": 0}},
            target_json,
        ))

    # Exit 0 but no compliant JSON — manufacture one.
    return _finish({
        "status": "ok",
        "wrapped": {
            "exit_code": 0,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
        },
    })


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
