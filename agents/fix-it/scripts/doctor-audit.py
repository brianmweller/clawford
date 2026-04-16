#!/usr/bin/env python3
"""doctor-audit.py — Mr Fixit's cognitive heartbeat (P0.2).

Every 30 minutes, scans every agent in the fleet for *semantic* drift
(distinct from `fleet-health.py`'s liveness check):

  - Has the agent's MEMORY.md accumulated rules that contradict its
    SOUL.md?
  - Are recent cron outputs producing the same degraded result over
    and over (suggesting a stuck token, broken workflow, or off-role
    behavior)?
  - Has the agent's behavior drifted away from its declared role?

For each agent, the script reads the brain-stored SOUL + MEMORY +
the agent's fleet-health probe block, then asks an LLM to identify
anomalies and rate severity 0–3. Findings above the threshold:

  - Append to ~/Dropbox/openclaw-backup/fix-it/drift-audit.md
  - In `--alert` mode, send a Telegram message via Mr Fixit's bot
  - When the suggested action maps to one of Mr Fixit's write-capable
    tools (refresh_session, snooze_alert, rerun_cron — see commit
    b92410f), include the proposal so the operator can confirm with
    one tap

Default mode is `--report-only` (no Telegram, no proposals) for
the first week of rollout. After the warn-stream stabilizes, flip
to `--alert`.

CLI:
    python3 doctor-audit.py              # report-only (default)
    python3 doctor-audit.py --alert      # send Telegram + propose
    python3 doctor-audit.py --severity 3 # only alert on severity >=3

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared import brain  # type: ignore  # noqa: E402
from agents.shared import llm  # type: ignore  # noqa: E402

DRIFT_AUDIT_REL = "fix-it/drift-audit.md"
FLEET_MANIFEST_REL = "agents/shared/fleet-manifest.json"
DEFAULT_SEVERITY_THRESHOLD = 2  # warn or higher
LLM_TIMEOUT_S = 60
MAX_MEMORY_CHARS = 4_000  # truncate huge MEMORY files for prompt budget
MAX_SOUL_CHARS = 2_500


SYSTEM_PROMPT = """You are a drift auditor for the Clawford agent fleet.
You read one agent's declared role (SOUL), accumulated rules (MEMORY),
and recent health-probe data, and identify *anomalies* — patterns
that suggest the agent is drifting from its role, accumulating
contradictory rules, or repeatedly failing in the same way.

Reply with valid JSON ONLY (no markdown fences, no prose):

{
  "anomalies": [
    {
      "kind": "drift" | "contradiction" | "repeat_failure" | "stale_session" | "other",
      "severity": 0,
      "summary": "one sentence",
      "evidence": "what you specifically observed",
      "suggested_action": "refresh_session" | "snooze_alert" | "rerun_cron" | "review_memory" | "none",
      "suggested_target": ""
    }
  ]
}

