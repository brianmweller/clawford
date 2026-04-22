"""Tests for Huckle Cat's Phase C producer tools."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(AGENT_DIR))


@pytest.fixture
def tools_mod(tmp_path, monkeypatch):
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir()
    brain_root = tmp_path / "brain"
    (brain_root / "people").mkdir(parents=True)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))
    for mod in list(sys.modules):
        if mod in ("tools", "brain"):
            del sys.modules[mod]
    import tools
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "CHECKIN_LOG_PATH", str(workspace / "checkin-log.json"))
    monkeypatch.setattr(tools, "CONFIG_PATH", str(workspace / "connector-config.json"))
    tools._TEST_BRAIN_ROOT = brain_root  # type: ignore[attr-defined]
    return tools


def _seed_person(tools_mod, name: str, circles: str = "friends-close") -> str:
    """Write a minimal person file so list_persons / get_person resolve.

    People files use H1 for display name and `- **key:** value` lines
    for frontmatter — matching brain._parse_people_md.
    """
    slug = name.lower().replace(" ", "-")
    path = tools_mod._TEST_BRAIN_ROOT / "people" / f"{slug}.md"
    path.write_text(
        f"# {name}\n\n- **circles:** {circles}\n",
        encoding="utf-8",
    )
    return slug


def test_mark_checkin_logs_contact(tools_mod):
    _seed_person(tools_mod, "John Smith")
    result = tools_mod.mark_checkin("John Smith")
    assert result["status"] == "ok"
    assert result["person"] == "John Smith"
    assert result["slug"] == "john-smith"

    with open(tools_mod.CHECKIN_LOG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["checkins"]) == 1
    assert data["checkins"][0]["person"] == "John Smith"
    assert data["checkins"][0]["slug"] == "john-smith"
    assert data["checkins"][0]["source"] == "manual"


def test_mark_checkin_appends_multiple(tools_mod):
    _seed_person(tools_mod, "Alice")
    _seed_person(tools_mod, "Bob")
    _seed_person(tools_mod, "Carol")
    tools_mod.mark_checkin("Alice")
    tools_mod.mark_checkin("Bob")
    tools_mod.mark_checkin("Carol")

    with open(tools_mod.CHECKIN_LOG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["checkins"]) == 3


def test_mark_checkin_unknown_person_returns_not_found(tools_mod):
    """Fuzzy-resolver behavior: descriptor that matches no person file
    returns status=not_found so the LLM asks the operator to disambiguate
    rather than silently writing a dangling log entry."""
    result = tools_mod.mark_checkin("Stranger Danger")
    assert result["status"] == "not_found"
    assert result["descriptor"] == "Stranger Danger"


def test_snooze_reminder(tools_mod):
    _seed_person(tools_mod, "Sarah")
    result = tools_mod.snooze_reminder("Sarah", days=14)
    assert result["status"] == "ok"
    assert result["person"] == "Sarah"
    assert result["slug"] == "sarah"
    assert result["snoozed_for_days"] == 14

    with open(tools_mod.CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    assert "sarah" in config["snoozes"]
    assert "until" in config["snoozes"]["sarah"]


def test_snooze_default_7_days(tools_mod):
    _seed_person(tools_mod, "Tom")
    result = tools_mod.snooze_reminder("Tom")
    assert result["snoozed_for_days"] == 7


def test_mark_checkin_and_snooze_in_executors(tools_mod):
    assert "mark_checkin" in tools_mod.EXECUTORS
    assert "snooze_reminder" in tools_mod.EXECUTORS


# ── handle_nudge_action (Phase C button callbacks) ───────────────────


@pytest.fixture
def nudge_tools(tools_mod, tmp_path, monkeypatch):
    """Same as tools_mod, plus SNOOZES_PATH redirected to tmp_path."""
    monkeypatch.setattr(
        tools_mod, "SNOOZES_PATH",
        str(Path(tools_mod.WORKSPACE) / "snoozes.json"),
    )
    return tools_mod


def test_handle_nudge_action_done_writes_snoozes_file(nudge_tools):
    result = nudge_tools.handle_nudge_action(slug="alice-smith", action="done")
    assert result["status"] == "ok"
    assert result["slug"] == "alice-smith"
    assert result["action"] == "done"
    with open(nudge_tools.SNOOZES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert "alice-smith" in data
    assert data["alice-smith"]["status"] == "done"
    # Until is a YYYY-MM-DD string.
    import re as _re
    assert _re.match(r"^\d{4}-\d{2}-\d{2}$", data["alice-smith"]["until"])


def test_handle_nudge_action_snoozed_uses_30_day_window(nudge_tools):
    from datetime import date, timedelta
    result = nudge_tools.handle_nudge_action(slug="bob", action="snoozed")
    expected = (date.today() + timedelta(days=30)).isoformat()
    assert result["until"] == expected


def test_handle_nudge_action_ignored_uses_365_day_window(nudge_tools):
    from datetime import date, timedelta
    result = nudge_tools.handle_nudge_action(slug="carol", action="ignored")
    expected = (date.today() + timedelta(days=365)).isoformat()
    assert result["until"] == expected


def test_handle_nudge_action_unknown_action_is_error(nudge_tools):
    result = nudge_tools.handle_nudge_action(slug="x", action="yeet")
    assert result["status"] == "error"


def test_handle_nudge_action_empty_slug_is_error(nudge_tools):
    result = nudge_tools.handle_nudge_action(slug="", action="done")
    assert result["status"] == "error"


def test_handle_nudge_action_preserves_other_slugs(nudge_tools):
    """Writing a new entry must NOT wipe other slugs' snoozes."""
    nudge_tools.handle_nudge_action(slug="alice", action="snoozed")
    nudge_tools.handle_nudge_action(slug="bob", action="ignored")
    with open(nudge_tools.SNOOZES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert set(data.keys()) == {"alice", "bob"}


def test_handle_nudge_action_in_executors(nudge_tools):
    assert "handle_nudge_action" in nudge_tools.EXECUTORS


# ── handle_nudge_action — done bumps last_interaction ────────────────
# 2026-04-21: pressing ✅ done on the morning Relationship Check now
# also stamps the person file's last_interaction to today. Without
# this, people the operator already contacted keep resurfacing because
# gmessages-mine misses email/Slack/IRL signals. Root-cause fix is
# gmail-sent-mine (Fix 4B); this is the explicit-signal path.


@pytest.fixture
def nudge_with_brain(tmp_path, monkeypatch):
    """Sandboxes both SNOOZES_PATH and the Dropbox brain so done-button
    last_interaction stamping can be observed end-to-end."""
    brain_root = tmp_path / "brain-root"
    (brain_root / "people").mkdir(parents=True)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))

    workspace = tmp_path / "connector-workspace"
    workspace.mkdir()

    # Force a fresh tools import so CLAWFORD_BRAIN_DROPBOX_ROOT is
    # picked up by any module-scope brain root resolution.
    for mod in list(sys.modules):
        if mod in ("tools", "brain"):
            del sys.modules[mod]
    import tools
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "SNOOZES_PATH", str(workspace / "snoozes.json"))

    return tools, brain_root


