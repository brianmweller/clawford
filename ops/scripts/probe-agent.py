#!/usr/bin/env python3
"""probe-agent.py — generic agent probe() invoker.

R6 of the registry-based health system. Used by fleet-health.py
(R3 orchestrator) to invoke each agent's `heartbeat.probe()`
function directly via `docker exec`, bypassing run() — which means
the per-agent .status.md side effect no longer fires on every
*/15 fleet-health tick.

Pre-R6: fleet-health.py invoked `python3 <heartbeat.py>` which ran
main() → run() → probe() + _write_status_md(). Every tick refreshed
the Dropbox .status.md files, which fix-it/morning-status (R4)
no longer reads. Wasteful Dropbox writes × 6 agents × 96 ticks/day.

Post-R6: fleet-health.py invokes
  python3 /home/node/repo/ops/scripts/probe-agent.py <heartbeat-script-path>
which imports the heartbeat module dynamically and prints
json.dumps(heartbeat.probe()). No .status.md side effects.

The heartbeat scripts keep their _write_status_md() helpers for
direct manual invocation (`python3 heartbeat.py` from a debug
shell) — that path still writes the file, useful for one-off
inspection. Only the orchestrator path is purified.

Usage: probe-agent.py <heartbeat-script-path>
Output: one JSON line per SCRIPT_CONTRACT.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import traceback


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({
            "status": "error",
            "alert": "probe-agent.py: usage: <heartbeat-script-path>",
        }))
        return 0

    heartbeat_path = sys.argv[1]
    try:
        spec = importlib.util.spec_from_file_location("agent_heartbeat", heartbeat_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load spec for {heartbeat_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["agent_heartbeat"] = mod
        spec.loader.exec_module(mod)

        if not hasattr(mod, "probe"):
            raise AttributeError(
                f"{heartbeat_path} has no probe() function — R2 contract violation"
            )

        result = mod.probe()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"probe-agent: {heartbeat_path}: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
