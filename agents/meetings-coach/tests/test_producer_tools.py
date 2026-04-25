"""Tests for Sergeant Murphy's Phase C producer tools."""
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
    workspace = tmp_path / "meetings-coach-workspace"
    workspace.mkdir()
    cache = workspace / "cache"
    cache.mkdir()
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    # Other agents' test files also insert AGENT_DIR at sys.path[0]
    # during collection; whichever runs last wins. Re-assert ours
    # before the bare `import tools` below.
    monkeypatch.syspath_prepend(str(AGENT_DIR))
    for mod in list(sys.modules):
        if mod in ("tools",):
            del sys.modules[mod]
    import tools
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "CACHE", str(cache))
    return tools


def _write_debrief(cache_dir, event_id, action_items):
    data = {
        "event_id": event_id,
        "meeting_title": f"Meeting {event_id}",
        "meeting_start": "2026-04-15T10:00:00-07:00",
        "krisp_action_items": action_items,
        "status": "pending_review",
    }
    path = cache_dir / f"pending-debrief-{event_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_list_pending_action_items_empty(tools_mod):
    result = tools_mod.list_pending_action_items()
    assert result["count"] == 0
    assert result["items"] == []


def test_list_pending_action_items(tools_mod, tmp_path):
    cache = Path(tools_mod.CACHE)
    _write_debrief(cache, "ev1", ["Follow up with Yesol", "Draft roadmap"])
    _write_debrief(cache, "ev2", ["Send meeting notes"])

    result = tools_mod.list_pending_action_items()
    assert result["count"] == 3
    ids = [i["item_id"] for i in result["items"]]
    assert "ev1:0" in ids
    assert "ev1:1" in ids
    assert "ev2:0" in ids


def test_confirm_action_item(tools_mod, tmp_path):
    cache = Path(tools_mod.CACHE)
    _write_debrief(cache, "ev1", ["Follow up with Yesol", "Draft roadmap"])

    result = tools_mod.confirm_action_item("ev1:0")
    assert result["status"] == "ok"
    assert result["action_item"] == "Follow up with Yesol"

    # Verify persisted
    with open(cache / "pending-debrief-ev1.json", encoding="utf-8") as f:
        data = json.load(f)
    assert 0 in data["accepted_items"]


def test_confirm_action_item_idempotent(tools_mod, tmp_path):
    cache = Path(tools_mod.CACHE)
    _write_debrief(cache, "ev1", ["Task"])

    tools_mod.confirm_action_item("ev1:0")
    tools_mod.confirm_action_item("ev1:0")

    with open(cache / "pending-debrief-ev1.json", encoding="utf-8") as f:
        data = json.load(f)
    assert data["accepted_items"].count(0) == 1


def test_dismiss_action_item(tools_mod, tmp_path):
    cache = Path(tools_mod.CACHE)
    _write_debrief(cache, "ev1", ["Not relevant"])

    result = tools_mod.dismiss_action_item("ev1:0")
    assert result["status"] == "ok"
    assert result["dismissed"] is True

    with open(cache / "pending-debrief-ev1.json", encoding="utf-8") as f:
        data = json.load(f)
    assert 0 in data["dismissed_items"]


def test_confirm_descriptor_matches_nothing_returns_not_found(tools_mod):
    """Fuzzy descriptor that hits neither item_id passthrough nor any
    substring match returns status=not_found (operator-friendly;
    distinguishes from a malformed item_id)."""
    result = tools_mod.confirm_action_item("bad_id")
    assert result["status"] == "not_found"


def test_confirm_missing_debrief(tools_mod):
    """An item_id-shaped descriptor that passes through the pattern
    but doesn't correspond to any real debrief file surfaces as an
    error at the file-access stage."""
    result = tools_mod.confirm_action_item("nonexistent:0")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# force_prep / force_debrief (on-demand script invocation)
# ---------------------------------------------------------------------------


def test_force_prep_shells_out_with_resolved_meeting_id(tools_mod, monkeypatch):
    """Passing a full-length event_id-shaped descriptor (>= 10
    alphanum/underscore chars) triggers id_passthrough in the fuzzy
    resolver, so force_prep shells out with that id verbatim."""
    captured = {}

    def fake_run(script_path, *args, **kwargs):
        captured["script"] = script_path
        captured["args"] = list(args)
        return {"status": "ok", "meeting_id": "abc123def4567890", "prep": "..."}

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error", lambda r: False)

    result = tools_mod.force_prep("abc123def4567890")
    assert result["status"] == "ok"
    assert captured["script"].endswith("meeting-prep.py")
    assert "--meeting-id" in captured["args"]
    idx = captured["args"].index("--meeting-id")
    assert captured["args"][idx + 1] == "abc123def4567890"


def test_force_prep_surfaces_error(tools_mod, monkeypatch):
    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(
        subprocess_helpers, "run_json_script",
        lambda *a, **k: {"__error__": "not found"},
    )
    monkeypatch.setattr(
        subprocess_helpers, "is_subprocess_error", lambda r: "__error__" in r,
    )
    # Use a descriptor that passes through as an id but points at a
    # non-existent meeting — subprocess wrapper returns the error.
    result = tools_mod.force_prep("missing0000000000")
    assert result["status"] == "error"


