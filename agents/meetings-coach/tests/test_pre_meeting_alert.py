"""Tests for agents/meetings-coach/scripts/pre-meeting-alert.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`meetings-coach:pre-meeting-alert`. Runs gcal-fetch.py, filters to real
meetings starting in 15-45 min, dedupes via sent-alerts.json, and sends
one Telegram alert per meeting with any existing open commitments and
Workflowy agenda items. No LLM — all composition is over structured
prep output (logic-gate rule).
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "pre-meeting-alert.py"
FIXTURES = Path(__file__).parent / "fixtures" / "pre-meeting-alert"


def _load():
    spec = importlib.util.spec_from_file_location("pre_meeting_alert", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def gcal():
    with open(FIXTURES / "gcal-fetch.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def prep_alexis():
    with open(FIXTURES / "meeting-prep-alexis.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def prep_board():
    with open(FIXTURES / "meeting-prep-board.json", encoding="utf-8") as f:
        return json.load(f)


# ─── filter_upcoming ─────────────────────────────────────────────────


def test_filter_upcoming_keeps_real_meetings_in_window(mod, gcal):
    """Now = 23:00 UTC = 16:00 PT. Window = 15-45 minutes. Keeps
    events at 25 min (Alexis 16:25), 35 min (Portfolio 16:35), and
    40 min (Board 16:40)."""
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    filtered = mod.filter_upcoming(gcal["events"], now_utc)
    ids = [e["id"] for e in filtered]
    assert "evt-in-window-1" in ids  # Alexis 25 min
    assert "evt-in-window-2" in ids  # Board 40 min
    assert "evt-already-alerted" in ids  # Portfolio 35 min (dedup happens later)


def test_filter_upcoming_drops_too_soon(mod, gcal):
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    filtered = mod.filter_upcoming(gcal["events"], now_utc)
    ids = [e["id"] for e in filtered]
    assert "evt-too-soon" not in ids  # 10 min away


def test_filter_upcoming_drops_too_far(mod, gcal):
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    filtered = mod.filter_upcoming(gcal["events"], now_utc)
    ids = [e["id"] for e in filtered]
    assert "evt-too-far" not in ids  # 90 min away


def test_filter_upcoming_drops_task_blocks(mod, gcal):
    """Events with is_real_meeting=False must be dropped, even if the
    start time falls inside the window."""
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    filtered = mod.filter_upcoming(gcal["events"], now_utc)
    ids = [e["id"] for e in filtered]
    assert "evt-task-block" not in ids


# ─── format_alert ────────────────────────────────────────────────────


def test_format_alert_includes_meeting_title_and_attendees(mod, gcal, prep_alexis):
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    event = next(e for e in gcal["events"] if e["id"] == "evt-in-window-1")
    msg = mod.format_alert(event, prep_alexis, [], now_utc)
    assert "Alexis 1:1" in msg
    assert "Alexis Lloyd" in msg
    # 25 minutes until start
    assert "25 min" in msg


def test_format_alert_surfaces_open_commitments(mod, gcal, prep_alexis):
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    event = next(e for e in gcal["events"] if e["id"] == "evt-in-window-1")
    msg = mod.format_alert(event, prep_alexis, [], now_utc)
    assert "Q2 roadmap draft" in msg


def test_format_alert_includes_workflowy_agenda_when_present(mod, gcal, prep_alexis):
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    event = next(e for e in gcal["events"] if e["id"] == "evt-in-window-1")
    agenda = ["Discuss Q3 headcount", "Review board deck draft"]
    msg = mod.format_alert(event, prep_alexis, agenda, now_utc)
    assert "Agenda" in msg
    assert "Discuss Q3 headcount" in msg
    assert "Review board deck draft" in msg


def test_format_alert_no_agenda_section_when_empty(mod, gcal, prep_board):
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    event = next(e for e in gcal["events"] if e["id"] == "evt-in-window-2")
    msg = mod.format_alert(event, prep_board, [], now_utc)
    assert "Agenda" not in msg


def test_format_alert_renders_professional_compact_block(mod, gcal):
    """A recruiter meeting should carry a compact prep header + one
    question + one talking point (not the full morning-brief shape)."""
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    event = next(e for e in gcal["events"] if e["id"] == "evt-in-window-1")
    prep = {
        "meetings": [{
            "meeting_id": event["id"],
            "meeting_type": "recruiter-screen",
            "self_context": {
                "target_company": {"company": "Anthropic", "tier_company": "A"},
                "active_pipeline_stage": {"stage": "hiring-manager"},
            },
            "llm_prep": {
                "compelling_angle": "AI safety meets scale — natural extension.",
                "evaluation_questions": ["Top question?", "Question 2", "Question 3"],
                "fit_evidence": ["Top drop-in", "TP2", "TP3"],
                "red_flags": [],
            },
            "context": {"commitments": []},
        }],
    }
    msg = mod.format_alert(event, prep, [], now_utc)
    assert "recruiter-screen" in msg
    assert "Anthropic" in msg
    assert "hiring-manager" in msg
    assert "AI safety" in msg
    assert "Top question?" in msg
    assert "Top drop-in" in msg
    # Only one of each (compact) — Question 2 should NOT appear.
    assert "Question 2" not in msg
    assert "TP2" not in msg


def test_format_alert_skips_professional_block_for_general_meeting(mod, gcal, prep_alexis):
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    event = next(e for e in gcal["events"] if e["id"] == "evt-in-window-1")
    # prep_alexis fixture has no meeting_type — should not render professional block.
    msg = mod.format_alert(event, prep_alexis, [], now_utc)
    assert "recruiter-screen" not in msg
    assert "Ask:" not in msg


def test_format_alert_degrades_to_header_only_on_llm_prep_error(mod, gcal):
    now_utc = datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    event = next(e for e in gcal["events"] if e["id"] == "evt-in-window-1")
    prep = {
        "meetings": [{
            "meeting_id": event["id"],
            "meeting_type": "hiring-panel",
            "self_context": {"target_company": None, "active_pipeline_stage": None},
            "llm_prep": {"error": "timeout"},
            "context": {"commitments": []},
        }],
    }
    msg = mod.format_alert(event, prep, [], now_utc)
    assert "hiring-panel" in msg
    assert "Ask:" not in msg
    assert "Surface:" not in msg


# ─── sent-alerts.json dedup ──────────────────────────────────────────


def test_load_sent_ids_tolerates_missing_file(mod, tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(mod, "SENT_ALERTS_FILE", missing)
    ids = mod._load_sent_ids()
    assert ids == set()


def test_load_sent_ids_reads_list_shape(mod, tmp_path, monkeypatch):
    f = tmp_path / "sent-alerts.json"
    f.write_text(json.dumps([{"id": "a"}, {"id": "b"}]), encoding="utf-8")
    monkeypatch.setattr(mod, "SENT_ALERTS_FILE", f)
    assert mod._load_sent_ids() == {"a", "b"}


def test_append_sent_ids_creates_and_appends(mod, tmp_path, monkeypatch):
    f = tmp_path / "sent-alerts.json"
    monkeypatch.setattr(mod, "SENT_ALERTS_FILE", f)
    mod._append_sent_ids(["a", "b"])
    mod._append_sent_ids(["b", "c"])  # b is deduped
    entries = json.loads(f.read_text(encoding="utf-8"))
    ids = {e["id"] for e in entries}
    assert ids == {"a", "b", "c"}


# ─── run() orchestration ─────────────────────────────────────────────


def test_run_sends_per_new_meeting_and_skips_already_alerted(
    mod, gcal, prep_alexis, prep_board, tmp_path, monkeypatch
):
    workspace = tmp_path / "meetings-coach-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    sent_alerts = workspace / "sent-alerts.json"
    sent_alerts.write_text(
        json.dumps([{"id": "evt-already-alerted", "alerted_at": "2026-04-15T22:30:00Z"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(mod, "SENT_ALERTS_FILE", sent_alerts)
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-pre-meeting.json"
    )

    def fake_run_script(script, *args, **kw):
        if script == "gcal-fetch.py":
            return gcal
        if script == "meeting-prep.py":
            if "evt-in-window-1" in args:
                return prep_alexis
            if "evt-in-window-2" in args:
                return prep_board
        if script == "workflowy-sync.py":
            return {"status": "ok", "agenda": []}
        return None

    monkeypatch.setattr(mod, "_run_script", fake_run_script)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    # 3 in window (Alexis 25 min, Portfolio 35 min, Board 40 min)
    # Portfolio is in sent-alerts → skip → 2 sent
    assert result["upcoming"] == 3
    assert result["sent"] == 2
    # Both new IDs should now be in sent-alerts.json
    persisted = {e["id"] for e in json.loads(sent_alerts.read_text(encoding="utf-8"))}
    assert "evt-in-window-1" in persisted
    assert "evt-in-window-2" in persisted
    assert "evt-already-alerted" in persisted
    # Neither sent message mentions Portfolio (already alerted) or Focus block
    assert not any("Portfolio" in m for m in sent)


def test_run_silent_when_nothing_in_window(mod, tmp_path, monkeypatch):
    workspace = tmp_path / "meetings-coach-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(mod, "SENT_ALERTS_FILE", workspace / "sent-alerts.json")
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-pre-meeting.json"
    )
    monkeypatch.setattr(
        mod, "_run_script",
        lambda *a, **kw: {"status": "ok", "events": []} if a[0] == "gcal-fetch.py" else None,
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["upcoming"] == 0
    assert result["sent"] == 0
    assert sent == []


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
