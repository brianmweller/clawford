"""HeartbeatProbe — base class for per-agent heartbeat probes.

Consolidates the scaffolding shared by every agent's
`scripts/heartbeat.py`: a pure `probe()` that returns a result dict,
a `run()` that calls probe and atomically writes the per-agent
`<agent>.status.md` file under the brain, and a `main()` wrapper that
conforms to SCRIPT_CONTRACT (always exit 0, single JSON line on
stdout, crash caught and emitted as error JSON with the agent emoji).

Subclasses override:
  - AGENT_ID: lower-kebab identifier, e.g. "news-digest"
  - TITLE: human-readable title, e.g. "News Digest"
  - EMOJI: alert prefix, e.g. "🐛"
  - probe() -> dict: the actual health-check logic
  - render_status_md(result) -> str: the agent-specific markdown body

The fleet-health.py orchestrator (R3) calls `probe()` directly via
`docker exec`, bypassing `run()` so no `.status.md` side effects fire
during the */15 tick. `run()` is kept for standalone invocation from
the agent's own heartbeat cron during the R6 transition period.

Conforms to agents/shared/SCRIPT_CONTRACT.md.
"""
from __future__ import annotations

import json
import os
import traceback


_DEFAULT_BRAIN_DIR = os.path.expanduser("~/Dropbox/openclaw-backup")


class HeartbeatProbe:
    AGENT_ID: str = ""
    TITLE: str = ""
    EMOJI: str = "⚠️"

    def __init__(self, brain_dir: str | None = None) -> None:
        self.brain_dir = brain_dir if brain_dir is not None else _DEFAULT_BRAIN_DIR

    @property
    def output_file(self) -> str:
        return os.path.join(self.brain_dir, "agents", f"{self.AGENT_ID}.status.md")

    def probe(self) -> dict:
        raise NotImplementedError("subclasses must implement probe()")

    def render_status_md(self, result: dict) -> str:
        raise NotImplementedError("subclasses must implement render_status_md()")

    def run(self) -> dict:
        result = self.probe()
        self._write_status_md(result)
        return result

    def _write_status_md(self, result: dict) -> None:
        content = self.render_status_md(result)
        path = self.output_file
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception as e:
            raise RuntimeError(f"status file write failed: {e}") from e

    def main(self) -> int:
        try:
            result = self.run()
        except Exception as e:
            result = {
                "status": "error",
                "error": str(e),
                "alert": f"{self.EMOJI} {self.AGENT_ID} heartbeat crashed: {e}",
                "traceback": traceback.format_exc().splitlines()[-3:],
            }
        print(json.dumps(result))
        return 0
