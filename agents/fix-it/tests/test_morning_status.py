"""Tests for agents/fix-it/scripts/morning-status.py.

R4 of the registry-based agent health system. Replaces the
LLM-driven morning-status cron with a pure Python script that:
  - reads ~/Dropbox/openclaw-backup/fleet-health.json (R3 output)
  - reads ~/Dropbox/openclaw-backup/fix-it/KNOWN_ISSUES.md
  - runs validate.py and captures the summary line
  - finds Dropbox "conflicted copy" files
  - classifies each agent into one of five buckets: DOWN / HEALTHY /
    KNOWN / STALE / OPEN ALERT
  - formats the emoji-headed morning status report
  - writes cache/morning-brief-ready.txt

The 6-rule classification logic (preserved from the existing LLM
cron message):

  (a) heartbeat > 6h old           → DOWN (whole fleet-health stale)
  (b) status == "ok"               → HEALTHY (ignore everything else)
  (c) status == degraded + match   → KNOWN
  (d) status == degraded + stale   → STALE
  (e) status == degraded + fresh   → OPEN ALERT
  (f) default                      → HEALTHY

Run: cd agents/fix-it && python3 -m pytest tests/test_morning_status.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_morning_status():
    path = SCRIPTS_DIR / "morning-status.py"
    spec = importlib.util.spec_from_file_location("fixit_morning_status", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fixit_morning_status"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def stub_paths(tmp_path, monkeypatch):
    """Set up stub fleet-health.json, KNOWN_ISSUES.md, and output dir."""
    brain = tmp_path / "openclaw-backup"
    fix_it_dir = brain / "fix-it"
    fix_it_dir.mkdir(parents=True)
    cache = tmp_path / "fix-it-workspace" / "cache"
    cache.mkdir(parents=True)

    # Default empty KNOWN_ISSUES.md
    (fix_it_dir / "KNOWN_ISSUES.md").write_text("", encoding="utf-8")

    fleet_health_path = brain / "fleet-health.json"
    output_path = cache / "morning-brief-ready.txt"

    mod = _load_morning_status()
    monkeypatch.setattr(mod, "BRAIN", str(brain))
    monkeypatch.setattr(mod, "FLEET_HEALTH_PATH", str(fleet_health_path))
    monkeypatch.setattr(mod, "KNOWN_ISSUES_PATH", str(fix_it_dir / "KNOWN_ISSUES.md"))
    monkeypatch.setattr(mod, "OUTPUT_PATH", str(output_path))

    # Stub external commands so tests are hermetic
    monkeypatch.setattr(mod, "_run_validate", lambda: ("PASS", 25, 0, 0))
    monkeypatch.setattr(mod, "_find_conflicted_copies", lambda: [])

    return mod, brain, output_path, fleet_health_path, fix_it_dir / "KNOWN_ISSUES.md"


def _write_fleet_health(path, agents: dict, generated_offset_min: int = 0):
    """Helper: write a fleet-health.json with the given agent reports.

    `agents` is a {id: probe_dict} mapping. Each probe dict goes into
    the FleetHealthReport agents{} field as { id, probe_ts, status, probes, [error], [alert] }.
    `generated_offset_min` shifts the report's generated_at timestamp
    (negative = in the past — used to test the >6h DOWN rule).
    """
    now = datetime.now(timezone.utc) + timedelta(minutes=generated_offset_min)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "generated_at": ts,
        "agents": {
            agent_id: {
                "id": agent_id,
                "probe_ts": ts,
                "status": probe["status"],
                "probes": probe.get("probes", {}),
                **({"error": probe["error"]} if "error" in probe else {}),
                **({"alert": probe["alert"]} if "alert" in probe else {}),
            }
            for agent_id, probe in agents.items()
        },
    }
    path.write_text(json.dumps(report))


# ─── classification rule (a): fleet-health stale → DOWN ─────────────


def test_fleet_health_stale_marks_all_agents_down(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    # generated_at >6h ago → whole fleet is DOWN
    _write_fleet_health(fleet_health, {
        "shopping": {"status": "ok"},
        "connector": {"status": "ok"},
    }, generated_offset_min=-7 * 60)

    result = mod.run()
    assert result["overall"] == "down"
    assert all(b == "down" for b in result["buckets"].values())


# ─── rule (b): status=ok → HEALTHY (ignore error_log) ───────────────


def test_status_ok_classified_healthy_regardless_of_other_fields(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    _write_fleet_health(fleet_health, {
        "shopping": {"status": "ok", "probes": {"costco_session": {"status": "ok"}}},
        "connector": {"status": "ok"},
    })

    result = mod.run()
    assert result["overall"] == "healthy"
    assert result["buckets"]["shopping"] == "healthy"
    assert result["buckets"]["connector"] == "healthy"


# ─── rule (c): degraded + KNOWN_ISSUES match → KNOWN ────────────────


def test_degraded_with_known_issue_match_classified_known(stub_paths):
    mod, brain, output, fleet_health, ki = stub_paths
    # A known-issue entry that matches "costco JWT expired"
    ki.write_text(
        "- **match:** costco.*JWT.*expired\n"
        "- **expires:** 2099-12-31\n"
        "- **reason:** Test suppression — not a real outage\n",
        encoding="utf-8",
    )

    _write_fleet_health(fleet_health, {
        "shopping": {
            "status": "degraded",
            "alert": "⚠️ shopping degraded: costco JWT expired",
        },
    })

    result = mod.run()
    assert result["buckets"]["shopping"] == "known"
    assert "Test suppression" in result["known_reasons"]["shopping"]


def test_degraded_with_expired_known_issue_falls_through_to_alert(stub_paths):
    mod, brain, output, fleet_health, ki = stub_paths
    # Known issue but expires date is in the past
    ki.write_text(
        "- **match:** costco.*JWT.*expired\n"
        "- **expires:** 2020-01-01\n"
        "- **reason:** Stale entry\n",
        encoding="utf-8",
    )
    _write_fleet_health(fleet_health, {
        "shopping": {
            "status": "degraded",
            "alert": "⚠️ shopping degraded: costco JWT expired",
        },
    })

    result = mod.run()
    assert result["buckets"]["shopping"] == "alert"  # NOT known


# ─── rule (e): degraded + fresh + no match → OPEN ALERT ─────────────


def test_degraded_fresh_no_known_issue_classified_alert(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    _write_fleet_health(fleet_health, {
        "shopping": {
            "status": "degraded",
            "alert": "⚠️ shopping degraded: amazon cookies stale",
        },
        "connector": {"status": "ok"},
    })

    result = mod.run()
    assert result["overall"] == "alert"
    assert result["buckets"]["shopping"] == "alert"
    assert result["buckets"]["connector"] == "healthy"


# ─── overall bucket precedence ──────────────────────────────────────


def test_overall_alert_when_any_alert(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    _write_fleet_health(fleet_health, {
        "shopping": {"status": "ok"},
        "connector": {"status": "degraded", "alert": "⚠️ connector test alert"},
    })
    result = mod.run()
    assert result["overall"] == "alert"


def test_overall_known_when_all_degraded_are_known(stub_paths):
    mod, brain, output, fleet_health, ki = stub_paths
    ki.write_text(
        "- **match:** test\\s*alert\n"
        "- **expires:** 2099-12-31\n"
        "- **reason:** Test\n",
        encoding="utf-8",
    )
    _write_fleet_health(fleet_health, {
        "shopping": {"status": "ok"},
        "connector": {"status": "degraded", "alert": "test alert"},
    })
    result = mod.run()
    assert result["overall"] == "known"


def test_overall_healthy_when_all_ok(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    _write_fleet_health(fleet_health, {
        "shopping": {"status": "ok"},
        "connector": {"status": "ok"},
    })
    result = mod.run()
    assert result["overall"] == "healthy"


# ─── KNOWN_ISSUES.md parser ──────────────────────────────────────────


def test_known_issues_parser_handles_multiple_entries(stub_paths):
    mod, brain, output, fleet_health, ki = stub_paths
    ki.write_text(
        "- **match:** pattern1\n"
        "- **expires:** 2099-12-31\n"
        "- **reason:** First\n"
        "\n---\n\n"
        "- **match:** pattern2\n"
        "- **expires:** 2099-12-31\n"
        "- **reason:** Second\n",
        encoding="utf-8",
    )
    issues = mod.parse_known_issues(ki.read_text())
    assert len(issues) == 2
    assert issues[0].pattern == "pattern1"
    assert issues[0].reason == "First"
    assert issues[1].pattern == "pattern2"


def test_known_issues_parser_skips_expired_entries(stub_paths):
    mod, brain, output, fleet_health, ki = stub_paths
    ki.write_text(
        "- **match:** old-pattern\n"
        "- **expires:** 2020-01-01\n"
        "- **reason:** Past\n"
        "\n---\n\n"
        "- **match:** new-pattern\n"
        "- **expires:** 2099-12-31\n"
        "- **reason:** Future\n",
        encoding="utf-8",
    )
    issues = mod.parse_known_issues(ki.read_text())
    # Expired entries are still parsed, but match_against() filters them
    assert len(issues) == 2
    # Direct check: matching against an expired pattern returns None
    assert mod.match_known_issue(issues, "old-pattern") is None
    assert mod.match_known_issue(issues, "new-pattern") is not None


# ─── report formatting ──────────────────────────────────────────────


def test_run_writes_morning_brief_ready_with_emoji_header(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    _write_fleet_health(fleet_health, {
        "shopping": {"status": "ok"},
        "connector": {"status": "ok"},
        "family-calendar": {"status": "ok"},
        "meetings-coach": {"status": "ok"},
        "news-digest": {"status": "ok"},
        "fix-it": {"status": "ok"},
    })

    mod.run()
    text = output.read_text(encoding="utf-8")
    assert "🦊🔧 Morning Status" in text
    assert "Overall: ✅" in text
    assert "Agents:" in text
    assert "shopping" in text
    assert "Brain:" in text


def test_run_includes_open_alerts_section_when_present(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    _write_fleet_health(fleet_health, {
        "shopping": {"status": "degraded", "alert": "⚠️ shopping degraded: costco"},
    })
    mod.run()
    text = output.read_text(encoding="utf-8")
    assert "🚨 Open alerts" in text
    assert "shopping" in text


def test_run_omits_known_section_when_no_known(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    _write_fleet_health(fleet_health, {
        "shopping": {"status": "ok"},
    })
    mod.run()
    text = output.read_text(encoding="utf-8")
    assert "ℹ️ Known" not in text


def test_run_includes_brain_validation_line(stub_paths, monkeypatch):
    mod, brain, output, fleet_health, _ki = stub_paths
    monkeypatch.setattr(mod, "_run_validate", lambda: ("PASS", 25, 0, 17))
    _write_fleet_health(fleet_health, {"shopping": {"status": "ok"}})
    mod.run()
    text = output.read_text(encoding="utf-8")
    assert "Brain: PASS (25 pass, 0 fail, 17 warn)" in text


def test_run_includes_fleet_health_freshness_line(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    # Fleet-health generated 12 minutes ago
    _write_fleet_health(
        fleet_health,
        {"shopping": {"status": "ok"}},
        generated_offset_min=-12,
    )
    mod.run()
    text = output.read_text(encoding="utf-8")
    assert "Fleet-health: generated " in text
    assert "UTC (" in text
    assert "ago)" in text
    # The old vestigial line must be gone
    assert "Platform heartbeat" not in text


def test_run_fleet_health_age_formats_hours_when_over_60min(stub_paths):
    mod, brain, output, fleet_health, _ki = stub_paths
    # 3h 5m old but still fresh (< 6h threshold)
    _write_fleet_health(
        fleet_health,
        {"shopping": {"status": "ok"}},
        generated_offset_min=-(3 * 60 + 5),
    )
    mod.run()
    text = output.read_text(encoding="utf-8")
    assert "3h 5m ago" in text


# ─── main() SCRIPT_CONTRACT ──────────────────────────────────────────


def test_main_prints_one_json_line_and_exits_zero(stub_paths, capsys):
    mod, brain, output, fleet_health, _ki = stub_paths
    _write_fleet_health(fleet_health, {"shopping": {"status": "ok"}})
    rc = mod.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert "status" in payload
    assert payload["status"] in ("ok", "degraded", "error")


def test_main_emits_error_json_when_fleet_health_missing(stub_paths, capsys):
    mod, brain, output, fleet_health, _ki = stub_paths
    # Don't write fleet-health.json
    rc = mod.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "alert" in payload
