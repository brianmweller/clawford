"""P0.2 — Doctor Agent (cognitive heartbeat) tests.

doctor-audit.py is Mr Fixit's drift auditor. It reads each agent's
SOUL + MEMORY + fleet-health probe block, asks an LLM to identify
anomalies, and appends findings to drift-audit.md. These tests
exercise the analysis logic with a stubbed LLM — no live network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(monkeypatch: pytest.MonkeyPatch, brain_root: Path):
    """Load doctor-audit.py with a fresh sys.path + brain root override."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))
    # Force-reload brain so it picks up the env var change.
    for m in ("brain", "llm"):
        sys.modules.pop(m, None)
    script = REPO_ROOT / "agents" / "fix-it" / "scripts" / "doctor-audit.py"
    spec = importlib.util.spec_from_file_location("doctor_audit", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def da(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return _load(monkeypatch, tmp_path / "brain")


@dataclass
class FakeInferResult:
    ok: bool = True
    text: str = ""
    error: str = ""
    trace_id: str = ""


def _seed_agent_brain(brain_root: Path, agent_id: str, soul: str, memory: str) -> None:
    agent_dir = brain_root / "agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "SOUL.md").write_text(soul, encoding="utf-8")
    (agent_dir / "MEMORY.md").write_text(memory, encoding="utf-8")


def _seed_fleet_health(brain_root: Path, agents: dict) -> None:
    brain_root.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": "2026-04-17T00:00:00Z", "agents": agents}
    (brain_root / "fleet-health.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def test_default_is_report_only(da) -> None:
    args = da.parse_args([])
    assert args["report_only"] is True
    assert args["alert"] is False
    assert args["severity"] == da.DEFAULT_SEVERITY_THRESHOLD


def test_alert_flag(da) -> None:
    args = da.parse_args(["--alert"])
    assert args["alert"] is True
    assert args["report_only"] is False


def test_severity_flag(da) -> None:
    args = da.parse_args(["--severity", "3"])
    assert args["severity"] == 3


def test_agent_filter_flag(da) -> None:
    args = da.parse_args(["--agent", "shopping"])
    assert args["agent_filter"] == "shopping"


# ---------------------------------------------------------------------------
# load_agent_doc — handles missing files + truncation
# ---------------------------------------------------------------------------


def test_load_agent_doc_missing_returns_empty(da, tmp_path: Path) -> None:
    assert da.load_agent_doc("nonexistent", "SOUL.md", 1000) == ""


def test_load_agent_doc_reads_file(da, tmp_path: Path) -> None:
    _seed_agent_brain(tmp_path / "brain", "shopping", "I am Hilda.", "memory text")
    soul = da.load_agent_doc("shopping", "SOUL.md", 1000)
    assert "I am Hilda." in soul


def test_load_agent_doc_truncates_huge_files(da, tmp_path: Path) -> None:
    big = "x" * 50_000
    _seed_agent_brain(tmp_path / "brain", "shopping", big, "")
    soul = da.load_agent_doc("shopping", "SOUL.md", 100)
    assert len(soul) <= 100
    assert "[truncated]" in soul


# ---------------------------------------------------------------------------
# load_agent_health_block — graceful handling of missing fleet-health
# ---------------------------------------------------------------------------


def test_load_health_block_missing_fleet_health(da, tmp_path: Path) -> None:
    assert da.load_agent_health_block("shopping") == {}


def test_load_health_block_returns_per_agent_dict(da, tmp_path: Path) -> None:
    _seed_fleet_health(tmp_path / "brain", {
        "shopping": {"id": "shopping", "status": "ok"},
        "connector": {"id": "connector", "status": "degraded"},
    })
    block = da.load_agent_health_block("connector")
    assert block["status"] == "degraded"


# ---------------------------------------------------------------------------
# audit_one_agent — happy path, error path, malformed reply
# ---------------------------------------------------------------------------


def test_audit_one_agent_clean(da, tmp_path: Path) -> None:
    _seed_agent_brain(tmp_path / "brain", "shopping", "Hilda role.", "ok memory")
    _seed_fleet_health(tmp_path / "brain", {"shopping": {"status": "ok"}})

    fake = lambda *a, **kw: FakeInferResult(
        ok=True, text='{"anomalies": []}',
    )
    out = da.audit_one_agent(
        {"id": "shopping", "display_name": "Hilda"}, infer_fn=fake,
    )
    assert out["ok"] is True
    assert out["anomalies"] == []


def test_audit_one_agent_with_drift(da, tmp_path: Path) -> None:
    _seed_agent_brain(tmp_path / "brain", "shopping", "Role", "Memory")
    fake = lambda *a, **kw: FakeInferResult(
        ok=True,
        text=json.dumps({
            "anomalies": [
                {
                    "kind": "stale_session",
                    "severity": 2,
                    "summary": "Costco token expired 3x in 24h",
                    "evidence": "last_cron_result repeats token-expired",
                    "suggested_action": "refresh_session",
                    "suggested_target": "costco",
                },
            ],
        }),
    )
    out = da.audit_one_agent(
        {"id": "shopping", "display_name": "Hilda"}, infer_fn=fake,
    )
    assert out["ok"] is True
    assert len(out["anomalies"]) == 1
    a = out["anomalies"][0]
    assert a["kind"] == "stale_session"
    assert a["severity"] == 2
    assert a["suggested_action"] == "refresh_session"
    assert a["suggested_target"] == "costco"


def test_audit_one_agent_handles_llm_error(da, tmp_path: Path) -> None:
    fake = lambda *a, **kw: FakeInferResult(ok=False, error="rate-limited")
    out = da.audit_one_agent(
        {"id": "shopping", "display_name": "Hilda"}, infer_fn=fake,
    )
    assert out["ok"] is False
    assert "rate-limited" in out["error"]


def test_audit_one_agent_handles_unparseable_json(da, tmp_path: Path) -> None:
    fake = lambda *a, **kw: FakeInferResult(
        ok=True, text="I cannot answer in JSON today, sorry.",
    )
    out = da.audit_one_agent(
        {"id": "shopping", "display_name": "Hilda"}, infer_fn=fake,
    )
    assert out["ok"] is False
    assert "unparseable" in out["error"]


def test_audit_one_agent_coerces_sloppy_anomaly_shapes(da, tmp_path: Path) -> None:
    """The LLM might return a list with stray strings / missing fields.
    We coerce to a stable shape rather than crash downstream."""
    fake = lambda *a, **kw: FakeInferResult(
        ok=True,
        text=json.dumps({
            "anomalies": [
                "garbage string",  # not a dict — should be dropped
                {"kind": "drift", "severity": "two"},  # bad severity int
                {"summary": "missing fields"},  # missing kind/severity
            ],
        }),
    )
    out = da.audit_one_agent(
        {"id": "x", "display_name": "X"}, infer_fn=fake,
    )
    assert out["ok"] is True
    # garbage string dropped, two dicts coerced to defaults.
    assert len(out["anomalies"]) == 2
    for a in out["anomalies"]:
        assert "kind" in a
        assert isinstance(a["severity"], int)


# ---------------------------------------------------------------------------
# append_audit_entry — writes to drift-audit.md, counts above threshold
# ---------------------------------------------------------------------------


def test_append_audit_clean_run(da, tmp_path: Path) -> None:
    audits = [{"agent_id": "shopping", "ok": True, "anomalies": []}]
    entry, flagged = da.append_audit_entry(audits, threshold=2)
    assert flagged == 0
    drift_md = tmp_path / "brain" / "fix-it" / "drift-audit.md"
    assert drift_md.exists()
    content = drift_md.read_text(encoding="utf-8")
    assert "shopping**: clean" in content


def test_append_audit_filters_below_threshold(da, tmp_path: Path) -> None:
    audits = [{
        "agent_id": "shopping", "ok": True,
        "anomalies": [
            {"kind": "drift", "severity": 1, "summary": "low",
             "evidence": "", "suggested_action": "none", "suggested_target": ""},
        ],
    }]
    entry, flagged = da.append_audit_entry(audits, threshold=2)
    assert flagged == 0  # severity 1 below threshold 2
    content = (tmp_path / "brain" / "fix-it" / "drift-audit.md").read_text(encoding="utf-8")
    assert "shopping**: clean" in content  # treated as clean at threshold 2


def test_append_audit_records_above_threshold(da, tmp_path: Path) -> None:
    audits = [{
        "agent_id": "shopping", "ok": True,
        "anomalies": [
            {"kind": "stale_session", "severity": 3, "summary": "Costco token dead",
             "evidence": "last 5 runs degraded", "suggested_action": "refresh_session",
             "suggested_target": "costco"},
        ],
    }]
    entry, flagged = da.append_audit_entry(audits, threshold=2)
    assert flagged == 1
    content = (tmp_path / "brain" / "fix-it" / "drift-audit.md").read_text(encoding="utf-8")
    assert "Costco token dead" in content
    assert "refresh_session" in content
    assert "costco" in content


def test_append_audit_records_failed_audit(da, tmp_path: Path) -> None:
    audits = [{"agent_id": "shopping", "ok": False, "error": "llm down"}]
    entry, flagged = da.append_audit_entry(audits, threshold=2)
    assert flagged == 0
    content = (tmp_path / "brain" / "fix-it" / "drift-audit.md").read_text(encoding="utf-8")
    assert "audit failed" in content
    assert "llm down" in content


# ---------------------------------------------------------------------------
# end-to-end run() — fleet manifest + brain + LLM stubs
# ---------------------------------------------------------------------------


def test_run_end_to_end_report_only(
    da, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed two agents in the brain.
    for aid in ("shopping", "connector"):
        _seed_agent_brain(tmp_path / "brain", aid, f"Role for {aid}", "")
    _seed_fleet_health(tmp_path / "brain", {
        "shopping": {"status": "ok"},
        "connector": {"status": "ok"},
    })

    # Filter to just shopping for a tight test.
    fake = lambda *a, **kw: FakeInferResult(ok=True, text='{"anomalies": []}')
    monkeypatch.setattr(da.llm, "infer", fake)

    summary = da.run(["--agent", "shopping", "--severity", "2"])

    assert summary["status"] == "ok"
    assert summary["agents_audited"] == 1
    assert summary["anomalies_above_threshold"] == 0
    assert summary["mode"] == "report-only"
    drift_md = tmp_path / "brain" / "fix-it" / "drift-audit.md"
    assert drift_md.exists()


def test_run_alert_mode_includes_alert_text_when_anomalies(
    da, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_agent_brain(tmp_path / "brain", "shopping", "Role", "")
    _seed_fleet_health(tmp_path / "brain", {"shopping": {"status": "degraded"}})

    fake = lambda *a, **kw: FakeInferResult(
        ok=True,
        text=json.dumps({"anomalies": [
            {"kind": "stale_session", "severity": 3,
             "summary": "Costco token", "evidence": "x",
             "suggested_action": "refresh_session", "suggested_target": "costco"},
        ]}),
    )
    monkeypatch.setattr(da.llm, "infer", fake)

    summary = da.run(["--agent", "shopping", "--alert"])

    assert summary["mode"] == "alert"
    assert summary["anomalies_above_threshold"] == 1
    assert "alert_text" in summary
    assert "Costco token" in summary["alert_text"]


def test_main_always_returns_zero_even_on_exception(
    da, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Script-contract: main() must always exit 0; errors land in JSON."""
    def boom(*a, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(da, "run", boom)
    code = da.main()
    assert code == 0
    out = capsys.readouterr().out.strip()
    obj = json.loads(out.splitlines()[-1])
    assert obj["status"] == "error"
    assert "kaboom" in obj["error"]
