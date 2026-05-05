#!/usr/bin/env python3
"""reauth-fleet-token.py — OAuth re-auth for fleet Google tokens.

Two modes:

  --all (preferred)
      One browser flow with the union of every agent's scopes; the
      resulting token.json is written to every workspace and SCPed
      to the VPS. One click per ~7-day cliff covers the whole fleet.

  <agent>  (legacy / single-agent)
      Run one agent's per-agent auth script. Kept for ad-hoc reauths
      when only one workspace is implicated. Avoid for routine use:
      all four agents share one OAuth client, and consenting with
      different scope subsets across reauths silently narrows
      whichever workspace consented last (the latest grant wins).

Both modes:
  1. Run interactive OAuth locally (browser opens).
  2. SCP token.json to the VPS workspace(s).
  3. Verify refresh against Google from the VPS.

Why a helper: gmail/calendar scopes are restricted/sensitive, so
unverified-app refresh tokens hit Google's 7-day cliff. Telegram
nags from ops/scripts/token-age-check.py reference this script by
name; tap the alert, run one command, done.

Usage (PowerShell or any shell):

  python scripts\\reauth-fleet-token.py --all       # routine
  python scripts\\reauth-fleet-token.py huckle      # ad-hoc

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

# Union scope set across the fleet. All four agents share one OAuth
# client_id; running per-agent flows with different scope subsets
# silently narrows whichever workspace re-auths last (Google issues
# one refresh_token per (client, account), and the latest consent
# wins). One flow with the union is the only way to keep every
# workspace's grant intact. See memory: reference_fleet_oauth_client.md.
UNION_SCOPES = [
    "https://www.googleapis.com/auth/calendar",          # supersedes calendar.readonly
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",     # connector only
    "https://www.googleapis.com/auth/pubsub",            # connector only
    "https://www.googleapis.com/auth/tasks",             # family-calendar only
]

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


# ─── --all mode: one flow, fan out to every workspace ────────────────


def _canonical_credentials_path() -> str:
    """Return the path to a credentials.json usable for the fleet flow.

    Any workspace's credentials.json works because all agents share the
    same OAuth client. family-calendar's is the canonical pick because
    it's the workspace that's been local-resident the longest.
    """
    return os.path.expanduser(
        "~/.clawford/family-calendar-workspace/credentials.json"
    )


def _run_oauth_flow(creds_path: str, scopes: list[str]):
    """Run the interactive browser OAuth flow and return Credentials.

    Mockable seam — tests monkeypatch this module-level reference so
    the suite never opens a real browser.
    """
    # Lazy import: the google_auth_oauthlib package is only needed on
    # the operator's machine, not in CI / on the VPS.
    sys.path.insert(0, str(REPO_ROOT))
    from agents.shared.google_oauth import build_flow  # type: ignore
    flow = build_flow(creds_path, scopes)
    return flow.run_local_server(port=8765, open_browser=True)


def _write_token(creds, token_path: Path, scopes: list[str]) -> None:
    """Atomically write a Credentials object to token.json.

    Mockable seam — tests substitute a fake that just touches the file.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from agents.shared.google_oauth import save_credentials  # type: ignore
    os.makedirs(token_path.parent, exist_ok=True)
    save_credentials(creds, str(token_path), scopes)


def run_reauth_all(remote_host: str = DEFAULT_REMOTE) -> int:
    """One browser flow → write token to every workspace → SCP/verify each.

    Eliminates the per-agent reauth race that silently narrows scopes
    when the fleet's shared OAuth client re-consents with a subset.
    """
    agents = list(_AGENT_CONFIG.keys())
    creds_path = _canonical_credentials_path()

    print(f"[reauth-all] {len(agents)} agents, union scopes:")
    for s in UNION_SCOPES:
        print(f"             {s}")
    print(f"[reauth-all] credentials: {creds_path}")
    print()

    if not Path(creds_path).exists():
        print(f"[reauth-all] credentials.json missing at {creds_path}",
              file=sys.stderr)
        return 1

    print("[reauth-all] step 1/3: launching local OAuth flow…")
    try:
        creds = _run_oauth_flow(creds_path, UNION_SCOPES)
    except Exception as e:  # noqa: BLE001
        print(f"[reauth-all] OAuth flow failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2

    print("[reauth-all] step 2/3: writing token to each workspace…")
    for agent in agents:
        info = agent_paths(agent)
        try:
            _write_token(creds, info["token_path"], UNION_SCOPES)
            print(f"             {agent}: wrote {info['token_path']}")
        except Exception as e:  # noqa: BLE001
            print(f"[reauth-all] {agent}: write failed: {e}", file=sys.stderr)
            return 3

    print("[reauth-all] step 3/3: SCP + verify each workspace on VPS…")
    for agent in agents:
        info = agent_paths(agent)
        token_path = info["token_path"]
        remote_path = info["remote_path"]

        scp = subprocess.run(
            ["scp", str(token_path), f"{remote_host}:{remote_path}"],
        )
        if scp.returncode != 0:
            print(f"[reauth-all] {agent}: scp failed (rc={scp.returncode}).",
                  file=sys.stderr)
            return 4

        verify = subprocess.run(
            ["ssh", remote_host, _vps_verify_command(remote_path)],
            capture_output=True,
            text=True,
        )
        ok = (verify.returncode == 0
              and "VPS refresh OK" in (verify.stdout or ""))
        if not ok:
            print(f"[reauth-all] {agent}: VPS verify FAILED.", file=sys.stderr)
            print(f"  stdout: {verify.stdout!r}", file=sys.stderr)
            print(f"  stderr: {verify.stderr!r}", file=sys.stderr)
            return 5
        print(f"             {agent}: ✅ verified")

    print(f"\n[reauth-all] ✅ all {len(agents)} agents re-authed and verified.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-auth a fleet agent's Google OAuth.")
    ap.add_argument("agent", nargs="?",
                    help="Agent name or alias (huckle, mouse, murphy, hilda, …)")
    ap.add_argument("--all", action="store_true", dest="all_agents",
                    help="Run one flow + fan out to every fleet agent (preferred).")
    ap.add_argument("--remote", default=DEFAULT_REMOTE,
                    help=f"SSH destination (default: {DEFAULT_REMOTE})")
    args = ap.parse_args(argv)

    if args.all_agents:
        if args.agent:
            ap.error("pass either an agent or --all, not both")
        return run_reauth_all(remote_host=args.remote)

    if not args.agent:
        ap.error("agent name required (or pass --all)")

    try:
        return run_reauth(args.agent, remote_host=args.remote)
    except ValueError as e:
        print(f"[reauth] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
