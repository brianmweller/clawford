#!/usr/bin/env python3
"""token-age-check.py — proactive OAuth refresh-token aging cron.

Walks every agent's `<workspace>/token.json` and pages the operator via
Telegram before any token hits Google's 7-day refresh-token expiry
cliff. The cliff is real for the whole fleet: gmail.readonly,
gmail.compose, calendar are restricted/sensitive scopes, and
unverified production OAuth apps using those scopes get 7-day
refresh-token lifetimes from Google regardless of publishing status.
Verification requires Google review + (for restricted scopes) a CASA
security assessment — not feasible for a personal fleet — so we
accept the chore and automate the friction.

Categories by age (mtime-based):

  fresh    age < 5 days     → silent
  warn     5 <= age < 7     → page: re-auth in next 2 days
  expired  age >= 7         → page: re-auth NOW (already broken)
  missing  no token.json    → silent (first-auth is a separate flow)

The mtime correctly tracks refresh-token issuance for the fleet's
scripts: load_credentials() in agents/shared/google_oauth.py
constructs Credentials with `expiry=None`, so the `creds.expired`
short-circuit in get_credentials() never fires for fresh-from-disk
loads. The on-disk file is only re-saved by the explicit
interactive-auth flow (gmail-auth.py / gcal-auth.py / etc.). So
mtime ≈ "last interactive re-auth date" ≈ refresh-token issuance.

Conforms to agents/shared/SCRIPT_CONTRACT.md: always exits 0,
prints exactly one JSON line on stdout. Run via the standard host
wrapper (script-contract-host.sh) using TELEGRAM_BOT_TOKEN —
fleet-wide signal, not agent-specific, so it pages on the fix-it
bot like fleet-health does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

DEFAULT_MANIFEST = "/home/openclaw/repo/agents/shared/fleet-manifest.json"
DAEMON_LINK_SCRIPT = "/home/openclaw/repo/scripts/fleet-oauth-daemon.py"

WARN_AGE_DAYS = 5.0
EXPIRED_AGE_DAYS = 7.0


def categorize(age_days: float | None) -> str:
    """Bucket a token by its age.

    Pure function: pinned by the test suite so future tuning
    (e.g. moving the warn boundary) is one constant + one test
    update away.
    """
    if age_days is None:
        return "missing"
    if age_days >= EXPIRED_AGE_DAYS:
        return "expired"
    if age_days >= WARN_AGE_DAYS:
        return "warn"
    return "fresh"


def _token_age_days(token_path: Path) -> float | None:
    """mtime → age in days, or None if file missing."""
    if not token_path.exists():
        return None
    try:
        mtime = token_path.stat().st_mtime
    except OSError:
        return None
    return (time.time() - mtime) / 86400


def scan_agents(manifest_path: str) -> list[dict]:
    """Read fleet-manifest.json and return one row per agent.

    Each row: {id, display_name, category, age_days, token_path}.
    Order matches the manifest order.
    """
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    rows: list[dict] = []
    for agent in manifest.get("agents", []):
        workspace_raw = agent.get("workspace", "")
        workspace = Path(os.path.expanduser(workspace_raw))
        token_path = workspace / "token.json"
        age = _token_age_days(token_path)
        rows.append({
            "id": agent.get("id", ""),
            "display_name": agent.get("display_name", agent.get("id", "")),
            "category": categorize(age),
            "age_days": age,
            "token_path": str(token_path),
        })
    return rows


def _format_row(row: dict) -> str:
    age = row.get("age_days")
    name = row.get("display_name") or row.get("id")
    if age is None:
        return f"{name} (no token)"
    return f"{name} ({age:.1f}d)"


def _mint_signed_link(daemon_script: str = DAEMON_LINK_SCRIPT) -> str | None:
    """Ask fleet-oauth-daemon.py to print a fresh tailnet link.

    Best-effort — if the daemon's credentials/secret aren't on this
    host yet, fall back to the laptop-only hint so the alert still
    fires. Never raises.
    """
    try:
        import subprocess
        out = subprocess.check_output(
            ["python3", daemon_script, "--print-link"],
            stderr=subprocess.STDOUT,
            timeout=5,
            text=True,
        ).strip()
        if out.startswith("https://"):
            return out
    except Exception:
        pass
    return None


def build_envelope(rows: list[dict]) -> dict:
    """Build the SCRIPT_CONTRACT envelope from scan rows.

    `missing` rows do not contribute to the alert — first-auth is
    a separate user-driven flow, not a re-auth chore.
    """
    expired = [r for r in rows if r["category"] == "expired"]
    warn = [r for r in rows if r["category"] == "warn"]

    if not expired and not warn:
        return {
            "status": "ok",
            "agents": rows,
        }

    parts: list[str] = []
    if expired:
        parts.append(
            "expired (re-auth NOW): "
            + ", ".join(_format_row(r) for r in expired)
        )
    if warn:
        parts.append(
            "expiring soon: "
            + ", ".join(_format_row(r) for r in warn)
        )

    link = _mint_signed_link()
    if link:
        helper_hint = f"Tap to re-auth (tailnet, mobile-friendly): {link}"
    else:
        helper_hint = "On laptop: python scripts/reauth-fleet-token.py --all"
    alert = "\U0001F511 OAuth aging — " + " | ".join(parts) + " — " + helper_hint

    return {
        "status": "error",
        "alert": alert,
        "agents": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    try:
        rows = scan_agents(args.manifest)
        envelope = build_envelope(rows)
    except Exception as e:  # noqa: BLE001
        envelope = {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "alert": f"\U0001F511 token-age-check crashed: {type(e).__name__}: {str(e)[:200]}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }

    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
