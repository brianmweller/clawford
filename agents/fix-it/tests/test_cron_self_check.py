"""Tests for agents/fix-it/scripts/cron-self-check.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:cron-self-check`. The old version walked `openclaw cron list`
and matched against agents/fix-it/expected-crons.json; once Phase 4
finishes the OpenClaw registry is empty and that path goes nowhere.

The replacement parses install-host-cron.sh's CONTRACT_ENTRIES +
DIRECT_ENTRIES bash arrays and diffs them against `crontab -l`. If any
expected marker is missing from the live crontab, send a Telegram
alert. The fix is for the operator to re-run install-host-cron.sh — this cron
diagnoses, it does not auto-install.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "fix-it" / "scripts" / "cron-self-check.py"
FIXTURES = Path(__file__).parent / "fixtures" / "cron-self-check"


def _load():
    spec = importlib.util.spec_from_file_location("cron_self_check", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def install_script():
    return FIXTURES / "install-host-cron-snippet.sh"


@pytest.fixture
def crontab_present():
    return (FIXTURES / "crontab-all-present.txt").read_text(encoding="utf-8")


@pytest.fixture
def crontab_missing():
    return (FIXTURES / "crontab-missing-shopping.txt").read_text(encoding="utf-8")


# ─── parse_install_script ────────────────────────────────────────────


def test_parse_install_script_yields_contract_entries(mod, install_script):
    expected = mod.parse_install_script(install_script)
    markers = [e["marker"] for e in expected]
    assert "# script-contract-linkedin-keepalive" in markers
    assert "# script-contract-family-calendar-reminder-check" in markers
    assert "# script-contract-shopping-delivery-digest" in markers


def test_parse_install_script_yields_direct_entries(mod, install_script):
    expected = mod.parse_install_script(install_script)
    markers = [e["marker"] for e in expected]
    assert "# costco-token-refresh-host" in markers
    assert "# morning-fleet-deliver-host" in markers


def test_parse_install_script_records_kind(mod, install_script):
    expected = mod.parse_install_script(install_script)
    by_marker = {e["marker"]: e for e in expected}
    assert by_marker["# costco-token-refresh-host"]["kind"] == "direct"
    assert by_marker["# script-contract-shopping-delivery-digest"]["kind"] == "contract"


# ─── find_missing_markers ────────────────────────────────────────────


def test_find_missing_markers_all_present(mod, install_script, crontab_present):
    expected = mod.parse_install_script(install_script)
    missing = mod.find_missing_markers(expected, crontab_present)
    assert missing == []


def test_find_missing_markers_detects_shopping_gap(
    mod, install_script, crontab_missing
):
    expected = mod.parse_install_script(install_script)
    missing = mod.find_missing_markers(expected, crontab_missing)
    markers = [e["marker"] for e in missing]
    assert "# script-contract-shopping-delivery-digest" in markers
    assert "# script-contract-linkedin-keepalive" not in markers


# ─── format_alert ────────────────────────────────────────────────────


def test_format_alert_lists_missing_markers(mod):
    missing = [
        {
            "marker": "# script-contract-shopping-delivery-digest",
            "logname": "shopping-delivery-digest",
            "schedule": "30 10 * * *",
            "kind": "contract",
        }
    ]
    msg = mod.format_alert(missing)
    assert "shopping-delivery-digest" in msg
    assert "30 10" in msg
    # Must mention the manual recovery step
    assert "install-host-cron.sh" in msg


# ─── run() orchestration ─────────────────────────────────────────────


def test_run_silent_when_all_installed(
    mod, install_script, crontab_present, tmp_path, monkeypatch
):
    """Happy path: no `alert` field in the result. The host wrapper
    gates Telegram delivery on the alert field's presence."""
    workspace = tmp_path / "fix-it-workspace"
    (workspace / "cache").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-cron-self-check.json"
    )
    monkeypatch.setattr(mod, "INSTALL_SCRIPT", install_script)
    monkeypatch.setattr(mod, "_read_crontab", lambda: crontab_present)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["expected"] == 5
    assert result["missing"] == 0
    assert "alert" not in result


def test_run_alerts_when_marker_missing(
    mod, install_script, crontab_missing, tmp_path, monkeypatch
):
    """Missing marker → result has `alert` populated. Wrapper sends it."""
    workspace = tmp_path / "fix-it-workspace"
    (workspace / "cache").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-cron-self-check.json"
    )
    monkeypatch.setattr(mod, "INSTALL_SCRIPT", install_script)
    monkeypatch.setattr(mod, "_read_crontab", lambda: crontab_missing)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["missing"] == 1
    assert "alert" in result
    assert "shopping-delivery-digest" in result["alert"]


def test_run_handles_missing_install_script(mod, tmp_path, monkeypatch):
    workspace = tmp_path / "fix-it-workspace"
    (workspace / "cache").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-cron-self-check.json"
    )
    monkeypatch.setattr(mod, "INSTALL_SCRIPT", tmp_path / "missing.sh")
    monkeypatch.setattr(mod, "_read_crontab", lambda: "")

    result = mod.run()
    assert result["status"] == "degraded"
    assert "alert" in result  # wrapper will surface this so the operator knows the check failed


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
