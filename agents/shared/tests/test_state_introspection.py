"""Tests for agents/shared/state_introspection.py.

The module parses the ~/.clawford/logs/<agent_id>-*-host.log files
written by ops/scripts/script-contract-host.sh and returns a
PT-relative summary an agent can cite when the operator asks "did you run
today?" instead of confabulating from its static prompt.

Log lines have a stable two-line pattern per run:

    [YYYY-MM-DDTHH:MM:SSZ] <logname> exit=<code>
    {"status": "ok|degraded|error", ...JSON envelope}

Plus occasional interleaved lines that are NOT new-run headers:

    [YYYY-MM-DDTHH:MM:SSZ] <logname> skipped (lock held)
    [YYYY-MM-DDTHH:MM:SSZ] <logname> alert sent (http=200): ...
    [YYYY-MM-DDTHH:MM:SSZ] <logname> alert dropped — FOO_ENV not set
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

from state_introspection import recent_runs  # noqa: E402


PT_OFFSET_HOURS = 7  # PT = UTC-7 (PDT) — tests below are timezone-agnostic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_logs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point state_introspection at a temp directory instead of
    ~/.clawford/logs."""
    monkeypatch.setenv("CLAWFORD_LOGS_DIR", str(tmp_path))
    return tmp_path


def _write_log(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Happy path — single log, single recent run
# ---------------------------------------------------------------------------


def test_parses_single_ok_run(fake_logs_dir: Path) -> None:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    body = (
        f"[{_utc_iso(now_utc)}] connector-auto-compose exit=0\n"
        '{"status": "ok", "processed": 0, "skipped_logged": 1, '
        '"trace_id": "abc", "agent_id": "connector", '
        '"tool_name": "auto-compose"}\n'
    )
    _write_log(fake_logs_dir / "connector-auto-compose-host.log", body)

    result = recent_runs("connector", since_hours=24)

    assert result["agent_id"] == "connector"
    assert result["since_hours"] == 24
    assert len(result["runs"]) == 1
    run = result["runs"][0]
    assert run["name"] == "connector-auto-compose"
    assert run["status"] == "ok"
    assert run["summary"]["processed"] == 0
    assert run["summary"]["skipped_logged"] == 1
    assert result["counts"]["ok"] == 1
    assert result["counts"]["degraded"] == 0
    assert result["counts"]["error"] == 0
    assert result["by_name"]["connector-auto-compose"] == 1


# ---------------------------------------------------------------------------
# PT-relative cutoff
# ---------------------------------------------------------------------------


def test_filters_out_runs_older_than_window(fake_logs_dir: Path) -> None:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    old_utc = now_utc - timedelta(hours=48)
    fresh_utc = now_utc - timedelta(hours=1)

    body = (
        f"[{_utc_iso(old_utc)}] connector-auto-compose exit=0\n"
        '{"status": "ok", "processed": 99}\n'
        f"[{_utc_iso(fresh_utc)}] connector-auto-compose exit=0\n"
        '{"status": "ok", "processed": 1}\n'
    )
    _write_log(fake_logs_dir / "connector-auto-compose-host.log", body)

    result = recent_runs("connector", since_hours=24)

    assert len(result["runs"]) == 1
    assert result["runs"][0]["summary"]["processed"] == 1


def test_since_hours_wider_than_log_captures_all(fake_logs_dir: Path) -> None:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    old_utc = now_utc - timedelta(hours=48)
    fresh_utc = now_utc - timedelta(hours=1)

    body = (
        f"[{_utc_iso(old_utc)}] connector-auto-compose exit=0\n"
        '{"status": "ok", "processed": 99}\n'
        f"[{_utc_iso(fresh_utc)}] connector-auto-compose exit=0\n"
        '{"status": "ok", "processed": 1}\n'
    )
    _write_log(fake_logs_dir / "connector-auto-compose-host.log", body)

    result = recent_runs("connector", since_hours=72)
    assert len(result["runs"]) == 2


# ---------------------------------------------------------------------------
# Multi-file aggregation
# ---------------------------------------------------------------------------


def test_aggregates_across_multiple_logs_for_agent(fake_logs_dir: Path) -> None:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)

    _write_log(
        fake_logs_dir / "connector-auto-compose-host.log",
        f"[{_utc_iso(now_utc)}] connector-auto-compose exit=0\n"
        '{"status": "ok", "processed": 1}\n',
    )
    _write_log(
        fake_logs_dir / "connector-inbox-triage-host.log",
        f"[{_utc_iso(now_utc)}] connector-inbox-triage exit=0\n"
        '{"status": "ok", "threads_scanned": 50, "queued": 0}\n',
    )

    result = recent_runs("connector", since_hours=24)

    assert len(result["runs"]) == 2
    names = {r["name"] for r in result["runs"]}
    assert names == {"connector-auto-compose", "connector-inbox-triage"}


