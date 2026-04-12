#!/usr/bin/env python3
"""
security-audit.py — Fix-It weekly security audit.

Replaces the inline chr()-obfuscated python -c that previously lived in the
security-audit cron prompt. Pure Python, no shell, no obfuscation.

What it does:
  1. Read /home/node/.openclaw/exec-approvals.json and emit per-agent policy
  2. Run `openclaw security audit --deep` and capture output
  3. Apply enrichment rules (suppress known-acceptable findings)
  4. Format an emoji-headed severity report

Exit code: always 0. Audit results are reported, not failed on.

Override the approvals path with EXEC_APPROVALS_PATH env var (for tests).
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

APPROVALS_PATH = os.environ.get(
    "EXEC_APPROVALS_PATH", "/home/node/.openclaw/exec-approvals.json"
)
AUDIT_TIMEOUT_SEC = 90


def get_agent_policies():
    """Return list of (agent, policy) tuples from exec-approvals.json."""
    try:
        with open(APPROVALS_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return [("(error)", f"approvals file not found at {APPROVALS_PATH}")]
    except Exception as e:
        return [("(error)", f"approvals file unreadable: {e}")]

    agents = data.get("agents", {})
    if not agents:
        defaults = data.get("defaults", {})
        policy = defaults.get("policy") or defaults.get("security") or "?"
        return [("(defaults)", policy)]

    result = []
    for name in sorted(agents.keys()):
        cfg = agents[name] or {}
        policy = cfg.get("policy") or cfg.get("security") or "?"
        result.append((name, policy))
    return result


def run_security_audit():
    """Run `openclaw security audit --deep`. Return raw stdout text."""
    try:
        r = subprocess.run(
            ["openclaw", "security", "audit", "--deep"],
            capture_output=True,
            text=True,
            timeout=AUDIT_TIMEOUT_SEC,
        )
        return (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return "(openclaw CLI not found on PATH)"
    except subprocess.TimeoutExpired:
        return f"(security audit timed out after {AUDIT_TIMEOUT_SEC}s)"
    except Exception as e:
        return f"(security audit failed: {e})"


SEVERITY_PATTERNS = {
    "CRITICAL": re.compile(r"\bcritical\b", re.IGNORECASE),
    "HIGH": re.compile(r"\bhigh\b", re.IGNORECASE),
    "MEDIUM": re.compile(r"\bmedium\b|\bmoderate\b", re.IGNORECASE),
    "LOW": re.compile(r"\blow\b|\binfo\b", re.IGNORECASE),
}

SUPPRESSED_FINDINGS = {
    "tools.exec.security_full_configured",
    "plugins.tools_reachable_permissive_policy",
}


def parse_findings(audit_text):
    """Parse audit output into {severity: [finding_lines]}.

    Best-effort. Handles JSON or plain-text. Suppresses known-acceptable IDs.
    """
    findings = {sev: [] for sev in SEVERITY_PATTERNS}

    if not audit_text or audit_text.startswith("("):
        return findings

    # Try JSON first
    try:
        data = json.loads(audit_text)
        items = data.get("findings") or data.get("issues") or []
        for item in items:
            ident = item.get("id") or item.get("name") or ""
            if ident in SUPPRESSED_FINDINGS:
                continue
            sev_raw = (item.get("severity") or item.get("level") or "").upper()
            sev = next((s for s in SEVERITY_PATTERNS if s in sev_raw), "LOW")
            desc = item.get("message") or item.get("description") or ident or "(no description)"
            findings[sev].append(desc.strip())
        return findings
    except (ValueError, AttributeError):
        pass

    # Fall back to plain-text parsing
    current_sev = None
    for line in audit_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if any(ident in stripped for ident in SUPPRESSED_FINDINGS):
            continue

        upper = stripped.upper()
        if upper.startswith(("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")):
            for sev, pat in SEVERITY_PATTERNS.items():
                if pat.search(stripped):
                    current_sev = sev
                    rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                    if rest:
                        findings[sev].append(rest)
                    break
            continue

        if current_sev and (stripped.startswith(("•", "-", "*"))):
            findings[current_sev].append(stripped.lstrip("•-* ").strip())

    return findings


SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🟢",
}


def render_report(policies, findings, audit_raw):
    """Format the final emoji-headed report."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"🦊🔧 Security Audit — {today}", ""]

    lines.append("🔒 Exec Policies")
    for agent, policy in policies:
        lines.append(f"• {agent}: {policy}")
    lines.append("(All agents policy=full is intentional — see KNOWN_ISSUES.md)")
    lines.append("")

    total_findings = sum(len(v) for v in findings.values())
    if total_findings == 0 and not audit_raw.startswith("("):
        lines.append("✅ Security audit clean.")
        return "\n".join(lines)

    if audit_raw.startswith("("):
        lines.append(f"⚠️ Audit run note: {audit_raw}")
        lines.append("")

    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        items = findings.get(sev, [])
        emoji = SEVERITY_EMOJI[sev]
        if not items:
            continue
        lines.append(f"{emoji} {sev} ({len(items)})")
        for item in items:
            lines.append(f"• {item}")
        lines.append("")

    crit_high = findings.get("CRITICAL", []) + findings.get("HIGH", [])
    if crit_high:
        lines.append("Remediation:")
        for item in crit_high:
            lines.append(f"• {item} — investigate, then notify human.")

    return "\n".join(lines).rstrip() + "\n"


def main():
    policies = get_agent_policies()
    audit_raw = run_security_audit()
    findings = parse_findings(audit_raw)
    report = render_report(policies, findings, audit_raw)
    sys.stdout.write(report)
    sys.exit(0)


if __name__ == "__main__":
    main()
