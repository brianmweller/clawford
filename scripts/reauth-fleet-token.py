#!/usr/bin/env python3
"""reauth-fleet-token.py — one-command OAuth re-auth for any fleet agent.

Bundles the three steps the operator had to run by hand whenever a refresh
token hit Google's 7-day cliff:

  1. Run the agent's interactive OAuth flow locally (browser opens).
  2. SCP the new token.json to the VPS.
  3. Verify the new token refreshes against Google on the VPS.

Why a helper: the 7-day rule applies to the whole fleet (gmail/cal
scopes are restricted/sensitive, and verification isn't feasible),
so this is a recurring chore. Telegram nags from
ops/scripts/token-age-check.py reference this script by name; tap
the alert, run one command, done.

Usage (PowerShell or any shell):

  python scripts\\reauth-fleet-token.py huckle
  python scripts\\reauth-fleet-token.py family-calendar
  python scripts\\reauth-fleet-token.py murphy

Aliases: huckle = connector, mistress-mouse / mouse = family-calendar,
murphy / sergeant-murphy = meetings-coach, hilda / hippo = shopping.

Defaults assume the VPS is reachable as `openclaw@<your-tailscale-host>`
(Tailscale-only; see memory: reference_vps_access.md).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REMOTE = "openclaw@<your-tailscale-host>"


# Canonical agent ids → (auth-script-name, workspace-name).
# workspace-name is the directory under ~/.clawford/ AND the path on
# the VPS (.clawford/<workspace>/token.json) — these two happen to
# match for every fleet agent.
_AGENT_CONFIG: dict[str, tuple[str, str]] = {
    "connector":       ("gmail-auth.py", "connector-workspace"),
    "family-calendar": ("gcal-auth.py",  "family-calendar-workspace"),
    "meetings-coach":  ("gcal-auth.py",  "meetings-coach-workspace"),
    "shopping":        ("gmail-auth.py", "shopping-workspace"),
}

# Friendly aliases. Keys must be lowercase; resolve_agent normalizes
# the input before lookup.
_ALIASES: dict[str, str] = {
    "huckle": "connector",
    "huckle-cat": "connector",
    "mistress-mouse": "family-calendar",
    "mouse": "family-calendar",
    "sergeant-murphy": "meetings-coach",
    "murphy": "meetings-coach",
    "hilda": "shopping",
    "hippo": "shopping",
}


def resolve_agent(name: str) -> str:
    """Map a user-supplied name (alias or canonical) to a canonical id."""
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("agent name required")
    if key in _AGENT_CONFIG:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    valid = sorted(set(list(_AGENT_CONFIG) + list(_ALIASES)))
    raise ValueError(f"unknown agent {name!r}; valid: {', '.join(valid)}")


def agent_paths(agent: str) -> dict:
    """Return resolved paths for an agent's re-auth flow.

    Keys:
      auth_script  Path to the local interactive auth script (Python).
      token_path   Path to the local token.json the auth script writes.
      remote_path  Relative path on the VPS (under ~/) for scp upload.
    """
    if agent not in _AGENT_CONFIG:
        raise ValueError(f"agent_paths called with non-canonical id {agent!r}")
    script_name, workspace = _AGENT_CONFIG[agent]

    home = Path(os.path.expanduser("~"))
    return {
        "auth_script": REPO_ROOT / "agents" / agent / "scripts" / script_name,
        "token_path": home / ".clawford" / workspace / "token.json",
        "remote_path": f".clawford/{workspace}/token.json",
    }


def _vps_verify_command(remote_path: str) -> str:
    """Return the bash command to run on the VPS to confirm refresh works."""
    return (
        "python3 -c \""
        "import json; "
        "from google.oauth2.credentials import Credentials; "
        "from google.auth.transport.requests import Request; "
        f"d=json.load(open('/home/openclaw/{remote_path}')); "
        "c=Credentials.from_authorized_user_info(d); "
        "c.refresh(Request()); "
        "print('VPS refresh OK')"
        "\""
    )


def run_reauth(agent_name: str, *, remote_host: str = DEFAULT_REMOTE) -> int:
    """Drive the three-step re-auth and return an exit code (0 = ok)."""
    agent = resolve_agent(agent_name)
    info = agent_paths(agent)
    auth_script = info["auth_script"]
    token_path = info["token_path"]
    remote_path = info["remote_path"]

    print(f"[reauth] agent={agent}")
    print(f"[reauth] auth script: {auth_script}")
    print(f"[reauth] local token: {token_path}")
    print(f"[reauth] remote path: {remote_host}:{remote_path}")
    print()

    # 1. Interactive auth — browser opens, user signs in.
    print("[reauth] step 1/3: launching local OAuth flow…")
    auth_result = subprocess.run(
        [sys.executable, str(auth_script)],
    )
    if auth_result.returncode != 0:
        print(
            f"[reauth] auth script exited {auth_result.returncode}; aborting.",
            file=sys.stderr,
        )
        return 2

    # 2. Verify token landed.
    if not Path(token_path).exists():
        print(
            f"[reauth] token.json was not written at {token_path}; "
            f"did you decline the overwrite prompt? Aborting.",
            file=sys.stderr,
        )
        return 3

    # 3. SCP to VPS.
    print(f"[reauth] step 2/3: scp to {remote_host}…")
    scp_result = subprocess.run(
        ["scp", str(token_path), f"{remote_host}:{remote_path}"],
    )
    if scp_result.returncode != 0:
        print(f"[reauth] scp failed (rc={scp_result.returncode}).", file=sys.stderr)
        return 4

    # 4. Verify on VPS.
    print(f"[reauth] step 3/3: verifying refresh against Google from VPS…")
    verify_result = subprocess.run(
        ["ssh", remote_host, _vps_verify_command(remote_path)],
        capture_output=True,
        text=True,
    )
    if verify_result.returncode != 0 or "VPS refresh OK" not in (verify_result.stdout or ""):
        print(f"[reauth] VPS verify FAILED.", file=sys.stderr)
        print(f"  stdout: {verify_result.stdout!r}", file=sys.stderr)
        print(f"  stderr: {verify_result.stderr!r}", file=sys.stderr)
        return 5

    print(f"[reauth] ✅ {agent} re-authed and verified on VPS.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-auth a fleet agent's Google OAuth.")
    ap.add_argument("agent", help="Agent name or alias (huckle, mouse, murphy, hilda, …)")
    ap.add_argument("--remote", default=DEFAULT_REMOTE,
                    help=f"SSH destination (default: {DEFAULT_REMOTE})")
    args = ap.parse_args(argv)

    try:
        return run_reauth(args.agent, remote_host=args.remote)
    except ValueError as e:
        print(f"[reauth] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
