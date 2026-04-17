"""HeartbeatProbe — base class for per-agent heartbeat probes.

Every agent's `scripts/heartbeat.py` defines a `probe()` function that
returns a SCRIPT_CONTRACT-shaped dict (status + probe-specific fields).
The fleet-health orchestrator (`ops/scripts/fleet-health.py`) invokes
those probes via `ops/scripts/probe-agent.py` and aggregates the
results into `<brain>/fleet-health.json`, the single authoritative
source of per-agent health.

This base class consolidates the thin wrapping that every probe
shares: a `main()` that prints one JSON line to stdout and always
exits 0, catching probe exceptions as error JSON. That's all.

Subclasses override:
  - AGENT_ID / TITLE / EMOJI
  - probe() -> dict

History: the base class used to also carry `run()`, `_write_status_md()`,
`render_status_md()`, and an `output_file` property that wrote a
per-agent `<id>.status.md` file under the brain. Those paths were
retired with fleet-health.json becoming authoritative — the writes
were always a no-op on the fleet-health path (which bypasses `run()`
by design) and the only consumer (`scripts/obsidian-briefing/
parse_agent_status`) was migrated to read fleet-health.json directly.

Conforms to agents/shared/SCRIPT_CONTRACT.md.
"""
from __future__ import annotations

import json
import traceback


class HeartbeatProbe:
    AGENT_ID: str = ""
    TITLE: str = ""
    EMOJI: str = "\u26a0\ufe0f"

    def probe(self) -> dict:
        raise NotImplementedError("subclasses must implement probe()")

    def main(self) -> int:
        try:
            result = self.probe()
        except Exception as e:
            result = {
                "status": "error",
                "error": str(e),
                "alert": f"{self.EMOJI} {self.AGENT_ID} heartbeat crashed: {e}",
                "traceback": traceback.format_exc().splitlines()[-3:],
            }
        print(json.dumps(result))
        return 0
