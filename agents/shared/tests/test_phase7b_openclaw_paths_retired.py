"""Phase 7b regression guard: ~/.openclaw/ paths must stay retired.

The Clawford liberation rename moved the agent workspace root from
~/.openclaw/ to ~/.clawford/ and the env file from ~/openclaw/.env to
~/clawford/.env. This test fails if any file under agents/ or ops/scripts/
reintroduces an openclaw path string or env-var reference.

The Dropbox brain root (~/Dropbox/openclaw-backup/) is intentionally NOT
renamed — Dropbox sync history would reset and re-download the whole brain.
Lines that match a forbidden pattern but are wholly explained by the
"openclaw-backup" substring are allowed.

Two test files (this one and test_host_cron_wrappers.py) legitimately
contain the forbidden patterns as string literals — they are listed in
SKIP_FILES so they don't trip themselves.

A handful of container-era dead-code shells under agents/shopping/scripts/
still reference /home/node/.openclaw/... — those files are not actually
called by any host cron after Phase 6.5 and are also in SKIP_FILES,
flagged as dead code awaiting a separate cleanup sweep.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


SKIP_FILES = {
    "agents/shared/tests/test_phase7b_openclaw_paths_retired.py",
    "agents/shared/tests/test_host_cron_wrappers.py",
    "agents/shopping/scripts/entrypoint.sh",
    "agents/shopping/scripts/on-startup.sh",
    "agents/shopping/scripts/cache-subscriptions.sh",
}


ALLOWED_SUBSTRINGS = (
    "openclaw-backup",
)


FORBIDDEN_PATTERNS = (
    re.compile(r'\.openclaw[/"\']'),
    re.compile(r'~/openclaw/'),
    re.compile(r'/openclaw/\.env'),
    re.compile(r'\bOPENCLAW_[A-Z_]+\b'),
)


SCAN_EXTENSIONS = {
    ".py", ".sh", ".json", ".md", ".txt", ".yml", ".yaml", ".toml",
}


SCAN_ROOTS = (
    "agents",
    "ops/scripts",
)


def _scan_file(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    violations: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        scrubbed = raw
        for allowed in ALLOWED_SUBSTRINGS:
            scrubbed = scrubbed.replace(allowed, "")
        for pat in FORBIDDEN_PATTERNS:
            if pat.search(scrubbed):
                violations.append((lineno, raw.rstrip()))
                break
    return violations


def _iter_scannable_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in SCAN_EXTENSIONS:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in SKIP_FILES:
                continue
            files.append(path)
    return sorted(files)


@pytest.mark.parametrize(
    "path",
    _iter_scannable_files(),
    ids=lambda p: p.relative_to(REPO_ROOT).as_posix(),
)
def test_no_openclaw_paths(path: Path) -> None:
    violations = _scan_file(path)
    if violations:
        rel = path.relative_to(REPO_ROOT).as_posix()
        sample = "\n".join(f"  {n}: {ln}" for n, ln in violations[:5])
        more = (
            f"\n  ...and {len(violations) - 5} more"
            if len(violations) > 5
            else ""
        )
        raise AssertionError(
            f"{rel}: {len(violations)} forbidden openclaw reference(s)\n"
            f"{sample}{more}"
        )
