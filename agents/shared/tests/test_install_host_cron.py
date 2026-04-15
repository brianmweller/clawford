"""Integration test for ops/scripts/install-host-cron.sh drift detection.

install-host-cron.sh's "already installed" check used to compare only
the trailing marker comment (`# script-contract-<logname>`). If the
schedule / wrapper path / script path / token env / timeout for an
existing entry changed in-source, reruns silently skipped it and the
live crontab stayed stale. Phase 4 hit this when the shopping delivery
digest schedule moved from `0 14 * * *` to `30 10 * * *` — the
existing cron line kept firing at 7 AM PT until the operator manually
`crontab -e`'d it.

This test runs install-host-cron.sh in a subprocess with a fake
`crontab` shim on PATH so the installer reads/writes a temp file
instead of the real crontab. We seed the fake crontab with a
stale-schedule version of shopping-delivery-digest, run the script,
and assert:

  1. Script exits 0.
  2. Stdout contains "drift detected" for the stale entry.
  3. The post-run crontab contains the NEW-schedule line (`30 10 * * *`).
  4. The stale line is gone.

Platform: requires bash + crontab-compatible shell. Skipped on Windows
where bash is not guaranteed. The VPS and CI both satisfy this.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = REPO_ROOT / "ops" / "scripts" / "install-host-cron.sh"
CONTRACT_WRAPPER = REPO_ROOT / "ops" / "scripts" / "script-contract-host.sh"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="install-host-cron.sh requires bash on a POSIX-like environment",
)


def _write_fake_crontab(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake `crontab` shim in tmp_path/bin that reads/writes
    tmp_path/crontab-state.txt instead of the real crontab. Returns
    (bin_dir, state_file)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_file = tmp_path / "crontab-state.txt"
    state_file.touch()

    shim = bin_dir / "crontab"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'STATE="{state_file}"\n'
        'if [[ "${1:-}" == "-l" ]]; then\n'
        '  cat "$STATE" 2>/dev/null || true\n'
        '  exit 0\n'
        'fi\n'
        'if [[ "${1:-}" == "-" ]]; then\n'
        '  cat > "$STATE"\n'
        '  exit 0\n'
        'fi\n'
        'echo "fake crontab: unsupported args: $*" >&2\n'
        'exit 1\n'
    )
    shim.chmod(0o755)
    return bin_dir, state_file


def _run_installer(bin_dir: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_install_script_exists_and_is_executable():
    assert INSTALL_SCRIPT.exists(), f"missing: {INSTALL_SCRIPT}"
    assert CONTRACT_WRAPPER.exists(), (
        f"missing contract wrapper (needed by installer): {CONTRACT_WRAPPER}"
    )


def test_drift_detection_evicts_stale_schedule_and_installs_new(tmp_path):
    """Seed the fake crontab with a stale-schedule shopping-delivery-digest
    line, run the installer, and verify the new 30 10 schedule replaces
    the old one."""
    bin_dir, state_file = _write_fake_crontab(tmp_path)

    # Seed with a STALE shopping-delivery-digest at the old Phase-4
    # schedule (0 14 * * *). The installer's current CONTRACT_ENTRIES
    # has it at 30 10 * * *.
    stale = (
        "0 14 * * * "
        f"{CONTRACT_WRAPPER} shopping-delivery-digest "
        "/home/node/.openclaw/shopping-workspace/scripts/delivery-digest.py "
        "SHOPPING_BOT_TOKEN 900 # script-contract-shopping-delivery-digest\n"
    )
    state_file.write_text(stale)

    result = _run_installer(bin_dir)
    assert result.returncode == 0, (
        f"installer exit {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    assert "drift detected" in result.stdout, (
        f"expected drift detection in stdout, got:\n{result.stdout}"
    )
    assert "shopping-delivery-digest" in result.stdout

    post = state_file.read_text()
    # Stale schedule must be gone.
    stale_prefix = "0 14 * * * "
    stale_lines_for_marker = [
        line
        for line in post.splitlines()
        if "shopping-delivery-digest" in line and line.startswith(stale_prefix)
    ]
    assert not stale_lines_for_marker, (
        f"stale 0 14 line survived:\n{post}"
    )
    # New schedule must be present.
    new_lines = [
        line
        for line in post.splitlines()
        if "shopping-delivery-digest" in line and line.startswith("30 10 * * *")
    ]
    assert len(new_lines) == 1, (
        f"expected exactly one 30 10 shopping-delivery-digest line, got:\n{post}"
    )


def test_matching_line_is_idempotent(tmp_path):
    """If the fake crontab already contains the EXACT desired line, the
    installer reports 'already installed' and leaves it alone."""
    bin_dir, state_file = _write_fake_crontab(tmp_path)

    correct = (
        "30 10 * * * "
        f"{CONTRACT_WRAPPER} shopping-delivery-digest "
        "/home/node/.openclaw/shopping-workspace/scripts/delivery-digest.py "
        "SHOPPING_BOT_TOKEN 900 # script-contract-shopping-delivery-digest\n"
    )
    state_file.write_text(correct)

    result = _run_installer(bin_dir)
    assert result.returncode == 0

    assert "already installed" in result.stdout
    # Should NOT report drift for the matching entry.
    drift_lines = [
        line
        for line in result.stdout.splitlines()
        if "drift detected" in line and "shopping-delivery-digest" in line
    ]
    assert not drift_lines, f"false drift report:\n{result.stdout}"