def test_handle_nudge_action_done_bumps_last_interaction(nudge_with_brain):
    """✅ done → last_interaction stamped to today."""
    from datetime import date
    tools, brain_root = nudge_with_brain
    fp = brain_root / "people" / "ellen-example.md"
    fp.write_text(
        "# Ellen Example\n"
        "- **slug:** ellen-example\n"
        "- **circles:** friends-close\n"
        "- **email:** ellen@example.com\n"
        "- **last_interaction:** 2026-01-01\n",
        encoding="utf-8",
    )
    tools.handle_nudge_action(slug="ellen-example", action="done")
    text = fp.read_text(encoding="utf-8")
    today_iso = date.today().isoformat()
    assert f"- **last_interaction:** {today_iso}" in text


def test_handle_nudge_action_done_preserves_fresher_existing(nudge_with_brain):
    """Max-merge: if the person file already has a newer
    last_interaction (e.g., gmail-sent-mine stamped a fresher date
    earlier today), done MUST NOT rewind it."""
    from datetime import date, timedelta
    tools, brain_root = nudge_with_brain
    fresh = (date.today() + timedelta(days=7)).isoformat()  # pretend future stamp
    fp = brain_root / "people" / "fresh-example.md"
    fp.write_text(
        "# Fresh Example\n"
        "- **slug:** fresh-example\n"
        "- **circles:** friends-close\n"
        "- **email:** fresh@example.com\n"
        f"- **last_interaction:** {fresh}\n",
        encoding="utf-8",
    )
    tools.handle_nudge_action(slug="fresh-example", action="done")
    text = fp.read_text(encoding="utf-8")
    assert f"- **last_interaction:** {fresh}" in text, (
        "max-merge must leave a fresher existing date untouched"
    )