Severity scale:
  0 = informational only (don't alert)
  1 = mild note (don't alert)
  2 = warn — operator should look this week
  3 = alert — operator should look now

Strict rules:
  - When in doubt, OMIT the anomaly. False positives waste the
    operator's time and erode trust in this auditor.
  - Do not invent issues to fill the list. Empty anomalies array
    is the correct answer most of the time.
  - "suggested_target" is optional context: vendor name for
    refresh_session, cron name for rerun_cron, etc.
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> dict:
    args = {
        "alert": False,
        "report_only": True,
        "severity": DEFAULT_SEVERITY_THRESHOLD,
        "agent_filter": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--alert":
            args["alert"] = True
            args["report_only"] = False
        elif a == "--report-only":
            args["report_only"] = True
            args["alert"] = False
        elif a == "--severity" and i + 1 < len(argv):
            try:
                args["severity"] = int(argv[i + 1])
            except ValueError:
                pass
            i += 1
        elif a == "--agent" and i + 1 < len(argv):
            args["agent_filter"] = argv[i + 1]
            i += 1
        i += 1
    return args


# ---------------------------------------------------------------------------
# Inputs — read brain + fleet-health
# ---------------------------------------------------------------------------


def load_fleet_agents(repo_root: Path | None = None) -> list[dict]:
    """Return the list of agent metadata dicts from fleet-manifest.json.

    When run from the repo (parents[3] = repo root) or from a per-
    agent workspace (parents[3] != repo root, but deploy.py mirrors
    fleet-manifest.json into each workspace's agents/shared/), walk
    ancestors looking for the file directly. Works in both layouts
    without assuming either.
    """
    if repo_root is not None:
        path = repo_root / FLEET_MANIFEST_REL.replace("/", os.sep)
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("agents", [])

    rel = FLEET_MANIFEST_REL.replace("/", os.sep)
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / rel
        if candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return data.get("agents", [])
    raise FileNotFoundError(
        f"fleet-manifest.json not found in any ancestor of {Path(__file__).resolve()}"
    )


def load_agent_doc(agent_id: str, filename: str, max_chars: int) -> str:
    """Read a brain-stored doc for an agent, truncated to max_chars."""
    try:
        path = brain.agent_config_path(agent_id, filename)
    except ValueError:
        return ""
    if not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(raw) <= max_chars:
        return raw
    return raw[: max_chars - 30] + "\n…[truncated]"


def load_agent_health_block(agent_id: str) -> dict:
    """Return the per-agent block from fleet-health.json, or an empty
    dict if fleet-health.json is missing or doesn't include this agent."""
    try:
        fh = brain.read_fleet_health()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return (fh.get("agents") or {}).get(agent_id, {})


# ---------------------------------------------------------------------------
# Prompt + LLM
# ---------------------------------------------------------------------------


def build_audit_prompt(
    *,
    agent_id: str,
    display_name: str,
    soul: str,
    memory: str,
    health_block: dict,
) -> str:
    health_summary = json.dumps(health_block, indent=2) if health_block else "(no health data)"
    # The literal token "json" must appear in the user message — OpenAI's
    # Responses API rejects json_mode=True requests whose input doesn't
    # contain it (HTTP 400 "input messages must contain the word 'json'").
    return (
        f"Agent: {agent_id} ({display_name})\n\n"
        f"=== SOUL.md (declared role) ===\n{soul or '(missing)'}\n\n"
        f"=== MEMORY.md (accumulated rules) ===\n{memory or '(empty)'}\n\n"
        f"=== fleet-health probe block (current state) ===\n{health_summary}\n\n"
        f"Audit this agent for drift signals. Reply with valid json "
        f"matching the schema in your instructions; an empty anomalies "
        f"array is the right answer when nothing's wrong.\n"
    )


def audit_one_agent(
    agent: dict,
    *,
    infer_fn=None,
    timeout: int = LLM_TIMEOUT_S,
) -> dict:
    """Run the drift audit for one agent. Returns a dict like:

      {"agent_id": "...", "ok": True, "anomalies": [...]}
      {"agent_id": "...", "ok": False, "error": "..."}

    infer_fn is injected for tests so this is exercisable offline.
    """
    agent_id = agent.get("id", "")
    display_name = agent.get("display_name", agent_id)
    soul = load_agent_doc(agent_id, "SOUL.md", MAX_SOUL_CHARS)
    memory = load_agent_doc(agent_id, "MEMORY.md", MAX_MEMORY_CHARS)
    health = load_agent_health_block(agent_id)

    prompt = build_audit_prompt(
        agent_id=agent_id, display_name=display_name,
        soul=soul, memory=memory, health_block=health,
    )

    if infer_fn is None:
        infer_fn = llm.infer

    result = infer_fn(
        prompt,
        instructions=SYSTEM_PROMPT,
        json_mode=True,
        timeout=timeout,
    )

    if not getattr(result, "ok", False):
        return {
            "agent_id": agent_id,
            "ok": False,
            "error": getattr(result, "error", "") or "llm call failed",
        }

    text = (getattr(result, "text", "") or "").strip()
    try:
        body = json.loads(text)
    except json.JSONDecodeError as e:
        return {
            "agent_id": agent_id, "ok": False,
            "error": f"unparseable LLM JSON: {e}; head={text[:120]!r}",
        }

    anomalies = body.get("anomalies") or []
    if not isinstance(anomalies, list):
        anomalies = []
    # Filter / coerce per-anomaly shape so a sloppy reply doesn't crash
    # downstream code.
    cleaned = []
    for a in anomalies:
        if not isinstance(a, dict):
            continue
        cleaned.append({
            "kind": a.get("kind", "other"),
            "severity": int(a.get("severity", 0)) if str(a.get("severity", 0)).isdigit() else 0,
            "summary": str(a.get("summary", ""))[:300],
            "evidence": str(a.get("evidence", ""))[:600],
            "suggested_action": a.get("suggested_action", "none"),
            "suggested_target": str(a.get("suggested_target", ""))[:120],
        })
    return {"agent_id": agent_id, "ok": True, "anomalies": cleaned}


# ---------------------------------------------------------------------------
# Output — append to drift-audit.md, optional Telegram alert
# ---------------------------------------------------------------------------


def append_audit_entry(audits: list[dict], threshold: int) -> tuple[str, int]:
    """Append one entry to drift-audit.md describing this run.
    Returns (entry_text, count_of_anomalies_at_or_above_threshold)."""
    now = datetime.now(timezone.utc).isoformat()
    lines = [f"\n## {now}", ""]
    flagged_total = 0
    for audit in audits:
        agent_id = audit["agent_id"]
        if not audit.get("ok"):
            lines.append(f"- **{agent_id}**: ⚠️ audit failed — {audit.get('error', '?')}")
            continue
        flagged = [a for a in audit["anomalies"] if a["severity"] >= threshold]
        if not flagged:
            lines.append(f"- **{agent_id}**: clean")
            continue
        flagged_total += len(flagged)
        lines.append(f"- **{agent_id}**: {len(flagged)} anomaly(ies) at severity >= {threshold}")
        for a in flagged:
            lines.append(
                f"    - [{a['severity']}] {a['kind']} — {a['summary']}"
            )
            if a["evidence"]:
                lines.append(f"        *evidence:* {a['evidence']}")
            if a["suggested_action"] not in ("", "none"):
                target = f" ({a['suggested_target']})" if a["suggested_target"] else ""
                lines.append(
                    f"        *suggested action:* `{a['suggested_action']}`{target}"
                )
    lines.append("")
    entry = "\n".join(lines)
    brain.append_text(DRIFT_AUDIT_REL, entry)
    return entry, flagged_total


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def run(argv: list[str]) -> dict:
    args = parse_args(argv)

    fleet = load_fleet_agents()
    if args["agent_filter"]:
        fleet = [a for a in fleet if a.get("id") == args["agent_filter"]]

    audits: list[dict] = []
    for agent in fleet:
        audits.append(audit_one_agent(agent))

    entry, flagged_total = append_audit_entry(audits, threshold=args["severity"])

    summary = {
        "status": "ok",
        "agents_audited": len(audits),
        "anomalies_above_threshold": flagged_total,
        "severity_threshold": args["severity"],
        "mode": "alert" if args["alert"] else "report-only",
        "drift_audit_path": str(brain.dropbox_brain_root() / DRIFT_AUDIT_REL),
    }

    # Alert mode currently just signals intent — wiring the actual
    # Telegram send + proposed-action gate is incremental work that
    # depends on the operator confirming the warn-stream is sound.
    # The entry is on-disk regardless; an operator pulling
    # drift-audit.md sees everything.
    if args["alert"] and flagged_total > 0:
        summary["alert_text"] = entry.strip()

    return summary


def main() -> int:
    try:
        result = run(sys.argv[1:])
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
