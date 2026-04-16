#!/usr/bin/env python3
"""morning-status.py — Mr Fixit's daily fleet health report.

R4 of the registry-based health system. Replaces the LLM-driven
fix-it/morning-status cron with a pure Python script that:

  1. Reads ~/Dropbox/openclaw-backup/fleet-health.json (R3 output)
  2. Reads ~/Dropbox/openclaw-backup/fix-it/KNOWN_ISSUES.md
  3. Runs ~/Dropbox/openclaw-backup/scripts/validate.py and parses
     the summary line
  4. Finds Dropbox conflicted-copy files via shell `find`
  5. Classifies each agent into one of 5 buckets using the same
     6-rule precedence the LLM cron used:
       (a) fleet-health stale (> 6h old)        → DOWN
       (b) status == "ok"                       → HEALTHY
       (c) status == degraded + KNOWN match     → KNOWN
       (d) status == degraded + heartbeat stale → STALE
       (e) status == degraded + fresh           → OPEN ALERT
       (f) default                              → HEALTHY
  6. Formats the emoji-headed report
  7. Writes ~/.clawford/fix-it-workspace/cache/morning-brief-ready.txt
     for the morning-fleet-deliver cron to pick up at 12:00 UTC

The LLM-cron-message classification rules and report format are
preserved byte-for-byte where possible. Per-rule unit tests in
tests/test_morning_status.py cover each bucket transition.

Conforms to agents/shared/SCRIPT_CONTRACT.md: always exits 0,
prints one JSON line to stdout. Run via host cron — does NOT need
docker exec since fleet-health.json + KNOWN_ISSUES.md + the
validate.py script all live on the host filesystem (Dropbox bind
mount + repo bind mount).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


# These constants are module-level so tests can monkeypatch them.
BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
FLEET_HEALTH_PATH = os.path.join(BRAIN, "fleet-health.json")
KNOWN_ISSUES_PATH = os.path.join(BRAIN, "fix-it", "KNOWN_ISSUES.md")
VALIDATE_PY = os.path.join(BRAIN, "scripts", "validate.py")
OUTPUT_PATH = os.path.expanduser(
    "~/.clawford/fix-it-workspace/cache/morning-brief-ready.txt"
)

FLEET_HEALTH_STALE_HOURS = 6


@dataclass
class KnownIssue:
    pattern: str       # regex string
    expires: str       # YYYY-MM-DD
    reason: str
    escalation: str = ""

    def is_expired(self, today: datetime) -> bool:
        try:
            exp_dt = datetime.strptime(self.expires, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return exp_dt < today
        except ValueError:
            return False  # unparseable expires keeps the entry alive


def parse_known_issues(text: str) -> list[KnownIssue]:
    """Parse KNOWN_ISSUES.md into a list of KnownIssue records.

    Format (separated by --- between entries):
      - **match:** <regex>
      - **expires:** YYYY-MM-DD
      - **reason:** <free text>
      - **escalation:** <free text, optional>
    """
    issues: list[KnownIssue] = []
    blocks = re.split(r"\n\s*---+\s*\n", text)
    for block in blocks:
        m_match = re.search(r"\*\*match:\*\*\s*(.+)", block)
        m_expires = re.search(r"\*\*expires:\*\*\s*(\S+)", block)
        m_reason = re.search(r"\*\*reason:\*\*\s*(.+)", block)
        m_escalation = re.search(r"\*\*escalation:\*\*\s*(.+)", block)
        if not (m_match and m_expires and m_reason):
            continue
        issues.append(KnownIssue(
            pattern=m_match.group(1).strip(),
            expires=m_expires.group(1).strip(),
            reason=m_reason.group(1).strip(),
            escalation=m_escalation.group(1).strip() if m_escalation else "",
        ))
    return issues


def match_known_issue(issues: list[KnownIssue], text: str) -> KnownIssue | None:
    """Return the first non-expired KnownIssue whose regex matches the
    given text. None means "no suppression — treat as a real alert"."""
    today = datetime.now(timezone.utc)
    for issue in issues:
        if issue.is_expired(today):
            continue
        try:
            if re.search(issue.pattern, text, re.IGNORECASE):
                return issue
        except re.error:
            continue  # malformed regex — skip
    return None


def _run_validate() -> tuple[str, int, int, int]:
    """Invoke validate.py and parse its summary line:
      'PASS: N | FAIL: N | WARN: N'
    Returns (status, pass_count, fail_count, warn_count) or
    ('ERROR', 0, 0, 0) on any failure.
    """
    try:
        result = subprocess.run(
            ["python3", VALIDATE_PY],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = result.stdout + result.stderr
    except Exception:
        return ("ERROR", 0, 0, 0)

    m = re.search(r"PASS:\s*(\d+)\s*\|\s*FAIL:\s*(\d+)\s*\|\s*WARN:\s*(\d+)", out)
    if not m:
        return ("ERROR", 0, 0, 0)
    p, f, w = int(m.group(1)), int(m.group(2)), int(m.group(3))
    overall = "PASS" if f == 0 else "FAIL"
    return (overall, p, f, w)


def _find_conflicted_copies() -> list[str]:
    """Return a list of Dropbox conflicted-copy file paths (relative to BRAIN)."""
    try:
        result = subprocess.run(
            ["find", BRAIN, "-name", "*conflicted copy*", "-type", "f"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return [
            line.replace(BRAIN + "/", "")
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except Exception:
        return []


def _classify_agent(
    agent_id: str,
    agent_report: dict,
    known_issues: list[KnownIssue],
    fleet_stale: bool,
) -> tuple[str, str]:
    """Return (bucket, detail) for one agent. Buckets:
      'down'    — fleet-health.json is too old (rule a)
      'healthy' — status == ok (rule b)
      'known'   — degraded + KNOWN_ISSUES match (rule c)
      'stale'   — degraded + heartbeat too old (rule d) — currently
                  unused since fleet-health.json freshness is global
      'alert'   — degraded + fresh + no match (rule e)
    """
    if fleet_stale:
        return "down", "fleet-health.json > 6h old"

    status = agent_report.get("status", "error")
    if status == "ok":
        return "healthy", ""

    # status is degraded/error/something — look for known-issue suppression
    alert_text = agent_report.get("alert") or agent_report.get("error") or ""
    matched = match_known_issue(known_issues, alert_text)
    if matched:
        return "known", matched.reason

    return "alert", alert_text


def _format_age(age: timedelta) -> str:
    """Render a timedelta as a compact 'Xh Ym' / 'Ym' string."""
    total_min = int(age.total_seconds() // 60)
    if total_min < 0:
        total_min = 0
    hours, minutes = divmod(total_min, 60)
    if hours:
        return f"{hours}h {minutes}m ago"
    return f"{minutes}m ago"


def _format_report(
    overall: str,
    buckets: dict[str, str],
    agent_alerts: dict[str, str],
    known_reasons: dict[str, str],
    validate_result: tuple,
    conflicts: list[str],
    generated_at: datetime,
    age: timedelta,
) -> str:
    """Render the morning status report in the existing emoji-headed format."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    overall_label = {
        "down": "🚨 fleet down",
        "alert": f"🚨 {sum(1 for b in buckets.values() if b == 'alert')} open alerts",
        "stale": f"⚠️ {sum(1 for b in buckets.values() if b == 'stale')} stale",
        "known": f"ℹ️ {sum(1 for b in buckets.values() if b == 'known')} known",
        "healthy": "✅ all clear",
    }.get(overall, "✅ all clear")

    lines = [
        f"🦊🔧 Morning Status — {now}",
        "",
        f"Overall: {overall_label}",
        "",
        "Agents:",
    ]
    bucket_emoji = {
        "down": "🚨",
        "alert": "🚨",
        "stale": "⚠️",
        "known": "ℹ️",
        "healthy": "✅",
    }
    for agent_id in sorted(buckets.keys()):
        bucket = buckets[agent_id]
        emoji = bucket_emoji.get(bucket, "✅")
        lines.append(f"  {emoji} {agent_id} — {bucket}")

    lines.append("")

    v_status, v_pass, v_fail, v_warn = validate_result
    lines.append(f"Brain: {v_status} ({v_pass} pass, {v_fail} fail, {v_warn} warn)")
    lines.append(
        f"Dropbox: {'no conflicts' if not conflicts else f'{len(conflicts)} conflicted copies'}"
    )
    lines.append(
        f"Fleet-health: generated {generated_at.strftime('%H:%M UTC')} ({_format_age(age)})"
    )

    # 🚨 Open alerts section
    alerts_in_bucket = [a for a, b in buckets.items() if b == "alert"]
    if alerts_in_bucket:
        lines.append("")
        lines.append("🚨 Open alerts:")
        for agent_id in sorted(alerts_in_bucket):
            lines.append(f"  - {agent_id}: {agent_alerts.get(agent_id, '(no alert text)')}")

    # ℹ️ Known section
    known_agents = [a for a, b in buckets.items() if b == "known"]
    if known_agents:
        lines.append("")
        lines.append("ℹ️ Known (pending):")
        for agent_id in sorted(known_agents):
            lines.append(f"  - {agent_id}: {known_reasons.get(agent_id, '')}")

    # 🚨 Down section (if fleet-health stale)
    down_agents = [a for a, b in buckets.items() if b == "down"]
    if down_agents:
        lines.append("")
        lines.append("🚨 Fleet down:")
        lines.append("  fleet-health.json is > 6h old. fleet-health-host.sh may be broken.")

    return "\n".join(lines) + "\n"