def test_handle_nudge_action_snoozed_does_not_bump_last_interaction(
    nudge_with_brain,
):
    """snoozed/ignored mean 'I don't want to see this for a while',
    NOT 'I contacted them'. last_interaction must stay put."""
    tools, brain_root = nudge_with_brain
    fp = brain_root / "people" / "snoozed-example.md"
    fp.write_text(
        "# Snoozed Example\n"
        "- **slug:** snoozed-example\n"
        "- **circles:** friends-close\n"
        "- **email:** snoozed@example.com\n"
        "- **last_interaction:** 2026-01-01\n",
        encoding="utf-8",
    )
    tools.handle_nudge_action(slug="snoozed-example", action="snoozed")
    text = fp.read_text(encoding="utf-8")
    assert "- **last_interaction:** 2026-01-01" in text


def test_handle_nudge_action_done_missing_person_file_still_returns_ok(
    nudge_with_brain,
):
    """If the slug doesn't have a person file (edge case: deleted
    mid-day, typo, test fixture), the snooze write still succeeds;
    don't raise."""
    tools, brain_root = nudge_with_brain
    result = tools.handle_nudge_action(slug="ghost", action="done")
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# get_person (delegates to brain.get_person)
# ---------------------------------------------------------------------------


@pytest.fixture
def brain_sandboxed_tools(tmp_path, monkeypatch):
    """Point both brain and tools.py at a sandboxed dropbox root."""
    brain_root = tmp_path / "brain-root"
    brain_root.mkdir()
    (brain_root / "people").mkdir()
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))

    for mod in list(sys.modules):
        if mod in ("tools", "brain"):
            del sys.modules[mod]
    import tools
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "PENDING_TRIAGE_PATH", str(workspace / "pending-triage.json"))
    return tools, brain_root


def test_get_person_found_by_slug(brain_sandboxed_tools):
    tools, brain_root = brain_sandboxed_tools
    (brain_root / "people" / "priya-rivera.md").write_text(
        "# Priya Rivera\n\n"
        "- **slug:** priya-rivera\n"
        "- **circles:** family-inner\n"
        "- **tone:** warm\n"
        "- **last_interaction:** 2026-02-11\n",
        encoding="utf-8",
    )
    result = tools.get_person("priya-rivera")
    assert result["status"] == "found"
    assert result["slug"] == "priya-rivera"
    assert result["fields"]["tone"] == "warm"


def test_get_person_found_by_display_name(brain_sandboxed_tools):
    tools, brain_root = brain_sandboxed_tools
    (brain_root / "people" / "priya-rivera.md").write_text(
        "# Priya Rivera\n\n- **slug:** priya-rivera\n- **circles:** family-inner\n",
        encoding="utf-8",
    )
    result = tools.get_person("Priya Rivera")
    assert result["status"] == "found"


