"""Tests for ops/scripts/install-bwrap-allowlist.sh.

Verifies the installer merges the repo-curated default allowlist into
the on-host file idempotently, preserves operator-added lines, skips
comments and blanks, and rewrites atomically.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops" / "scripts" / "install-bwrap-allowlist.sh"


def _have_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = pytest.mark.skipif(not _have_bash(), reason="bash not on PATH")


def _posix(p: Path) -> str:
    """Convert a path to one the subprocess bash can parse.

    On Windows, subprocess.run invokes WSL bash (not Git Bash), so
    Windows drives need to be translated to /mnt/<drive>/... form.
    On Linux/Mac, paths are returned unchanged.
    """
    s = str(p).replace("\\", "/")
    if sys.platform == "win32" and len(s) >= 2 and s[1] == ":":
        drive, rest = s[0].lower(), s[2:]
        if not rest.startswith("/"):
            rest = "/" + rest
        return f"/mnt/{drive}{rest}"
    return s


def _run(default_file: Path, target_file: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DEFAULT_FILE"] = _posix(default_file)
    env["TARGET_FILE"] = _posix(target_file)
    # Windows Python → WSL bash interop: env vars are only forwarded
    # when listed in WSLENV. No-op on Linux/Mac.
    if sys.platform == "win32":
        env["WSLENV"] = env.get("WSLENV", "") + ":TARGET_FILE:DEFAULT_FILE"
    return subprocess.run(
        ["bash", _posix(SCRIPT)],
        capture_output=True, text=True, env=env, check=False,
    )


def _default_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "default.txt"
    path.write_text(
        "# header comment\n"
        "\n"
        "connector-gmail-facts-mine\n"
        "connector-workflowy-facts-mine\n"
        "meetings-coach-krisp-facts-mine\n",
        encoding="utf-8",
    )
    return path


def test_creates_target_when_absent(tmp_path):
    default = _default_fixture(tmp_path)
    target = tmp_path / "nested" / "bwrap-allowlist.txt"
    r = _run(default, target)
    assert r.returncode == 0, r.stderr
    content = target.read_text(encoding="utf-8")
    assert "connector-gmail-facts-mine" in content
    assert "connector-workflowy-facts-mine" in content
    assert "meetings-coach-krisp-facts-mine" in content


def test_skips_comments_and_blanks(tmp_path):
    default = _default_fixture(tmp_path)
    target = tmp_path / "bwrap-allowlist.txt"
    _run(default, target)
    content = target.read_text(encoding="utf-8")
    assert "# header comment" not in content


def test_preserves_operator_additions(tmp_path):
    default = _default_fixture(tmp_path)
    target = tmp_path / "bwrap-allowlist.txt"
    target.write_text("operator-custom-cron\n", encoding="utf-8")
    _run(default, target)
    content = target.read_text(encoding="utf-8")
    assert "operator-custom-cron" in content
    assert "connector-gmail-facts-mine" in content


def test_idempotent_on_second_run(tmp_path):
    default = _default_fixture(tmp_path)
    target = tmp_path / "bwrap-allowlist.txt"
    _run(default, target)
    first_content = target.read_text(encoding="utf-8")
    _run(default, target)
    second_content = target.read_text(encoding="utf-8")
    assert first_content == second_content


def test_fails_when_default_file_missing(tmp_path):
    target = tmp_path / "bwrap-allowlist.txt"
    r = _run(tmp_path / "does-not-exist.txt", target)
    assert r.returncode != 0
    assert "not found" in r.stderr.lower() or "not found" in r.stdout.lower()


def test_reports_count_added(tmp_path):
    default = _default_fixture(tmp_path)
    target = tmp_path / "bwrap-allowlist.txt"
    r = _run(default, target)
    assert "added 3" in r.stdout