def run() -> dict:
    """Read inputs, classify, write the morning brief, return summary."""
    if not os.path.exists(FLEET_HEALTH_PATH):
        raise FileNotFoundError(f"fleet-health.json not found at {FLEET_HEALTH_PATH}")

    with open(FLEET_HEALTH_PATH, encoding="utf-8") as f:
        report = json.load(f)

    generated_at_str = report.get("generated_at", "")
    try:
        generated_at = datetime.fromisoformat(generated_at_str.replace("Z", "+00:00"))
    except ValueError:
        generated_at = datetime.now(timezone.utc) - timedelta(days=1)
    age = datetime.now(timezone.utc) - generated_at
    fleet_stale = age > timedelta(hours=FLEET_HEALTH_STALE_HOURS)

    known_issues_text = ""
    if os.path.exists(KNOWN_ISSUES_PATH):
        with open(KNOWN_ISSUES_PATH, encoding="utf-8") as f:
            known_issues_text = f.read()
    known_issues = parse_known_issues(known_issues_text)

    agents = report.get("agents", {})
    buckets: dict[str, str] = {}
    agent_alerts: dict[str, str] = {}
    known_reasons: dict[str, str] = {}

    for agent_id, agent_report in agents.items():
        bucket, detail = _classify_agent(agent_id, agent_report, known_issues, fleet_stale)
        buckets[agent_id] = bucket
        if bucket == "alert":
            agent_alerts[agent_id] = agent_report.get("alert") or agent_report.get("error", "")
        elif bucket == "known":
            known_reasons[agent_id] = detail

    # Overall precedence: down > alert > stale > known > healthy
    if any(b == "down" for b in buckets.values()):
        overall = "down"
    elif any(b == "alert" for b in buckets.values()):
        overall = "alert"
    elif any(b == "stale" for b in buckets.values()):
        overall = "stale"
    elif any(b == "known" for b in buckets.values()):
        overall = "known"
    else:
        overall = "healthy"

    validate_result = _run_validate()
    conflicts = _find_conflicted_copies()

    report_text = _format_report(
        overall, buckets, agent_alerts, known_reasons, validate_result, conflicts,
        generated_at, age,
    )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    tmp = OUTPUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(report_text)
    os.replace(tmp, OUTPUT_PATH)

    summary = {
        "status": "ok" if overall in ("healthy", "known") else "degraded",
        "overall": overall,
        "buckets": buckets,
        "agent_alerts": agent_alerts,
        "known_reasons": known_reasons,
        "alerts_count": sum(1 for b in buckets.values() if b == "alert"),
    }
    if summary["status"] != "ok":
        summary["alert"] = (
            f"🦊 morning-status: overall={overall}, "
            f"alerts={summary['alerts_count']}"
        )
    return summary


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"🦊 morning-status crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
