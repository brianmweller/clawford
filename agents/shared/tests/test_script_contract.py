"""Enforce the agent script / cron message contract.

See agents/shared/SCRIPT_CONTRACT.md for the full contract. Three
enforcement surfaces live in this file:

  (a) STATIC CRON MESSAGE HYGIENE — walk every manifest's crons[]
      array and fail if any `message` field contains shell-operator
      patterns that the openclaw 2026.4.11 exec preflight rejects.

  (b) WRAPPER COMPLIANCE — for each script referenced by a manifest's
      scripts[] list, run it VIA `agents/shared/contract_wrap.py` with
      an isolated HOME. The wrapper guarantees compliant JSON output
      regardless of what the target does; any failure of this surface
      indicates contract_wrap.py itself is broken. This is the
      ship-blocker check.

  (c) NATIVE COMPLIANCE (soft) — same script set, but run bare without
      the wrapper. Scripts that produce compliant JSON natively turn
      green here; others are listed via pytest.xfail as "pending
      native conversion". Tracks conversion progress without blocking
      shipping.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# ────────────────────────────────────────────────────────────────────────
# Discovery
# ────────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"
CONTRACT_WRAP = REPO_ROOT / "agents" / "shared" / "contract_wrap.py"


def _load_manifests() -> list[tuple[str, dict]]:
    """Return [(agent_id, manifest_dict), ...] for every manifest in the repo."""
    out: list[tuple[str, dict]] = []
    for child in sorted(AGENTS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".", "shared")):
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            mf = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            pytest.fail(f"could not parse {manifest_path}: {e}")
        out.append((child.name, mf))
    return out


def _all_cron_messages() -> list[tuple[str, str, str]]:
    """Yield (agent_id, cron_name, message) for every cron in every manifest."""
    out: list[tuple[str, str, str]] = []
    for agent_id, mf in _load_manifests():
        for cron in mf.get("crons") or []:
            name = cron.get("name") or "<unnamed>"
            msg = cron.get("message") or ""
            out.append((agent_id, name, msg))
    return out


def _all_scripts() -> list[tuple[str, Path]]:
    """Yield (agent_id, absolute_script_path) for every Python script in every manifest.

    Non-.py files (e.g. retire.sh) are excluded — the contract only
    applies to Python scripts invoked by cron LLM sessions.
    """
    out: list[tuple[str, Path]] = []
    for agent_id, mf in _load_manifests():
        agent_dir = AGENTS_DIR / agent_id
        for rel in mf.get("scripts") or []:
            if not rel.endswith(".py"):
                continue
            out.append((agent_id, (agent_dir / rel).resolve()))
    return out


# ────────────────────────────────────────────────────────────────────────
# (a) Static cron message hygiene
# ────────────────────────────────────────────────────────────────────────

# Patterns that are bug-attractors: if an LLM reads one of these in a
# cron message, it will often copy the pattern verbatim into its exec
# command and then hit the openclaw 2026.4.11 hardcoded preflight.
#
# Intentionally narrow — these are literal substrings, not regexes, so
# documentation that mentions e.g. "the script prints {status: ok}"
# won't be flagged. The check focuses on what a shell command looks
# like, not what English looks like.
FORBIDDEN_PATTERNS: list[str] = [
    "; echo $?",
    "; printf",
    "; echo __EXIT",
    '; echo "EXIT',
    "; echo EXIT",
    "$?",
    "sh -lc 'python",
    'sh -lc "python',
    "bash -lc 'python",
    'bash -lc "python',
    "&& python3",
    "&& python ",
    "| python3 ",
    "> /tmp/",
    ">> /tmp/",
    "2>&1",
    "2>/dev/null",
    "$(python",
]


def _message_violations(msg: str) -> list[str]:
    return [p for p in FORBIDDEN_PATTERNS if p in msg]


@pytest.mark.parametrize(
    "agent_id,cron_name,msg",
    _all_cron_messages(),
    ids=lambda x: str(x) if not isinstance(x, str) or len(x) < 60 else x[:60],
)
def test_cron_message_is_hygienic(agent_id: str, cron_name: str, msg: str) -> None:
    """Every cron message must be free of shell-operator bug-attractors.

    See agents/shared/SCRIPT_CONTRACT.md §5. If this test fails with a
    forbidden pattern, rewrite the cron message to say the script
    self-reports via stdout JSON and the LLM should run it bare — don't
    check $?, don't redirect, don't wrap in sh/bash -lc.
    """
    violations = _message_violations(msg)
    assert not violations, (
        f"{agent_id}/{cron_name}: cron message contains forbidden "
        f"shell-operator pattern(s) {violations!r}. "
        f"See agents/shared/SCRIPT_CONTRACT.md. Full message:\n{msg}"
    )


# ────────────────────────────────────────────────────────────────────────
# (b) & (c) Runtime script self-report — via wrapper and native
# ────────────────────────────────────────────────────────────────────────

# Scripts listed here are known to NOT natively conform to the contract
# — they use sys.exit(N!=0), print free-form text, or otherwise rely on
# the cron LLM reading exit codes. The wrapper covers them at runtime,
# but the soft `test_script_is_natively_compliant` test marks them xfail
# until converted. Add a reason when listing a script.
NATIVE_COMPLIANCE_XFAIL: set[str] = {
    # Compose-pipeline libraries — not executable, import-only. Added to
    # manifest for deploy.py sync but aren't meant to run bare.
    "connector/compose_lib.py",
    "connector/inbox_triage_lib.py",
    "connector/flux_import_lib.py",
    "connector/voice_profile_lib.py",
    # Compose-pipeline CLIs — require args (--person-slug, --gmail-thread-id,
    # --circle, etc.) or a live Gmail token; bare invocation can't produce
    # the contract envelope without side effects. Invoked by cron with
    # explicit args, never bare.
    "connector/draft-compose.py",
    "connector/inbox-triage.py",
    "connector/auto-compose.py",
    "connector/voice-profile-build.py",
    "connector/facts-import-flux.py",
    "connector/people-expand-from-flux.py",
    # Interactive OAuth helper — prompts for user input; never run bare.
    "connector/gmail-auth.py",
    # Long-running systemd daemon (clawford-huckle-push.service). Doesn't
    # conform to the one-shot JSON contract model: the script runs a
    # blocking Pub/Sub pull loop until SIGTERM. Health is observed via
    # heartbeat.py's gmail_push probe (systemctl is-active + watch-state
    # freshness), not via the contract envelope.
    "connector/gmail-push-listener.py",
}


@dataclass
class ContractResult:
    ok: bool
    reason: str
    stdout: str
    stderr: str
    returncode: int


def _isolated_env(tmp_home: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(tmp_home)
    env["USERPROFILE"] = str(tmp_home)
    for k in list(env.keys()):
        if k.endswith("_BOT_TOKEN"):
            del env[k]
    return env


def _assert_compliant_output(stdout: str, returncode: int, stderr: str) -> ContractResult:
    """Validate stdout last-line JSON shape. Returns ok=True on valid output."""
    if returncode != 0:
        return ContractResult(
            ok=False,
            reason=f"script exited with code {returncode}; contract requires 0",
            stdout=stdout, stderr=stderr, returncode=returncode,
        )
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return ContractResult(
            ok=False, reason="script produced no stdout",
            stdout=stdout, stderr=stderr, returncode=returncode,
        )
    try:
        obj = json.loads(lines[-1])
    except json.JSONDecodeError as e:
        return ContractResult(
            ok=False, reason=f"last stdout line is not valid JSON ({e}): {lines[-1]!r}",
            stdout=stdout, stderr=stderr, returncode=returncode,
        )
    if not isinstance(obj, dict):
        return ContractResult(
            ok=False, reason=f"last stdout JSON is not an object",
            stdout=stdout, stderr=stderr, returncode=returncode,
        )
    status = obj.get("status")
    if status not in ("ok", "error", "degraded"):
        return ContractResult(
            ok=False, reason=f"JSON status must be ok/error/degraded; got {status!r}",
            stdout=stdout, stderr=stderr, returncode=returncode,
        )
    return ContractResult(ok=True, reason="", stdout=stdout, stderr=stderr, returncode=0)


def _run_bare(script_path: Path, tmp_home: Path) -> ContractResult:
    # input="" (not stdin=DEVNULL) — on Windows, DEVNULL maps to NUL which
    # is reported as a TTY by sys.stdin.isatty(), so scripts that branch
    # on isatty (e.g. interactive getpass helpers) would still try to
    # prompt and hang. An empty-PIPE stdin guarantees isatty()==False
    # everywhere.
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True, text=True,
            env=_isolated_env(tmp_home),
            cwd=str(script_path.parent),
            input="",
            timeout=20,
        )
    except subprocess.TimeoutExpired as e:
        return ContractResult(
            ok=False, reason=f"script hung >20s", stdout=e.stdout or "",
            stderr=e.stderr or "", returncode=-1,
        )
    except Exception as e:
        return ContractResult(
            ok=False, reason=f"subprocess failed to launch: {e}",
            stdout="", stderr="", returncode=-2,
        )
    return _assert_compliant_output(proc.stdout or "", proc.returncode, proc.stderr or "")


def _run_wrapped(script_path: Path, tmp_home: Path) -> ContractResult:
    try:
        proc = subprocess.run(
            [sys.executable, str(CONTRACT_WRAP), "--timeout", "15", str(script_path)],
            capture_output=True, text=True,
            env=_isolated_env(tmp_home),
            cwd=str(script_path.parent),
            input="",
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        return ContractResult(
            ok=False, reason=f"wrapper itself hung >30s",
            stdout=e.stdout or "", stderr=e.stderr or "", returncode=-1,
        )
    return _assert_compliant_output(proc.stdout or "", proc.returncode, proc.stderr or "")


def _rel_script_key(script_path: Path) -> str:
    """agents/shopping/scripts/amazon-orders.py -> shopping/amazon-orders.py"""
    return f"{script_path.parent.parent.name}/{script_path.name}"


@pytest.mark.parametrize(
    "agent_id,script_path",
    _all_scripts(),
    ids=lambda x: _rel_script_key(x) if isinstance(x, Path) else str(x),
)
def test_script_is_wrapper_compliant(
    agent_id: str, script_path: Path, tmp_path: Path
) -> None:
    """Every script must produce compliant JSON when run via contract_wrap.py.

    This is the ship-blocker. If it fails, either contract_wrap.py is
    broken or the target script crashes in a way the wrapper can't
    recover from (extremely rare — only imports that core-dump Python).
    """
    if not script_path.exists():
        pytest.fail(f"script listed in manifest but does not exist: {script_path}")
    result = _run_wrapped(script_path, tmp_path)
    if not result.ok:
        pytest.fail(
            f"wrapper failed on {script_path.name}: {result.reason}\n"
            f"stdout:\n{result.stdout[:800]}\nstderr:\n{result.stderr[:800]}"
        )


@pytest.mark.parametrize(
    "agent_id,script_path",
    _all_scripts(),
    ids=lambda x: _rel_script_key(x) if isinstance(x, Path) else str(x),
)
def test_script_is_natively_compliant(
    agent_id: str, script_path: Path, tmp_path: Path
) -> None:
    """Soft check: does the script conform WITHOUT contract_wrap.py?

    Non-conforming scripts are listed in NATIVE_COMPLIANCE_XFAIL with
    a reason. As scripts are converted one-by-one, their xfail entry
    is removed and this test turns green for them. Scripts that
    accidentally regress from natively compliant to non-conforming
    will be caught because they're not in the xfail list.
    """
    if not script_path.exists():
        pytest.fail(f"script listed in manifest but does not exist: {script_path}")
    rel = _rel_script_key(script_path)
    if rel in NATIVE_COMPLIANCE_XFAIL:
        pytest.xfail(f"{rel}: pending native contract conversion")
    result = _run_bare(script_path, tmp_path)
    if not result.ok:
        pytest.fail(
            f"native compliance violation in {script_path.name}: {result.reason}\n"
            f"If this script is new or not yet converted, add "
            f"{rel!r} to NATIVE_COMPLIANCE_XFAIL with a reason.\n"
            f"stdout:\n{result.stdout[:800]}\nstderr:\n{result.stderr[:800]}"
        )
