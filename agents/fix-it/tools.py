"""agents/fix-it/tools.py — Mr Fixit's tool manifest for the inbox dispatcher.

Read tools (pure):
  - get_fleet_health, get_morning_status, get_known_issues

Write tools (propose → Telegram button → confirm executor):
  - propose_remember / confirm_remember           (MEMORY.md append)
  - propose_snooze_alert / confirm_snooze_alert   (KNOWN_ISSUES.md append)
  - propose_refresh_session / confirm_refresh_session (Costco/Google reauth)
  - propose_rerun_cron / confirm_rerun_cron       (re-fire a host cron)

Confirm executors are NOT in the TOOLS manifest — the dispatcher
shortcut path invokes them directly from a callback_data="confirm:<id>"
button tap. P1–P4 diagnostic discipline (see SOUL.md) applies BEFORE
staging any propose; cite evidence first.

Contract:
  TOOLS → list[dict] in OpenAI Responses tool-manifest shape
  EXECUTORS → dict[tool_name -> callable] used by tool_use.run()
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The dispatcher exec_module's this file from agents/fix-it/ but does not
# add that directory to sys.path. Self-bootstrap so sibling-module imports
# (`_cron_lookup`) resolve. shared/ is already on the path via the dispatcher.
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import memory_writer  # type: ignore
import pending_actions  # type: ignore

import _cron_lookup  # type: ignore

try:
    from agents.shared import google_oauth  # type: ignore
except ImportError:  # pragma: no cover — runtime path on the VPS
    import google_oauth  # type: ignore

AGENT_ID = "fix-it"
BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
FLEET_HEALTH_PATH = os.path.join(BRAIN, "fleet-health.json")
KNOWN_ISSUES_PATH = os.path.join(BRAIN, "fix-it", "KNOWN_ISSUES.md")
MORNING_STATUS_PATH = os.path.expanduser(
    "~/.clawford/fix-it-workspace/cache/morning-brief-ready.txt"
)


def get_fleet_health() -> dict:
    """Read fleet-health.json and return a terse summary dict.

    Returns a dict with {generated_at, status, agents_ok, agents_checked,
    degraded_agents, alert}. Keys mirror the format fleet-health.py writes.
    """
    if not os.path.exists(FLEET_HEALTH_PATH):
        return {"error": "fleet-health.json missing", "path": FLEET_HEALTH_PATH}
    try:
        with open(FLEET_HEALTH_PATH, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"fleet-health.json unreadable: {exc}"}

    summary = {
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "agents_checked": report.get("agents_checked"),
        "agents_ok": report.get("agents_ok"),
        "agents_degraded": report.get("agents_degraded", []),
        "alert": report.get("alert", ""),
    }
    generated = report.get("generated_at", "")
    if generated:
        try:
            ts = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            age_min = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
            summary["age_minutes"] = age_min
        except (ValueError, AttributeError):
            pass

    per_agent = report.get("per_agent") or report.get("agents")
    if per_agent:
        summary["per_agent"] = per_agent

    return summary


def get_morning_status() -> str:
    """Read the most recent morning status brief, if one exists."""
    if not os.path.exists(MORNING_STATUS_PATH):
        return "no morning status available yet"
    try:
        return Path(MORNING_STATUS_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        return f"morning status unreadable: {exc}"


def get_known_issues() -> str:
    """Read KNOWN_ISSUES.md — the operator-curated suppression list."""
    if not os.path.exists(KNOWN_ISSUES_PATH):
        return "no KNOWN_ISSUES.md file"
    try:
        return Path(KNOWN_ISSUES_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        return f"KNOWN_ISSUES.md unreadable: {exc}"


def propose_remember(rule: str, category: str = "General") -> dict:
    """Stage a memory-write for confirmation. Auto-attaches buttons."""
    return memory_writer.propose_pending_remember(AGENT_ID, rule, category)


def confirm_remember(rule: str, category: str) -> dict:
    """Execute the staged memory-write. Called by dispatcher shortcut only."""
    return memory_writer.append_rule(AGENT_ID, rule, category)


# ───── snooze_alert ──────────────────────────────────────────────────

SNOOZE_MAX_HOURS = 168  # 7d cap — prevents accidental permanent suppression
_OVERBROAD_SNOOZE_RE = re.compile(r"^\.\*\??$")


def propose_snooze_alert(pattern: str, hours: int, reason: str) -> dict:
    """Stage a KNOWN_ISSUES.md snooze entry for the operator's confirmation.

    expires is computed at confirm time, not stage time, so a slept-on
    confirmation gets its full window starting from the tap.
    """
    try:
        re.compile(pattern)
    except re.error as exc:
        return {"status": "error", "error": f"invalid regex: {exc}"}

    if _OVERBROAD_SNOOZE_RE.match(pattern):
        return {
            "status": "error",
            "error": "pattern too broad — snoozes everything; tighten and retry",
        }

    if len(re.sub(r"[.*+?\\(){}[\]|^$]", "", pattern)) < 4:
        return {
            "status": "error",
            "error": "pattern too short (<4 non-meta chars); risk of over-broad mask",
        }

    if hours <= 0 or hours > SNOOZE_MAX_HOURS:
        return {
            "status": "error",
            "error": f"hours must be 1..{SNOOZE_MAX_HOURS} (7d cap)",
        }

    summary = f"Snooze alerts matching /{pattern}/ for {hours}h. Reason: {reason}"
    return pending_actions.stage(
        AGENT_ID,
        "snooze_alert",
        payload={"pattern": pattern, "hours": hours, "reason": reason},
        summary=summary,
        confirm_label="\U0001f507 Snooze",
        cancel_label="Skip",
        ttl_hours=1,
    )


def confirm_snooze_alert(pattern: str, hours: int, reason: str) -> dict:
    """Append a parseable block to KNOWN_ISSUES.md. Atomic write."""
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d")
    block = (
        "\n---\n"
        f"- **match:** {pattern}\n"
        f"- **expires:** {expires}\n"
        f"- **reason:** {reason}\n"
        f"- **escalation:** auto-expire on {expires}\n"
    )
    target = Path(KNOWN_ISSUES_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(existing + block)
    os.replace(tmp, target)
    return {"status": "ok", "expires": expires, "path": str(target)}


# ───── refresh_session ───────────────────────────────────────────────

_REFRESH_SOURCES = ("costco", "google")
_GOOGLE_TOKEN_PATHS = [
    os.path.expanduser("~/.clawford/family-calendar-workspace/token.json"),
    os.path.expanduser("~/.clawford/meetings-coach-workspace/token.json"),
]
_COSTCO_REFRESH_SCRIPT = str(
    Path(__file__).resolve().parents[1]
    / "shopping" / "scripts" / "costco_refresh_headless.py"
)


def propose_refresh_session(source: str) -> dict:
    """Stage a headless session refresh. source ∈ {'costco', 'google'}."""
    if source not in _REFRESH_SOURCES:
        return {
            "status": "error",
            "error": (
                f"unknown source '{source}'. Supported: {', '.join(_REFRESH_SOURCES)}. "
                "Amazon has no headless refresh entrypoint yet."
            ),
        }
    summary = f"Refresh {source} session headlessly"
    return pending_actions.stage(
        AGENT_ID,
        "refresh_session",
        payload={"source": source},
        summary=summary,
        confirm_label="\U0001f504 Refresh",
        cancel_label="Skip",
        ttl_hours=2,
    )


def confirm_refresh_session(source: str) -> dict:
    """Run the headless refresh for source. Returns per-token status."""
    if source == "costco":
        result = subprocess.run(
            ["python3", _COSTCO_REFRESH_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return {"status": "ok", "stdout_tail": result.stdout[-1000:]}
        return {
            "status": "error",
            "rc": result.returncode,
            "error": (result.stderr or result.stdout)[-1000:],
        }
    if source == "google":
        results = []
        for token_path in _GOOGLE_TOKEN_PATHS:
            try:
                refreshed = google_oauth.refresh_if_stale(
                    token_path, max_age_days=0, scopes=[],
                )
                results.append({"token": token_path, "refreshed": bool(refreshed)})
            except Exception as exc:
                results.append({"token": token_path, "error": str(exc)})
        any_ok = any(r.get("refreshed") for r in results)
        return {
            "status": "ok" if any_ok else "error",
            "results": results,
        }
    return {"status": "error", "error": f"unsupported source: {source}"}


# ───── rerun_cron ────────────────────────────────────────────────────


def propose_rerun_cron(name: str, reason: str) -> dict:
    """Stage a cron re-execution. ANY cron — but the summary carries
    the explicit double-execution warning so the operator sees the risk on
    the confirm prompt."""
    cron = _cron_lookup.lookup_cron(name)
    if cron is None:
        return {
            "status": "error",
            "error": f"no cron named '{name}' in install-host-cron.sh or any expected-crons.json",
        }
    schedule = cron.get("schedule", "?")
    summary = (
        f"\u26a0\ufe0f Re-run '{name}' ({schedule}) \u2014 "
        f"may double-execute side effects (e.g. duplicate Telegram delivery, "
        f"duplicate writes). Reason: {reason}"
    )
    payload = {
        "name": name,
        "kind": cron["kind"],
        "contract": cron.get("contract", False),
        "wrapper_path": str(cron.get("wrapper_path", "")),
        "schedule": schedule,
        "reason": reason,
    }
    if cron.get("contract"):
        payload["script_path"] = cron.get("script_path", "")
        payload["token_env"] = cron.get("token_env", "")
        payload["timeout_s"] = cron.get("timeout_s", "")
    return pending_actions.stage(
        AGENT_ID,
        "rerun_cron",
        payload=payload,
        summary=summary,
        confirm_label="\u26a0\ufe0f Re-run anyway",
        cancel_label="Skip",
        ttl_hours=2,
    )


def confirm_rerun_cron(
    name: str,
    kind: str,
    contract: bool,
    wrapper_path: str,
    schedule: str,
    reason: str,
    script_path: str = "",
    token_env: str = "",
    timeout_s: str = "",
) -> dict:
    """Re-fire the cron. host+contract → script-contract-host.sh with args;
    host+direct → wrapper directly; llm → error (no runner exists)."""
    if kind == "llm":
        return {
            "status": "error",
            "error": (
                "LLM crons retired post-Phase-7 — no runner available. "
                "Convert to a host cron in ops/scripts/install-host-cron.sh first."
            ),
        }
    if kind != "host":
        return {"status": "error", "error": f"unknown cron kind: {kind}"}

    if contract:
        argv = [wrapper_path, name, script_path, token_env, timeout_s]
    else:
        argv = [wrapper_path]

    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        return {"status": "error", "error": f"timeout after {exc.timeout}s"}
    except (FileNotFoundError, OSError) as exc:
        return {"status": "error", "error": f"could not invoke wrapper: {exc}"}

    out = {
        "status": "ok" if result.returncode == 0 else "error",
        "rc": result.returncode,
        "stdout_tail": (result.stdout or "")[-1000:],
        "stderr_tail": (result.stderr or "")[-1000:],
    }
    if result.returncode != 0:
        out["error"] = f"wrapper exited {result.returncode}"
    return out


TOOLS: list[dict] = [
    {
        "type": "function",
        "name": "get_fleet_health",
        "description": (
            "Read the current fleet-health.json snapshot. Returns a dict "
            "with overall status, per-agent health, and how old the "
            "snapshot is in minutes. Call this when the operator asks about "
            "fleet status, agent health, or what's alerting."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_morning_status",
        "description": (
            "Read the most recent morning status brief (5 AM PT / 12 UTC "
            "daily report). Returns the full markdown string. Call this "
            "when the operator asks for the morning briefing or what the overnight "
            "system state looked like."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_known_issues",
        "description": (
            "Read the operator-curated KNOWN_ISSUES.md file. This lists "
            "known-but-not-yet-fixed issues and their expiry dates — "
            "anything in this file is intentionally being ignored by Mr "
            "Fixit's alert classifier. Call this when the operator asks what's "
            "being suppressed or what's a 'known issue'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "propose_remember",
        "description": (
            "Stage a rule for the operator's confirmation, to be added to "
            "your persistent MEMORY.md. the operator will see inline buttons "
            "to remember or skip. Use when the operator says 'remember that...', "
            "'from now on...', or teaches you a new rule."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The rule to remember"},
                "category": {"type": "string", "description": "Category heading (e.g. 'Alert Classification')"},
            },
            "required": ["rule"],
        },
    },
    {
        "type": "function",
        "name": "propose_snooze_alert",
        "description": (
            "Stage a regex+expiry suppression to KNOWN_ISSUES.md so the "
            "morning-status classifier treats matching alerts as 'known' "
            "until expires. Use when an alert is real-but-already-tracked "
            "or transiently noisy. Cite the symptom you saw before "
            "snoozing — diagnostic discipline applies. Max 168h (7d). "
            "Reason is mandatory; it goes into the audit trail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Case-insensitive regex matched against alert text. >=4 non-meta chars; '.*' rejected.",
                },
                "hours": {
                    "type": "integer",
                    "description": "Snooze duration in hours. 1..168.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this is being snoozed (audit trail).",
                },
            },
            "required": ["pattern", "hours", "reason"],
        },
    },
    {
        "type": "function",
        "name": "propose_refresh_session",
        "description": (
            "Stage a headless session refresh for an external service. "
            "Sources: 'costco' (Hilda Hippo's Costco JWT) or 'google' "
            "(family-calendar + meetings-coach OAuth tokens). Use when "
            "fleet-health.json or a recent alert points to an expired "
            "session. Verify the symptom with get_fleet_health first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["costco", "google"],
                    "description": "Which session to refresh.",
                },
            },
            "required": ["source"],
        },
    },
    {
        "type": "function",
        "name": "propose_rerun_cron",
        "description": (
            "Stage a re-execution of a host cron by name. Looks the cron "
            "up in install-host-cron.sh (DIRECT or CONTRACT entries). "
            "WARNING: rerunning a cron may double-execute its side "
            "effects (duplicate Telegram delivery, duplicate writes). The "
            "confirm prompt always carries this warning. Use when a "
            "scheduled run silently failed or was missed; cite evidence "
            "(crontab -l, log tail, fleet-health) before staging."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Cron name (e.g. 'fix-it-conflict-scan', 'morning-fleet-deliver-host', 'costco-token-refresh-host').",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this rerun is needed (audit trail; appears in confirm summary).",
                },
            },
            "required": ["name", "reason"],
        },
    },
]


EXECUTORS: dict = {
    "get_fleet_health": get_fleet_health,
    "get_morning_status": get_morning_status,
    "get_known_issues": get_known_issues,
    "propose_remember": propose_remember,
    "propose_snooze_alert": propose_snooze_alert,
    "propose_refresh_session": propose_refresh_session,
    "propose_rerun_cron": propose_rerun_cron,
    # Shortcut-only (not in TOOLS manifest):
    "confirm_remember": confirm_remember,
    "confirm_snooze_alert": confirm_snooze_alert,
    "confirm_refresh_session": confirm_refresh_session,
    "confirm_rerun_cron": confirm_rerun_cron,
}
