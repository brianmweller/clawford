"""P1.1 — Safeguard 12 (pip-audit supply-chain gate).

Tests for deploy.py's check_pip_audit + filter_blocking_findings +
_run_pip_audit_safeguard. The safeguard runs ONCE per deploy
invocation (not per agent), short-circuits before any backup or
workspace write, and is gated behind the warn/enforce mode env var.

Mode resolution:
  --skip-pip-audit            → skip
  CLAWFORD_PIP_AUDIT_MODE=skip → skip
  pip-audit not installed     → warn-and-continue
  no findings >= threshold    → log clean, proceed
  findings + mode=warn        → log + proceed
  findings + mode=enforce     → log + block
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import deploy  # type: ignore


# ---------------------------------------------------------------------------
# Mode + threshold resolution
# ---------------------------------------------------------------------------


def test_default_mode_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(deploy.PIP_AUDIT_MODE_ENV_VAR, raising=False)
    assert deploy._pip_audit_mode() == "warn"


def test_env_var_overrides_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "enforce")
    assert deploy._pip_audit_mode() == "enforce"


def test_env_var_skip_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "skip")
    assert deploy._pip_audit_mode() == "skip"


def test_invalid_mode_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "loud-and-proud")
    assert deploy._pip_audit_mode() == "warn"


def test_default_blocking_threshold_is_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(deploy.PIP_AUDIT_BLOCKING_SEVERITY_ENV_VAR, raising=False)
    assert deploy._pip_audit_blocking_severity_threshold() == deploy._SEVERITY_RANK["high"]


def test_severity_threshold_can_be_lowered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_BLOCKING_SEVERITY_ENV_VAR, "low")
    assert deploy._pip_audit_blocking_severity_threshold() == deploy._SEVERITY_RANK["low"]


# ---------------------------------------------------------------------------
# _parse_pip_audit_json — handles both shapes pip-audit emits
# ---------------------------------------------------------------------------


def test_parse_envelope_shape() -> None:
    """{"dependencies": [{name, version, vulns: [...]}, ...]}"""
    raw = json.dumps({
        "dependencies": [
            {
                "name": "requests",
                "version": "2.30.0",
                "vulns": [
                    {"id": "GHSA-x", "severity": "HIGH", "fix_versions": ["2.32.0"]},
                ],
            },
            {"name": "clean-pkg", "version": "1.0", "vulns": []},
        ],
    })
    findings = deploy._parse_pip_audit_json(raw)
    assert len(findings) == 1
    f = findings[0]
    assert f["name"] == "requests"
    assert f["version"] == "2.30.0"
    assert f["severity"] == "high"
    assert f["fix_versions"] == ["2.32.0"]
    assert f["cve"] == "GHSA-x"


def test_parse_bare_list_shape() -> None:
    """Some pip-audit versions emit the flat list form."""
    raw = json.dumps([
        {"name": "urllib3", "version": "1.26.0",
         "vulns": [{"id": "CVE-2024-x", "severity": "Critical"}]},
    ])
    findings = deploy._parse_pip_audit_json(raw)
    assert findings[0]["name"] == "urllib3"
    assert findings[0]["severity"] == "critical"


def test_parse_handles_garbage_returns_empty() -> None:
    assert deploy._parse_pip_audit_json("not json at all") == []
    assert deploy._parse_pip_audit_json("") == []
    assert deploy._parse_pip_audit_json('"a string"') == []


# ---------------------------------------------------------------------------
# filter_blocking_findings — severity gate
# ---------------------------------------------------------------------------


def test_filter_keeps_at_or_above_threshold() -> None:
    findings = [
        {"name": "x", "severity": "low"},
        {"name": "y", "severity": "medium"},
        {"name": "z", "severity": "high"},
        {"name": "w", "severity": "critical"},
    ]
    blocking = deploy.filter_blocking_findings(
        findings, threshold_rank=deploy._SEVERITY_RANK["high"],
    )
    names = [f["name"] for f in blocking]
    assert names == ["z", "w"]


def test_filter_drops_unknown_severity() -> None:
    """unknown / unmapped severity falls through as non-blocking."""
    findings = [{"name": "x", "severity": "unknown"}]
    blocking = deploy.filter_blocking_findings(
        findings, threshold_rank=deploy._SEVERITY_RANK["high"],
    )
    assert blocking == []


# ---------------------------------------------------------------------------
# check_pip_audit — runner injection for tests
# ---------------------------------------------------------------------------


def _fake_proc(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_check_pip_audit_clean_run() -> None:
    runner = lambda cmd: _fake_proc(0, json.dumps({"dependencies": []}))
    findings, err = deploy.check_pip_audit(runner=runner)
    assert err is None
    assert findings == []


def test_check_pip_audit_returns_findings() -> None:
    body = json.dumps({"dependencies": [
        {"name": "requests", "version": "2.30.0",
         "vulns": [{"id": "GHSA-x", "severity": "high"}]},
    ]})
    runner = lambda cmd: _fake_proc(1, body)  # pip-audit exits 1 on findings
    findings, err = deploy.check_pip_audit(runner=runner)
    assert err is None
    assert len(findings) == 1


def test_check_pip_audit_missing_binary() -> None:
    def runner(cmd):
        raise FileNotFoundError("pip-audit")
    findings, err = deploy.check_pip_audit(runner=runner)
    assert findings == []
    assert "pip-audit not installed" in err


def test_check_pip_audit_unexpected_exit_is_error() -> None:
    """pip-audit exit codes other than 0/1 indicate a real failure."""
    runner = lambda cmd: _fake_proc(2, "", "missing requirements file")
    findings, err = deploy.check_pip_audit(runner=runner)
    assert findings == []
    assert "pip-audit exit 2" in err


# ---------------------------------------------------------------------------
# _run_pip_audit_safeguard — the dispatcher main() actually calls
# ---------------------------------------------------------------------------


def test_safeguard_skipped_via_cli_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    args = SimpleNamespace(skip_pip_audit=True)
    # Force the runner to blow up if it's called — proves the gate
    # short-circuited before invoking pip-audit.
    monkeypatch.setattr(
        deploy, "check_pip_audit",
        lambda **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    assert deploy._run_pip_audit_safeguard(args) is True


def test_safeguard_skipped_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "skip")
    args = SimpleNamespace(skip_pip_audit=False)
    monkeypatch.setattr(
        deploy, "check_pip_audit",
        lambda **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    assert deploy._run_pip_audit_safeguard(args) is True


def test_safeguard_proceeds_when_pip_audit_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "enforce")
    monkeypatch.setattr(
        deploy, "check_pip_audit",
        lambda **kw: ([], "pip-audit not installed"),
    )
    args = SimpleNamespace(skip_pip_audit=False)
    # Missing tool => warn-and-continue, even in enforce mode (the
    # operator's environment is the gap, not their dependency choices).
    assert deploy._run_pip_audit_safeguard(args) is True


def test_safeguard_proceeds_on_clean_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "enforce")
    monkeypatch.setattr(
        deploy, "check_pip_audit",
        lambda **kw: ([], None),
    )
    args = SimpleNamespace(skip_pip_audit=False)
    assert deploy._run_pip_audit_safeguard(args) is True


def test_safeguard_proceeds_in_warn_mode_with_high_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "warn")
    monkeypatch.setattr(
        deploy, "check_pip_audit",
        lambda **kw: ([
            {"name": "requests", "version": "2.30.0", "cve": "GHSA-x",
             "severity": "high", "fix_versions": ["2.32.0"], "description": "rce"},
        ], None),
    )
    args = SimpleNamespace(skip_pip_audit=False)
    assert deploy._run_pip_audit_safeguard(args) is True


def test_safeguard_blocks_in_enforce_mode_with_high_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "enforce")
    monkeypatch.setattr(
        deploy, "check_pip_audit",
        lambda **kw: ([
            {"name": "requests", "version": "2.30.0", "cve": "GHSA-x",
             "severity": "critical", "fix_versions": ["2.32.0"],
             "description": "rce"},
        ], None),
    )
    args = SimpleNamespace(skip_pip_audit=False)
    assert deploy._run_pip_audit_safeguard(args) is False


def test_safeguard_proceeds_when_only_low_severity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default threshold is 'high'; low/medium findings don't block."""
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "enforce")
    monkeypatch.delenv(deploy.PIP_AUDIT_BLOCKING_SEVERITY_ENV_VAR, raising=False)
    monkeypatch.setattr(
        deploy, "check_pip_audit",
        lambda **kw: ([
            {"name": "x", "version": "1.0", "cve": "GHSA-y",
             "severity": "low", "fix_versions": [], "description": ""},
            {"name": "y", "version": "1.0", "cve": "GHSA-z",
             "severity": "medium", "fix_versions": [], "description": ""},
        ], None),
    )
    args = SimpleNamespace(skip_pip_audit=False)
    assert deploy._run_pip_audit_safeguard(args) is True


def test_severity_threshold_can_be_lowered_to_block_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(deploy.PIP_AUDIT_MODE_ENV_VAR, "enforce")
    monkeypatch.setenv(deploy.PIP_AUDIT_BLOCKING_SEVERITY_ENV_VAR, "medium")
    monkeypatch.setattr(
        deploy, "check_pip_audit",
        lambda **kw: ([
            {"name": "y", "version": "1.0", "cve": "GHSA-z",
             "severity": "medium", "fix_versions": [], "description": ""},
        ], None),
    )
    args = SimpleNamespace(skip_pip_audit=False)
    assert deploy._run_pip_audit_safeguard(args) is False
