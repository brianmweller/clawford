"""Tests for subprocess_stdout_reports_ok() in costco-token-daemon.py.

Pre-2026-04-19 bug: spawn_step checked `result.returncode == 0` but the
script-contract wrapper always calls sys.exit(0), so every failed --step
subprocess looked like a success. The reliable signal is the JSON
envelope printed as the LAST stdout line.

Run: cd agents/shared && python3 -m pytest tests/test_spawn_step_envelope.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DAEMON_PATH = REPO_ROOT / "agents" / "shopping" / "scripts" / "costco-token-daemon.py"


def _load_daemon():
    sys.modules.setdefault("camoufox", type(sys)("camoufox"))
    sys.modules.setdefault("camoufox.sync_api", type(sys)("camoufox.sync_api"))
    sys.modules.setdefault("curl_cffi", type(sys)("curl_cffi"))
    sys.modules.setdefault("curl_cffi.requests", type(sys)("curl_cffi.requests"))
    spec = importlib.util.spec_from_file_location("costco_token_daemon", DAEMON_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["costco_token_daemon"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def daemon():
    return _load_daemon()


def test_ok_envelope_returns_true(daemon):
    stdout = '[log] doing thing\n[log] done\n{"status": "ok"}\n'
    assert daemon.subprocess_stdout_reports_ok(stdout) is True


def test_error_envelope_returns_false(daemon):
    # The real-world case that regressed before: reauth hits B2C 429,
    # subprocess prints status=error as its LAST line, exits 0.
    stdout = (
        '[reauth] /SelfAsserted returned 429\n'
        '[reauth] Aborting retries\n'
        '{"status": "error", "error": "main returned 1"}\n'
    )
    assert daemon.subprocess_stdout_reports_ok(stdout) is False


def test_empty_stdout_returns_false(daemon):
    assert daemon.subprocess_stdout_reports_ok("") is False


def test_last_non_empty_line_parsed(daemon):
    # Trailing blank lines shouldn't hide the envelope.
    stdout = '[log] x\n{"status": "ok"}\n\n\n'
    assert daemon.subprocess_stdout_reports_ok(stdout) is True


def test_non_json_last_line_returns_false(daemon):
    stdout = '{"status": "ok"}\nnow some more log text\n'
    assert daemon.subprocess_stdout_reports_ok(stdout) is False


def test_envelope_missing_status_returns_false(daemon):
    stdout = '{"not_status": "ok"}\n'
    assert daemon.subprocess_stdout_reports_ok(stdout) is False


def test_envelope_status_degraded_returns_false(daemon):
    # Only status=="ok" counts as success.
    stdout = '{"status": "degraded"}\n'
    assert daemon.subprocess_stdout_reports_ok(stdout) is False