def test_force_debrief_shells_out(tools_mod, monkeypatch):
    captured = {}

    def fake_run(script_path, *args, **kwargs):
        captured["script"] = script_path
        return {"status": "ok", "transcripts_processed": 1, "debriefs_sent": 1}

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error", lambda r: False)

    result = tools_mod.force_debrief()
    assert result["status"] == "ok"
    assert captured["script"].endswith("post-meeting-scan.py")


def test_force_tools_in_executors(tools_mod):
    assert "force_prep" in tools_mod.EXECUTORS
    assert "force_debrief" in tools_mod.EXECUTORS


# ---------------------------------------------------------------------------
# /coaching on / off — propose_coaching_toggle / confirm_coaching_toggle
# ---------------------------------------------------------------------------


def _seed_config(tools_mod, coaching_block=None):
    cfg = {"calendars": []}
    if coaching_block is not None:
        cfg["coaching"] = coaching_block
    with open(tools_mod.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def test_propose_coaching_toggle_stages_pending_action(tools_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    _seed_config(tools_mod, {"enabled": True})
    result = tools_mod.propose_coaching_toggle(False)
    assert "__pending_action__" in result
    assert "off" in result["summary"].lower() or "disable" in result["summary"].lower()


def test_confirm_coaching_toggle_flips_flag(tools_mod):
    _seed_config(tools_mod, {"enabled": True, "growth_areas": []})
    result = tools_mod.confirm_coaching_toggle(False)
    assert result["status"] == "ok"
    with open(tools_mod.CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["coaching"]["enabled"] is False


def test_confirm_coaching_toggle_creates_coaching_block_if_absent(tools_mod):
    _seed_config(tools_mod, None)
    result = tools_mod.confirm_coaching_toggle(True)
    assert result["status"] == "ok"
    with open(tools_mod.CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["coaching"]["enabled"] is True


def test_coaching_toggle_executors_wired(tools_mod):
    assert "propose_coaching_toggle" in tools_mod.EXECUTORS
    assert "confirm_coaching_toggle" in tools_mod.EXECUTORS


# ---------------------------------------------------------------------------
# /coaching add {id} {description} — propose + confirm
# ---------------------------------------------------------------------------


def test_confirm_coaching_area_add_appends_to_growth_areas(tools_mod):
    _seed_config(tools_mod, {"enabled": True, "growth_areas": [
        {"id": "clarity", "label": "Clarity"},
    ]})
    result = tools_mod.confirm_coaching_area_add("brevity", "Keep intros under 60s")
    assert result["status"] == "ok"
    with open(tools_mod.CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    ids = [a["id"] for a in cfg["coaching"]["growth_areas"]]
    assert "brevity" in ids
    brevity = next(a for a in cfg["coaching"]["growth_areas"] if a["id"] == "brevity")
    assert brevity["label"] == "Keep intros under 60s"


def test_confirm_coaching_area_add_rejects_duplicate_id(tools_mod):
    _seed_config(tools_mod, {"enabled": True, "growth_areas": [
        {"id": "clarity", "label": "Clarity"},
    ]})
    result = tools_mod.confirm_coaching_area_add("clarity", "Different label")
    assert result["status"] == "error"


def test_confirm_coaching_area_remove_drops_by_id(tools_mod):
    _seed_config(tools_mod, {"enabled": True, "growth_areas": [
        {"id": "clarity", "label": "Clarity"},
        {"id": "brevity", "label": "Be concise"},
    ]})
    result = tools_mod.confirm_coaching_area_remove("clarity")
    assert result["status"] == "ok"
    with open(tools_mod.CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    ids = [a["id"] for a in cfg["coaching"]["growth_areas"]]
    assert ids == ["brevity"]


def test_confirm_coaching_area_remove_missing_id_is_error(tools_mod):
    _seed_config(tools_mod, {"enabled": True, "growth_areas": [
        {"id": "clarity", "label": "Clarity"},
    ]})
    result = tools_mod.confirm_coaching_area_remove("nonexistent")
    assert result["status"] == "error"


def test_propose_coaching_area_add_stages_pending_action(tools_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    _seed_config(tools_mod, {"enabled": True, "growth_areas": []})
    result = tools_mod.propose_coaching_area_add("brevity", "Keep intros under 60s")
    assert "__pending_action__" in result


def test_propose_coaching_area_remove_stages_pending_action(tools_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    _seed_config(tools_mod, {"enabled": True, "growth_areas": [{"id": "clarity", "label": "Clarity"}]})
    result = tools_mod.propose_coaching_area_remove("clarity")
    assert "__pending_action__" in result


def test_coaching_area_executors_wired(tools_mod):
    for name in (
        "propose_coaching_area_add", "confirm_coaching_area_add",
        "propose_coaching_area_remove", "confirm_coaching_area_remove",
    ):
        assert name in tools_mod.EXECUTORS, f"{name} missing from EXECUTORS"
