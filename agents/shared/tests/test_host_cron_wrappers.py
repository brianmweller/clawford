"""Phase 6.5 regression guard: host-cron wrappers must not `docker exec`.

Phase 4 moved scheduling off the OpenClaw cron registry but left script
execution inside the gateway container via `docker exec`. Phase 6.5
rewrites the wrappers to invoke scripts with bare host `python3` so the
gateway container can be stopped without breaking the fleet.

This test is the structural guarantee. It walks `ops/scripts/*.sh` and:

  1. For each wrapper, strips comment lines and asserts no remaining
     `docker exec` substring. Comments are allowed to reference the
     phrase (several wrappers document *why* they avoid `docker exec`).
  2. For each wrapper that runs python, asserts an explicit
     `/usr/bin/python3` — cron PATH on the VPS is minimal and bare
     `python3` may not resolve.
  3. For `install-host-cron.sh` specifically, asserts no CONTRACT_ENTRY
     or DIRECT_ENTRIES line references `/home/node/.openclaw/...`.
     Container paths belong inside the container; host-cron entries
     must use `/home/openclaw/.openclaw/...` so the bare-host wrapper
     can resolve them.

Three wrappers are known-clean pre-6.5 and stay clean after: they don't
run python at all, they run python but not via docker exec, or they
invoke only curl/crontab. The comment grep still runs on them; the
python-path check only runs on wrappers that actually call python3.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "ops" / "scripts"

# Wrappers that don't invoke python3 at all — the explicit-path check skips these.
NON_PYTHON_WRAPPERS = {
    "set-bot-commands.sh",
    "set-bot-descriptions.sh",
    "sync-brain-to-vps.sh",
    # install-host-cron.sh manages crontab, not python directly
    "install-host-cron.sh",
    # fix-it-cron-self-check runs its own python block via /usr/bin/python3 already
    # — included in the check below, not here.
}


def _iter_wrapper_files() -> list[Path]:
    return sorted(SCRIPTS_DIR.glob("*.sh"))


def _strip_comments(source: str) -> str:
    """Return `source` with full-line and inline shell comments removed.

    Shell comments start at `#` that is not inside a single- or
    double-quoted string. This implementation is deliberately simple:
    it walks character-by-character, toggles quote state on `'` and
    `"`, and cuts the line at the first `#` seen outside of quotes.
    Good enough for the ops/scripts wrappers, which don't embed `#`
    inside strings.
    """
    out_lines = []
    for raw_line in source.splitlines():
        in_single = False
        in_double = False
        cut = len(raw_line)
        for i, ch in enumerate(raw_line):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                cut = i
                break
        out_lines.append(raw_line[:cut])
    return "\n".join(out_lines)


@pytest.mark.parametrize(
    "wrapper_path",
    _iter_wrapper_files(),
    ids=lambda p: p.name,
)
def test_no_docker_exec_in_wrapper_code(wrapper_path: Path) -> None:
    """After comment-stripping, no wrapper may invoke `docker exec`.

    Several wrappers (fleet-health-host.sh, morning-status-host.sh,
    some comments in costco-token-refresh-host.sh etc.) document in
    comments *why* they avoid docker exec. The strip_comments pass
    removes those, so they don't trip the regression guard.
    """
    source = wrapper_path.read_text(encoding="utf-8")
    stripped = _strip_comments(source)
    assert "docker exec" not in stripped, (
        f"{wrapper_path.name}: `docker exec` appears in non-comment code. "
        f"Phase 6.5 rewrote these to use bare /usr/bin/python3 — if you're "
        f"adding a new wrapper, use the same pattern. If you genuinely need "
        f"the gateway container, this test needs an explicit allowlist update."
    )


@pytest.mark.parametrize(
    "wrapper_path",
    [p for p in _iter_wrapper_files() if p.name not in NON_PYTHON_WRAPPERS],
    ids=lambda p: p.name,
)
def test_python_wrappers_use_explicit_bin_path(wrapper_path: Path) -> None:
    """Host cron runs with a minimal PATH. Any wrapper that invokes
    python3 must use the explicit `/usr/bin/python3` path so the cron
    environment can resolve it.

    Exceptions: wrappers that only use python3 inside a `python3 -c`
    inline snippet for stdout JSON parsing are also required to use
    the explicit path — the same PATH risk applies.
    """
    source = wrapper_path.read_text(encoding="utf-8")
    stripped = _strip_comments(source)

    # A wrapper "invokes python3" if the stripped source contains the
    # token `python3`. The check is lenient — if a wrapper happens to
    # spell it as `/usr/bin/python3` everywhere, it passes trivially.
    if "python3" not in stripped:
        pytest.skip(f"{wrapper_path.name} does not invoke python3 in code")

    # Every occurrence of `python3` in the non-comment source must be
    # preceded by `/usr/bin/` (allowing inline heredocs and nested
    # subshells).
    bare_python_pattern = re.compile(r"(?<!/usr/bin/)(?<!['\"])\bpython3\b")
    matches = bare_python_pattern.findall(stripped)
    assert not matches, (
        f"{wrapper_path.name}: found bare `python3` invocation(s). "
        f"Host cron PATH is minimal — use `/usr/bin/python3` everywhere. "
        f"Matches: {matches}"
    )


FORBIDDEN_CRON_PATHS = (
    "/home/node/.openclaw/",      # container path retired in Phase 6.5
    "/home/openclaw/.openclaw/",  # host openclaw path retired in Phase 7b
)
REQUIRED_CRON_PATH_PREFIX = "/home/openclaw/.clawford/"


def test_install_host_cron_contract_entries_use_host_paths() -> None:
    """Every CONTRACT_ENTRY and DIRECT_ENTRIES line in install-host-cron.sh
    must reference /home/openclaw/.clawford/... for scripts.

    Phase 6.5 moved scripts off the container path /home/node/.openclaw/...
    onto host paths so /usr/bin/python3 can resolve them. Phase 7b then
    renamed the host workspace root from .openclaw → .clawford. Both
    legacy prefixes are now hard-forbidden in this file.
    """
    source = (SCRIPTS_DIR / "install-host-cron.sh").read_text(encoding="utf-8")
    stripped = _strip_comments(source)

    for forbidden in FORBIDDEN_CRON_PATHS:
        if forbidden in stripped:
            offending_lines = [
                line for line in stripped.splitlines() if forbidden in line
            ]
            raise AssertionError(
                f"install-host-cron.sh references {forbidden!r} in "
                f"{len(offending_lines)} non-comment line(s). Phase 7b "
                f"renamed the host workspace root to "
                f"{REQUIRED_CRON_PATH_PREFIX}; update the CONTRACT_ENTRIES "
                f"and DIRECT_ENTRIES to match.\n\n"
                + "\n".join(f"  {ln.strip()}" for ln in offending_lines[:5])
            )

    if REQUIRED_CRON_PATH_PREFIX not in stripped:
        raise AssertionError(
            f"install-host-cron.sh has no references to "
            f"{REQUIRED_CRON_PATH_PREFIX}. Phase 7b expects every "
            f"CONTRACT_ENTRY to use the renamed host workspace root."
        )
