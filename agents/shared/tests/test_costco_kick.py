"""Tests for agents/shopping/scripts/costco-kick.sh.

Covers idempotency, tunnel-wait, and daemon invocation using env-var
stubs (PROBE_CMD, DAEMON_CMD). Requires bash on PATH.

Run: cd agents/shared && python3 -m pytest tests/test_costco_kick.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
KICK_SCRIPT = REPO_ROOT / "agents" / "shopping" / "scripts" / "costco-kick.sh"


@pytest.fixture
def kick_env(tmp_path: Path):
    """Return a dict of env vars pointing the kick script at tmp paths
    and marker files it can write. Each call refreshes via a helper."""
    daemon_marker = tmp_path / "daemon.called"
    log_path = tmp_path / "kick.log"
    last_run = tmp_path / "kick.last-ts"
    env_file = tmp_path / ".env"
    env_file.write_text("# empty\n", encoding="utf-8")

    env = {
        "COSTCO_KICK_DAEMON": str(tmp_path / "daemon.py"),  # unused; DAEMON_CMD overrides
        "COSTCO_KICK_LOG": str(log_path),
        "COSTCO_KICK_LAST_RUN": str(last_run),
        "COSTCO_KICK_ENV_FILE": str(env_file),
        "COSTCO_KICK_IDEMPOTENCY_S": "120",
        "COSTCO_KICK_WAIT_S": "3",
        "PATH": os.environ.get("PATH", ""),
    }
    env["_MARKER"] = str(daemon_marker)
    env["_LOG"] = str(log_path)
    env["_LAST_RUN"] = str(last_run)
    return env


def _run_kick(env: dict) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not on PATH")
    return subprocess.run(
        [bash, str(KICK_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_first_run_with_healthy_tunnel_fires_daemon(kick_env):
    env = dict(kick_env)
    env["COSTCO_KICK_PROBE_CMD"] = "true"
    env["COSTCO_KICK_DAEMON_CMD"] = f"touch '{env['_MARKER']}'; echo fake-daemon-ran"

    result = _run_kick(env)
    assert result.returncode == 0, result.stderr

    assert Path(env["_MARKER"]).exists(), "daemon was not invoked"
    log = Path(env["_LOG"]).read_text(encoding="utf-8")
    assert "tunnel healthy" in log
    assert "daemon exit=0" in log


def test_second_run_within_idempotency_window_skips(kick_env):
    env = dict(kick_env)
    env["COSTCO_KICK_PROBE_CMD"] = "true"
    env["COSTCO_KICK_DAEMON_CMD"] = f"touch '{env['_MARKER']}'; echo ran"

    r1 = _run_kick(env)
    assert r1.returncode == 0

    # Replace the marker so we can detect whether the SECOND run touches it.
    os.remove(env["_MARKER"])

    r2 = _run_kick(env)
    assert r2.returncode == 0
    assert not Path(env["_MARKER"]).exists(), \
        "second kick fired daemon despite being within idempotency window"

    log = Path(env["_LOG"]).read_text(encoding="utf-8")
    assert "skipped" in log


def test_second_run_after_idempotency_window_fires_again(kick_env):
    env = dict(kick_env)
    env["COSTCO_KICK_PROBE_CMD"] = "true"
    env["COSTCO_KICK_DAEMON_CMD"] = f"touch '{env['_MARKER']}'; echo ran"
    env["COSTCO_KICK_IDEMPOTENCY_S"] = "1"  # collapse window so second run qualifies

    r1 = _run_kick(env)
    assert r1.returncode == 0
    os.remove(env["_MARKER"])
    time.sleep(2)

    r2 = _run_kick(env)
    assert r2.returncode == 0
    assert Path(env["_MARKER"]).exists(), \
        "second kick should have fired daemon after idempotency window elapsed"


def test_unhealthy_tunnel_gives_up_after_wait(kick_env):
    env = dict(kick_env)
    env["COSTCO_KICK_PROBE_CMD"] = "false"  # always unhealthy
    env["COSTCO_KICK_DAEMON_CMD"] = f"touch '{env['_MARKER']}'; echo ran"
    env["COSTCO_KICK_WAIT_S"] = "2"  # small wait so the test is fast

    start = time.time()
    result = _run_kick(env)
    elapsed = time.time() - start

    assert result.returncode == 0
    assert not Path(env["_MARKER"]).exists(), \
        "daemon fired even though tunnel was never healthy"
    assert elapsed < 10, f"kick waited too long: {elapsed:.1f}s"

    log = Path(env["_LOG"]).read_text(encoding="utf-8")
    assert "giving up" in log