def test_get_person_not_found(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    result = tools.get_person("does-not-exist")
    assert result["status"] == "not_found"


def test_get_person_in_executors(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    assert "get_person" in tools.EXECUTORS


# ---------------------------------------------------------------------------
# get_commitments (invokes commitment-scan.py via run_json_script)
# ---------------------------------------------------------------------------


def test_get_commitments_shells_out_to_script(brain_sandboxed_tools, monkeypatch):
    tools, _ = brain_sandboxed_tools
    called = {}

    def fake_run(script_path, *args, **kwargs):
        called["script"] = script_path
        called["args"] = list(args)
        return {
            "status": "ok",
            "commitments": [{"id": "c-1", "who": "Alice"}],
            "summary": {"total": 1, "open": 1, "overdue": 0, "approaching": 0},
        }

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error", lambda r: False)

    result = tools.get_commitments()
    assert result["status"] == "ok"
    assert len(result["commitments"]) == 1
    assert called["script"].endswith("commitment-scan.py")


def test_get_commitments_surfaces_script_error(brain_sandboxed_tools, monkeypatch):
    tools, _ = brain_sandboxed_tools
    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(
        subprocess_helpers, "run_json_script",
        lambda *a, **k: {"__error__": "timed out"},
    )
    monkeypatch.setattr(
        subprocess_helpers, "is_subprocess_error",
        lambda r: "__error__" in r,
    )
    result = tools.get_commitments()
    assert result["status"] == "error"
    assert "timed out" in result["error"]


def test_get_commitments_in_executors(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    assert "get_commitments" in tools.EXECUTORS


# ---------------------------------------------------------------------------
# dismiss_triage_n (removes item N from pending-triage.json)
# ---------------------------------------------------------------------------


def test_dismiss_triage_n_removes_nth_item(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    Path(tools.PENDING_TRIAGE_PATH).write_text(json.dumps({
        "items": [
            {"id": "t-1", "content": "first"},
            {"id": "t-2", "content": "second"},
            {"id": "t-3", "content": "third"},
        ],
    }), encoding="utf-8")
    result = tools.dismiss_triage_n(2)
    assert result["status"] == "ok"
    assert result["dismissed"]["id"] == "t-2"

    with open(tools.PENDING_TRIAGE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    remaining = [i["id"] for i in data["items"]]
    assert remaining == ["t-1", "t-3"]


def test_dismiss_triage_n_out_of_range_is_error(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    Path(tools.PENDING_TRIAGE_PATH).write_text(json.dumps({
        "items": [{"id": "t-1"}],
    }), encoding="utf-8")
    result = tools.dismiss_triage_n(5)
    assert result["status"] == "error"


def test_dismiss_triage_n_empty_file_is_error(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    result = tools.dismiss_triage_n(1)
    assert result["status"] == "error"


def test_dismiss_triage_n_in_executors(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    assert "dismiss_triage_n" in tools.EXECUTORS


# ---------------------------------------------------------------------------
# /note flow — propose_add_note / confirm_add_note
# ---------------------------------------------------------------------------


def test_propose_add_note_stages_pending_action(brain_sandboxed_tools, monkeypatch, tmp_path):
    tools, _ = brain_sandboxed_tools
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    result = tools.propose_add_note("call mom next week")
    assert "__pending_action__" in result
    assert "note" in result["summary"].lower()


def test_confirm_add_note_writes_to_inbox(brain_sandboxed_tools):
    tools, brain_root = brain_sandboxed_tools
    result = tools.confirm_add_note("call mom next week")
    assert result["status"] == "ok"
    inbox = (brain_root / "notes" / "inbox.md").read_text(encoding="utf-8")
    assert "call mom next week" in inbox
    assert "- **agent:** connector" in inbox
    assert "- **triaged:** false" in inbox


def test_confirm_add_note_honors_triaged_flag(brain_sandboxed_tools):
    tools, brain_root = brain_sandboxed_tools
    tools.confirm_add_note("already triaged", triaged=True)
    inbox = (brain_root / "notes" / "inbox.md").read_text(encoding="utf-8")
    assert "- **triaged:** true" in inbox


def test_add_note_executors_wired(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    assert "propose_add_note" in tools.EXECUTORS
    assert "confirm_add_note" in tools.EXECUTORS


# ---------------------------------------------------------------------------
# /add flow — propose_add_person / confirm_add_person
# ---------------------------------------------------------------------------


def test_propose_add_person_stages_pending_action(brain_sandboxed_tools, monkeypatch, tmp_path):
    tools, _ = brain_sandboxed_tools
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    result = tools.propose_add_person("Sarah Example", "friends")
    assert "__pending_action__" in result
    assert "Sarah" in result["summary"]


def test_confirm_add_person_creates_file(brain_sandboxed_tools):
    tools, brain_root = brain_sandboxed_tools
    result = tools.confirm_add_person("Sarah Example", "friends", tone="warm")
    assert result["status"] == "ok"
    path = brain_root / "people" / "sarah-example.md"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "# Sarah Example" in content
    assert "- **circles:** friends" in content
    assert "- **tone:** warm" in content


def test_confirm_add_person_duplicate_is_error(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    tools.confirm_add_person("Sarah Example", "friends")
    result = tools.confirm_add_person("Sarah Example", "friends")
    assert result["status"] == "error"


def test_add_person_executors_wired(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    assert "propose_add_person" in tools.EXECUTORS
    assert "confirm_add_person" in tools.EXECUTORS


# ---------------------------------------------------------------------------
# force_nudge / force_triage (on-demand scripts)
# ---------------------------------------------------------------------------


def test_force_nudge_invokes_people_scan(brain_sandboxed_tools, monkeypatch):
    tools, _ = brain_sandboxed_tools
    captured = {}

    def fake_run(script_path, *args, **kwargs):
        captured["script"] = script_path
        return {
            "overdue": [{"slug": "priya-rivera", "days_since": 67}],
            "approaching": [],
            "healthy": [],
            "summary": {"total": 1, "overdue": 1, "approaching": 0, "skipped": 0},
        }

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error", lambda r: False)

    result = tools.force_nudge()
    assert result["status"] == "ok"
    assert captured["script"].endswith("people-scan.py")
    assert len(result["overdue"]) == 1


def test_force_nudge_surfaces_script_error(brain_sandboxed_tools, monkeypatch):
    tools, _ = brain_sandboxed_tools
    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(
        subprocess_helpers, "run_json_script",
        lambda *a, **k: {"__error__": "boom"},
    )
    monkeypatch.setattr(
        subprocess_helpers, "is_subprocess_error",
        lambda r: "__error__" in r,
    )
    result = tools.force_nudge()
    assert result["status"] == "error"


def test_force_triage_invokes_notes_triage_script(brain_sandboxed_tools, monkeypatch):
    tools, _ = brain_sandboxed_tools
    captured = {}

    def fake_run(script_path, *args, **kwargs):
        captured["script"] = script_path
        return {"untriaged": [], "count": 0, "already_triaged": 0}

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error", lambda r: False)

    result = tools.force_triage()
    assert result["status"] == "ok"
    assert captured["script"].endswith("notes-triage.py")


def test_force_script_tools_in_executors(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    assert "force_nudge" in tools.EXECUTORS
    assert "force_triage" in tools.EXECUTORS


# ---------------------------------------------------------------------------
# draft_reply (/draft [name])
# ---------------------------------------------------------------------------


def test_draft_reply_invokes_draft_compose_script(brain_sandboxed_tools, monkeypatch):
    tools, _ = brain_sandboxed_tools
    captured = {}

    def fake_run(script_path, *args, **kwargs):
        captured["script"] = script_path
        captured["args"] = list(args)
        return {
            "status": "ok",
            "recipient": {"slug": "priya-rivera", "name": "Priya Rivera"},
            "draft": "Hi Mom,\n\nJust wanted to check in...\n\nLove,\nBrian",
            "voice": {"register": "informal_personal"},
        }

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error", lambda r: False)

    result = tools.draft_reply("Priya Rivera")
    assert result["status"] == "ok"
    assert "Mom" in result["draft"]
    assert captured["script"].endswith("draft-compose.py")
    assert "--recipient" in captured["args"]


def test_draft_reply_passes_inbound_text_when_supplied(brain_sandboxed_tools, monkeypatch):
    tools, _ = brain_sandboxed_tools
    captured = {}

    def fake_run(script_path, *args, **kwargs):
        captured["args"] = list(args)
        return {"status": "ok", "draft": "..."}

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error", lambda r: False)

    tools.draft_reply("Priya", inbound_text="When can you come over for dinner?")
    assert "--inbound-text" in captured["args"]
    idx = captured["args"].index("--inbound-text")
    assert captured["args"][idx + 1] == "When can you come over for dinner?"


def test_draft_reply_surfaces_script_error(brain_sandboxed_tools, monkeypatch):
    tools, _ = brain_sandboxed_tools
    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(
        subprocess_helpers, "run_json_script",
        lambda *a, **k: {"__error__": "llm timeout"},
    )
    monkeypatch.setattr(
        subprocess_helpers, "is_subprocess_error",
        lambda r: "__error__" in r,
    )
    result = tools.draft_reply("Priya")
    assert result["status"] == "error"
    assert "llm timeout" in result["error"]


def test_draft_reply_in_executors(brain_sandboxed_tools):
    tools, _ = brain_sandboxed_tools
    assert "draft_reply" in tools.EXECUTORS
