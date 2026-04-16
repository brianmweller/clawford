"""Tests for ops/systemd/clawford-inbox.service — the systemd user unit.

Parses the unit file as INI and asserts all load-bearing keys are present
and correctly set. Does NOT test systemd runtime behavior — that's the
manual smoke test runbook.
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest

UNIT_PATH = Path(__file__).resolve().parents[3] / "ops" / "systemd" / "clawford-inbox.service"


@pytest.fixture
def unit():
    assert UNIT_PATH.exists(), f"unit file missing at {UNIT_PATH}"
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(UNIT_PATH.read_text(encoding="utf-8"))
    return cp


def test_unit_file_exists():
    assert UNIT_PATH.exists()


def test_has_required_sections(unit):
    assert "Unit" in unit
    assert "Service" in unit
    assert "Install" in unit


def test_exec_start_points_to_telegram_inbox(unit):
    exec_start = unit.get("Service", "ExecStart")
    assert "telegram_inbox.py" in exec_start
    assert "/usr/bin/python3" in exec_start


def test_environment_file_set(unit):
    env_file = unit.get("Service", "EnvironmentFile")
    assert env_file == "/home/openclaw/clawford/.env"


def test_restart_on_failure(unit):
    assert unit.get("Service", "Restart") == "on-failure"


def test_restart_sec(unit):
    assert int(unit.get("Service", "RestartSec")) == 5


def test_kill_switch_check_in_exec_start_pre(unit):
    pre = unit.get("Service", "ExecStartPre")
    assert "inbox-disabled" in pre
    assert "test" in pre
    assert "! -f" in pre


def test_working_directory(unit):
    wd = unit.get("Service", "WorkingDirectory")
    assert wd == "/home/openclaw/repo"


def test_log_output_appended(unit):
    stdout = unit.get("Service", "StandardOutput")
    assert stdout.startswith("append:")
    assert "inbox.log" in stdout
    stderr = unit.get("Service", "StandardError")
    assert stderr.startswith("append:")


def test_wanted_by_default_target(unit):
    assert unit.get("Install", "WantedBy") == "default.target"


def test_network_dependency(unit):
    assert "network-online.target" in unit.get("Unit", "After")
    assert "network-online.target" in unit.get("Unit", "Wants")