def test_ignores_other_agents_logs(fake_logs_dir: Path) -> None:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)

    _write_log(
        fake_logs_dir / "connector-auto-compose-host.log",
        f"[{_utc_iso(now_utc)}] connector-auto-compose exit=0\n"
        '{"status": "ok", "processed": 1}\n',
    )
    _write_log(
        fake_logs_dir / "shopping-heartbeat-host.log",
        f"[{_utc_iso(now_utc)}] shopping-heartbeat exit=0\n"
        '{"status": "ok"}\n',
    )

    result = recent_runs("connector", since_hours=24)

    assert len(result["runs"]) == 1
    assert result["runs"][0]["name"] == "connector-auto-compose"


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------


def test_counts_degraded_and_error_runs(fake_logs_dir: Path) -> None:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    ts2 = now_utc - timedelta(minutes=30)
    ts3 = now_utc - timedelta(minutes=60)

    body = (
        f"[{_utc_iso(now_utc)}] connector-inbox-triage exit=0\n"
        '{"status": "ok"}\n'
        f"[{_utc_iso(ts2)}] connector-inbox-triage exit=0\n"
        '{"status": "degraded", "alert": "token near expiry"}\n'
        f"[{_utc_iso(ts3)}] connector-inbox-triage exit=1\n"
        '{"status": "error", "alert": "gmail refused"}\n'
    )
    _write_log(fake_logs_dir / "connector-inbox-triage-host.log", body)

    result = recent_runs("connector", since_hours=24)

    assert result["counts"]["ok"] == 1
    assert result["counts"]["degraded"] == 1
    assert result["counts"]["error"] == 1
    # Error run's alert should be surfaced in its summary.
    error_run = next(r for r in result["runs"] if r["status"] == "error")
    assert error_run["summary"]["alert"] == "gmail refused"


# ---------------------------------------------------------------------------
# Robustness — malformed / interleaved lines
# ---------------------------------------------------------------------------


def test_ignores_non_header_bracket_lines(fake_logs_dir: Path) -> None:
    """alert-sent and lock-skipped lines start with [ but aren't run headers."""
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    ts2 = now_utc - timedelta(minutes=10)

    body = (
        f"[{_utc_iso(now_utc)}] connector-auto-compose exit=0\n"
        '{"status": "degraded", "alert": "foo"}\n'
        f"[{_utc_iso(now_utc)}] connector-auto-compose alert sent (http=200): foo\n"
        f"[{_utc_iso(ts2)}] connector-auto-compose skipped (lock held)\n"
    )
    _write_log(fake_logs_dir / "connector-auto-compose-host.log", body)

    result = recent_runs("connector", since_hours=24)

    # One finalized run (the degraded one); the lock-skipped header
    # shouldn't start a new run because it has no envelope AND isn't
    # an exit= header.
    assert len(result["runs"]) == 1
    assert result["runs"][0]["status"] == "degraded"


def test_handles_malformed_json_line(fake_logs_dir: Path) -> None:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    body = (
        f"[{_utc_iso(now_utc)}] connector-auto-compose exit=0\n"
        '{not-valid-json\n'
    )
    _write_log(fake_logs_dir / "connector-auto-compose-host.log", body)

    result = recent_runs("connector", since_hours=24)

    # The header is present but envelope is unparseable; we still
    # surface the run, marked status=unknown, not crash.
    assert len(result["runs"]) == 1
    assert result["runs"][0]["status"] == "unknown"


def test_no_logs_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the logs dir doesn't exist, return an empty result — don't crash."""
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("CLAWFORD_LOGS_DIR", str(missing))
    result = recent_runs("connector", since_hours=24)
    assert result["runs"] == []
    assert result["counts"] == {"ok": 0, "degraded": 0, "error": 0, "unknown": 0}


def test_no_logs_for_agent(fake_logs_dir: Path) -> None:
    """Directory exists but no matching agent logs — empty result."""
    _write_log(
        fake_logs_dir / "shopping-heartbeat-host.log",
        "[2026-04-20T00:00:00Z] shopping-heartbeat exit=0\n"
        '{"status": "ok"}\n',
    )
    result = recent_runs("connector", since_hours=24)
    assert result["runs"] == []
