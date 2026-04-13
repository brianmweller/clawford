"""Enforce the agent script / cron message contract.

See agents/shared/SCRIPT_CONTRACT.md for the full contract. Two
enforcement surfaces live in this file:

  (a) STATIC CRON MESSAGE HYGIENE — walk every manifest's crons[]
      array and fail if any `message` field contains shell-operator
      patterns that the openclaw 2026.4.11 exec preflight rejects.
      These are the patterns an LLM would copy verbatim into its
      exec command if it saw them in the instructions.

  (b) RUNTIME SCRIPT SELF-REPORT — for each script referenced by a
      manifest's scripts[] list, run it in a subprocess with an
      isolated HOME and no real credentials, and assert the contract:
      exits 0, last stdout line parses as JSON with a valid status key.

Both surfaces are parameterized so each violation produces its own
failing test case with a useful name.
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

    Non-.py files (e.g. on-startup.sh, retire.sh) are excluded — the
    contract only applies to Python scripts invoked by cron LLM sessions.
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
# (b) Runtime script self-report
# ────────────────────────────────────────────────────────────────────────

# Some scripts should be excluded from the runtime subprocess check
# because they would take too long, have unavoidable side effects, or
# are genuinely stubs. This list is the authoritative allowlist — every
# entry needs a reason. Adding to this list is a signal that the script
# needs special consideration.
RUNTIME_CHECK_SKIPLIST: dict[str, str] = {
    # format: "agent/relative/path.py": "reason skipped"
}


@dataclass
class ContractResult:
    ok: bool
    reason: str
    stdout: str
    stderr: str
    returncode: int


def _run_script_in_sandbox(script_path: Path, tmp_home: Path) -> ContractResult:
    """Run a script with an isolated HOME and assert contract compliance."""
    env = os.environ.copy()
    # Isolate credentials / state — most scripts read from ~/.openclaw/*
    # or ~/.config/*. Pointing HOME at a scratch dir forces them through
    # their error paths without touching real tokens.
    env["HOME"] = str(tmp_home)
    env["USERPROFILE"] = str(tmp_home)  # Windows equivalent
    # Strip bot tokens so scripts that want them fail fast.
    for k in list(env.keys()):
        if k.endswith("_BOT_TOKEN"):
            del env[k]

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(script_path.parent),
            timeout=20,
        )
    except subprocess.TimeoutExpired as e:
        return ContractResult(
            ok=False,
            reason=f"script hung >20s (may be waiting on network). stdout so far: {e.stdout or ''!r}",
            stdout=e.stdout or "",
            stderr=e.stderr or "",
            returncode=-1,
        )
    except Exception as e:
        return ContractResult(
            ok=False,
            reason=f"subprocess failed to launch: {e}",
            stdout="",
            stderr="",
            returncode=-2,
        )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    if proc.returncode != 0:
        return ContractResult(
            ok=False,
            reason=f"script exited with code {proc.returncode}; contract requires 0",
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )

    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return ContractResult(
            ok=False,
            reason="script produced no stdout; contract requires a JSON final line",
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )

    last = lines[-1]
    try:
        obj = json.loads(last)
    except json.JSONDecodeError as e:
        return ContractResult(
            ok=False,
            reason=f"last stdout line is not valid JSON ({e}); got: {last!r}",
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )

    if not isinstance(obj, dict):
        return ContractResult(
            ok=False,
            reason=f"last stdout JSON is not an object; got {type(obj).__name__}",
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )

    status = obj.get("status")
    if status not in ("ok", "error", "degraded"):
        return ContractResult(
            ok=False,
            reason=f"JSON status must be one of ok/error/degraded; got {status!r}",
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )

    return ContractResult(ok=True, reason="", stdout=stdout, stderr=stderr, returncode=0)


@pytest.mark.parametrize(
    "agent_id,script_path",
    _all_scripts(),
    ids=lambda x: f"{x.parent.parent.name}/{x.name}" if isinstance(x, Path) else str(x),
)
def test_script_follows_contract(agent_id: str, script_path: Path, tmp_path: Path) -> None:
    """Every cron-invoked script must exit 0 and print a JSON status line.

    See agents/shared/SCRIPT_CONTRACT.md. The script is run in a
    subprocess with an isolated HOME and bot tokens stripped — most
    scripts will fall through to their error path, which is expected to
    emit `{"status": "error", "error": "..."}` rather than crash.
    """
    if not script_path.exists():
        pytest.fail(f"script listed in manifest but does not exist: {script_path}")

    rel_key = None
    for key in RUNTIME_CHECK_SKIPLIST:
        if str(script_path).replace("\\", "/").endswith(key):
            rel_key = key
            break
    if rel_key is not None:
        pytest.skip(f"{rel_key} — {RUNTIME_CHECK_SKIPLIST[rel_key]}")

    result = _run_script_in_sandbox(script_path, tmp_path)
    if not result.ok:
        pytest.fail(
            f"contract violation in {script_path.name}: {result.reason}\n"
            f"stdout:\n{result.stdout[:800]}\n"
            f"stderr:\n{result.stderr[:800]}"
        )
