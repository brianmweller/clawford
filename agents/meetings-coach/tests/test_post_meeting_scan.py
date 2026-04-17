"""Tests for agents/meetings-coach/scripts/post-meeting-scan.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`meetings-coach:post-meeting-scan`. THIS CRON IS DELICATE — the
2026-04-14 Alexis Lloyd incident re-sent the same debrief 5x across
2.5 hours. Preserve the fix invariants:

1. Delivery is gated ONLY on `transcript-scan.py` output's `processed`
   list — NEVER iterate `cache/pending-debrief-*.json` as a queue.
2. Coaching appends only once per event — check `coaching-history.json`
   before running metrics and sending.
3. Cleanup pass is separate from delivery: walks pending files, greps
   `commitments/active.md` for their event_id, deletes matched ones.
   That step never sends anything.
4. Krisp 401 4-step rate-limited alert: stamp `cache/krisp-last-401.json`,
   check `cache/krisp-last-alert.json` mtime, send Telegram only when
   >= 90 min since last alert, stamp `cache/krisp-last-alert.json`.

See agents/shared/tests/test_post_meeting_scan_idempotency.py — that
file asserted the invariants on the LLM cron prompt. This file asserts
them on the Python orchestrator.
"""
from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "post-meeting-scan.py"
FIXTURES = Path(__file__).parent / "fixtures" / "post-meeting-scan"


def _load():
    spec = importlib.util.spec_from_file_location("post_meeting_scan", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def scan_one_new():
    with open(FIXTURES / "transcript-scan-one-new.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def scan_empty():
    with open(FIXTURES / "transcript-scan-empty.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def scan_401():
    with open(FIXTURES / "transcript-scan-401.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def pending_alexis():
    with open(FIXTURES / "pending-debrief-evt-alexis-420.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def metrics_alexis():
    with open(FIXTURES / "transcript-metrics-alexis.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def meeting_config():
    with open(FIXTURES / "meeting-config.json", encoding="utf-8") as f:
        return json.load(f)


def _fake_infer(json_payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        ok=True,
        text=json.dumps(json_payload),
        error=None,
        input_tokens=0,
        output_tokens=0,
        model="fake",
    )


def _coaching_llm_reply() -> dict:
    """Shape the orchestrator's LLM prompt should return."""
    return {
        "concision": {
            "assessment": "Long stretches, could tighten.",
            "quote": "So walking through where Q2 landed...",
            "timestamp": "0:12:35",
            "try": "Q2 slipped two weeks; bandwidth is the bottleneck.",
        },
        "structured_thinking": {
            "assessment": "Jumped between scope and timeline.",
            "quote": "And then on the bandwidth side...",
            "timestamp": "0:14:02",
            "try": "Lead with timeline, then causes, then mitigations.",
        },
        "empathy": {
            "assessment": "Asked 3 questions — decent.",
            "quote": "What do you need from me to unblock the hiring side?",
            "timestamp": "0:22:10",
            "try": None,
        },
    }


# ─── format_debrief ──────────────────────────────────────────────────


def test_format_debrief_includes_title_and_sections(mod, pending_alexis):
    msg = mod.format_debrief(pending_alexis)
    assert "Alexis 1:1" in msg
    assert "ACTION ITEMS" in msg
    assert "DECISIONS" in msg or "KEY POINTS" in msg
    assert "Q2 roadmap draft" in msg
    assert "hiring pipeline" in msg


def test_format_debrief_drops_text_command_footer(mod, pending_alexis):
    """The text-command footer ('/confirm to save...') is replaced by
    an inline keyboard (build_debrief_keyboard). Leaving the footer in
    place would be confusing now that buttons are the interaction
    surface, and the '/dismiss N' phrasing misleads on single-item
    debriefs where N makes no sense."""
    msg = mod.format_debrief(pending_alexis)
    assert "/confirm" not in msg
    assert "/dismiss" not in msg
    assert "item N" not in msg


# ─── build_debrief_keyboard ──────────────────────────────────────────


def test_build_debrief_keyboard_has_three_buttons(mod):
    """One button set per debrief: Save / Dismiss / Modify. Each carries
    the event_id so the dispatcher can resolve the pending file."""
    pending = {"event_id": "evt-abc-123", "meeting_title": "X"}
    markup = mod.build_debrief_keyboard(pending)
    assert isinstance(markup, dict)
    rows = markup.get("inline_keyboard")
    assert rows and len(rows) == 1
    buttons = rows[0]
    assert len(buttons) == 3
    texts = [b["text"] for b in buttons]
    assert any("Save" in t for t in texts)
    assert any("Dismiss" in t for t in texts)
    assert any("Modify" in t for t in texts)
    callback_data = [b["callback_data"] for b in buttons]
    assert "debrief_save:evt-abc-123" in callback_data
    assert "debrief_dismiss:evt-abc-123" in callback_data
    assert "debrief_modify:evt-abc-123" in callback_data


def test_build_debrief_keyboard_returns_none_without_event_id(mod):
    """Without an event_id the callbacks can't resolve anything, so
    no buttons are worth showing."""
    assert mod.build_debrief_keyboard({"meeting_title": "X"}) is None
    assert mod.build_debrief_keyboard({"event_id": ""}) is None


# ─── save_debrief_to_brain ───────────────────────────────────────────


def _stage_pending(cache_dir, event_id, items, **extra):
    """Write a pending-debrief-{event_id}.json with the given action
    items and optional extra fields."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "event_id": event_id,
        "meeting_title": extra.get("meeting_title", "Test Meeting"),
        "meeting_start": extra.get(
            "meeting_start", "2026-04-16T12:00:00-07:00"
        ),
        "krisp_action_items": items,
        "krisp_key_points": extra.get("krisp_key_points", []),
        "krisp_speakers": extra.get(
            "krisp_speakers", ["Sam Smith", "Steve Shadman"]
        ),
        "status": "pending_review",
    }
    for k, v in extra.items():
        if k not in data:
            data[k] = v
    (cache_dir / f"pending-debrief-{event_id}.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    return data


def test_save_debrief_appends_to_active_md_with_schema(mod, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text("# Commitments — Active\n\n---\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(
        cache,
        "evt-save-1",
        [
            {
                "title": "{{Speaker_2}} to talk to recruiting about the operator's pipeline.",
                "assignee": None,
            }
        ],
    )

    result = mod.save_debrief_to_brain("evt-save-1")
    assert result["status"] == "ok"
    assert result["written"] == 1

    content = active.read_text(encoding="utf-8")
    # Schema fields must all land in the entry.
    assert "## meetings-coach-" in content
    assert "- who: Steve" in content
    assert "- to_whom: the operator" in content
    assert "- what: " in content
    assert "recruiting" in content
    assert "- status: open" in content
    assert "event_id: evt-save-1" in content
    assert "- source_agent: meetings-coach" in content
    assert "- created_at: " in content


def test_save_debrief_is_idempotent_on_duplicate_event_id(mod, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text(
        "# Commitments — Active\n\n## meetings-coach-2026-04-16-001\n"
        "- who: Steve\n- to_whom: the operator\n- what: previously saved.\n"
        "- status: open\n- source_detail: x (event_id: evt-dup, meeting_start: y)\n"
        "- source_agent: meetings-coach\n- created_at: 2026-04-16T00:00:00Z\n\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(
        cache, "evt-dup",
        [{"title": "Steve to do something new.", "assignee": None}],
    )
    before = active.read_text(encoding="utf-8")
    result = mod.save_debrief_to_brain("evt-dup")
    assert result["status"] == "ok"
    assert result.get("already_saved") is True
    assert result["written"] == 0
    assert active.read_text(encoding="utf-8") == before


def test_save_debrief_skips_dismissed_indices(mod, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text("# Commitments — Active\n\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(
        cache,
        "evt-skip",
        [
            {"title": "Steve to do thing A.", "assignee": None},
            {"title": "Steve to do thing B.", "assignee": None},
            {"title": "Steve to do thing C.", "assignee": None},
        ],
        dismissed_items=[1],  # skip index 1 (thing B)
    )
    result = mod.save_debrief_to_brain("evt-skip")
    assert result["status"] == "ok"
    assert result["written"] == 2
    content = active.read_text(encoding="utf-8")
    assert "thing A" in content
    assert "thing B" not in content
    assert "thing C" in content


def test_save_debrief_deletes_pending_file_on_success(
    mod, tmp_path, monkeypatch
):
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text("# Commitments — Active\n\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(
        cache, "evt-del",
        [{"title": "Steve to do it.", "assignee": None}],
    )
    pending_path = cache / "pending-debrief-evt-del.json"
    assert pending_path.exists()

    result = mod.save_debrief_to_brain("evt-del")
    assert result["status"] == "ok"
    assert not pending_path.exists()


def test_save_debrief_with_no_items_is_graceful(mod, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text("# Commitments — Active\n\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(cache, "evt-empty", [])
    result = mod.save_debrief_to_brain("evt-empty")
    assert result["status"] == "ok"
    assert result["written"] == 0
    # Pending file should still be deleted on save with no items —
    # pressing Save communicates "I'm done with this debrief".
    assert not (cache / "pending-debrief-evt-empty.json").exists()


def test_save_debrief_missing_pending_returns_error(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", tmp_path / "active.md")
    result = mod.save_debrief_to_brain("evt-missing")
    assert result["status"] == "error"


# ─── dismiss_debrief ─────────────────────────────────────────────────


def test_dismiss_debrief_deletes_pending_file(mod, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text("# Commitments — Active\n\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(
        cache, "evt-dismiss",
        [{"title": "Steve to do it.", "assignee": None}],
    )
    pending_path = cache / "pending-debrief-evt-dismiss.json"

    result = mod.dismiss_debrief("evt-dismiss")
    assert result["status"] == "ok"
    assert not pending_path.exists()
    # Dismiss must not write to active.md.
    assert "evt-dismiss" not in active.read_text(encoding="utf-8")


# ─── replace_action_items ────────────────────────────────────────────


def test_replace_action_items_writes_given_items(mod, tmp_path, monkeypatch):
    """After the Modify button, the operator types his corrected list. The
    LLM calls replace_action_items(event_id, items=[...]) with one
    string per item; they should be saved to active.md one-for-one
    and the pending file deleted."""
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text("# Commitments — Active\n\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(
        cache, "evt-modify",
        [{"title": "stale original item", "assignee": None}],
        meeting_title="Meet & Greet",
    )

    result = mod.replace_action_items(
        "evt-modify",
        [
            "Steve: Talk to recruiting about the operator's pipeline.",
            "the operator: Send Steve the role description by Friday.",
        ],
    )
    assert result["status"] == "ok"
    assert result["written"] == 2
    content = active.read_text(encoding="utf-8")
    assert "- who: Steve" in content
    assert "Talk to recruiting about the operator's pipeline." in content
    assert "- who: the operator" in content
    assert "Send Steve the role description by Friday." in content
    # Pending file gone.
    assert not (cache / "pending-debrief-evt-modify.json").exists()
    # Stale original item should NOT be in active.md.
    assert "stale original item" not in content


def test_replace_action_items_accepts_plain_strings(mod, tmp_path, monkeypatch):
    """Items without a 'Who: what' colon should still be saved; owner
    falls back to the first speaker / 'the operator' so the invariant that
    every committed item has a `who` holds."""
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text("# Commitments — Active\n\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(
        cache, "evt-strs",
        [],
        krisp_speakers=["Sam Smith", "Steve Shadman"],
    )
    result = mod.replace_action_items(
        "evt-strs",
        ["Draft the Q3 OKRs deck.", "Steve to review the offer template."],
    )
    assert result["status"] == "ok"
    assert result["written"] == 2
    content = active.read_text(encoding="utf-8")
    assert "Draft the Q3 OKRs deck." in content
    assert "review the offer template." in content
    # Second item extracts owner from prose.
    assert "- who: Steve" in content


def test_replace_action_items_with_empty_list_clears(mod, tmp_path, monkeypatch):
    """An empty list after Modify means 'no action items worth tracking'.
    Don't write anything to active.md, just clear the pending file."""
    cache = tmp_path / "cache"
    active = tmp_path / "active.md"
    active.write_text("# Commitments — Active\n\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    _stage_pending(cache, "evt-clear", [{"title": "old", "assignee": None}])
    result = mod.replace_action_items("evt-clear", [])
    assert result["status"] == "ok"
    assert result["written"] == 0
    assert not (cache / "pending-debrief-evt-clear.json").exists()
    assert "evt-clear" not in active.read_text(encoding="utf-8")


def test_replace_action_items_missing_pending_returns_error(
    mod, tmp_path, monkeypatch,
):
    monkeypatch.setattr(mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", tmp_path / "active.md")
    result = mod.replace_action_items("evt-missing", ["the operator: do it."])
    assert result["status"] == "error"


def test_format_debrief_handles_krisp_native_action_item_shape(mod):
    """Krisp MCP returns action_items as {title, assignee, completed},
    not {who, what, by_when}. The renderer must extract content from
    Krisp's native keys AND surface an owner, even when the owner is
    embedded in the prose. Regression: 2026-04-16 Meet & Greet
    the operator/Steve debrief delivered as '1. ' with no content."""
    pending = {
        "event_id": "e1",
        "meeting_title": "Meet & Greet: Sam Smith | Steve Shadman",
        "krisp_action_items": [
            {
                "title": "Steve to talk to the recruiting team and schedule more calls with the operator.",
                "completed": False,
                "assignee": None,
            }
        ],
        "krisp_key_points": [],
        "participants": ["Sam Smith", "Steve Shadman"],
        "krisp_speakers": ["Sam Smith", "Steve Shadman"],
    }
    msg = mod.format_debrief(pending)
    assert "ACTION ITEMS" in msg
    assert "recruiting team" in msg
    # Every rendered item must carry an owner prefix.
    for line in msg.splitlines():
        if line.startswith("1. "):
            assert ":" in line, f"no owner prefix: {line!r}"
            assert line.split(":", 1)[0].replace("1. ", "").strip(), \
                f"empty owner in: {line!r}"


def test_format_debrief_resolves_speaker_placeholder_via_krisp_speakers(mod):
    """Krisp emits {{Speaker_N}} placeholders in action item titles;
    the resolver must replace them with krisp_speakers[N-1]. The
    placeholder must NOT leak through to the rendered message."""
    pending = {
        "event_id": "e2",
        "meeting_title": "Meet & Greet",
        "krisp_action_items": [
            {
                "title": "{{Speaker_2}} to talk to the recruiting team and schedule more calls with the operator.",
                "completed": False,
                "assignee": None,
            }
        ],
        "krisp_key_points": [],
        "krisp_speakers": ["Sam Smith", "Steve Shadman"],
    }
    msg = mod.format_debrief(pending)
    assert "{{Speaker_2}}" not in msg
    assert "Speaker_2" not in msg
    assert "recruiting team" in msg
    # Resolved owner should be Steve (first name from speakers[1]).
    assert "Steve" in msg


def test_format_debrief_extracts_owner_from_prose_when_assignee_null(mod):
    """When assignee is null but the title starts with 'NAME to/will/should...'
    pattern, extract NAME as the owner and strip the verb-phrase from
    the task so we render '1. NAME: <capitalized task>'."""
    pending = {
        "event_id": "e3",
        "meeting_title": "1:1 with Alexis",
        "krisp_action_items": [
            {
                "title": "Alexis will share hiring pipeline data by Friday.",
                "completed": False,
                "assignee": None,
            }
        ],
        "krisp_key_points": [],
    }
    msg = mod.format_debrief(pending)
    assert "1. Alexis: " in msg
    assert "hiring pipeline" in msg


def test_format_debrief_every_item_has_owner(mod):
    """Hard invariant — every rendered action item carries a who: prefix.
    Even when owner resolution fails, we fall back to 'Unassigned' rather
    than rendering a bare task."""
    pending = {
        "event_id": "e4",
        "meeting_title": "Standup",
        "krisp_action_items": [
            {"title": "Review the new onboarding deck.", "assignee": None},
            {"title": "{{Speaker_1}} to merge the PR.", "assignee": None},
        ],
        "krisp_speakers": ["Sam Smith"],
        "krisp_key_points": [],
    }
    msg = mod.format_debrief(pending)
    for line in msg.splitlines():
        if line.startswith(("1. ", "2. ")):
            prefix = line.split(":", 1)[0]
            owner = prefix.split(". ", 1)[-1].strip()
            assert owner, f"no owner before colon in: {line!r}"


def test_format_debrief_skips_empty_action_items(mod):
    """If Krisp emits an empty-placeholder item (all fields null/empty),
    skip it rather than rendering a bare '1. '. If ALL items are empty,
    suppress the ACTION ITEMS header entirely."""
    pending = {
        "event_id": "e3",
        "meeting_title": "Short Sync",
        "krisp_action_items": [
            {"title": "", "assignee": None, "completed": False},
            {},
        ],
        "krisp_key_points": [],
    }
    msg = mod.format_debrief(pending)
    assert "ACTION ITEMS" not in msg
    assert "\n1. \n" not in msg
    assert "\n1. " not in msg.rstrip() + "\n"


def test_format_debrief_uses_assignee_when_present(mod):
    """When Krisp resolves the assignee (non-null), surface it as the
    'who' prefix on the rendered line."""
    pending = {
        "event_id": "e4",
        "meeting_title": "1:1",
        "krisp_action_items": [
            {
                "title": "Send updated Q2 roadmap draft",
                "assignee": "the operator",
                "completed": False,
            }
        ],
        "krisp_key_points": [],
    }
    msg = mod.format_debrief(pending)
    assert "the operator: Send updated Q2 roadmap draft" in msg


def test_format_debrief_assignee_beats_prose_extraction(mod):
    """Explicit assignee should win over prose-based extraction so we
    don't mis-assign when Krisp resolves the owner but the title still
    begins with a speaker name (e.g., paraphrase)."""
    pending = {
        "event_id": "e5",
        "meeting_title": "Planning",
        "krisp_action_items": [
            {
                "title": "Alexis to draft the Q3 OKRs.",
                "assignee": "the operator",
                "completed": False,
            }
        ],
        "krisp_key_points": [],
    }
    msg = mod.format_debrief(pending)
    assert "the operator: " in msg
    assert "Alexis: " not in msg


# ─── coaching history ───────────────────────────────────────────────


def test_load_coaching_history_accepts_dict_with_history_key(mod, tmp_path, monkeypatch):
    """Legacy LLM-cron wrote coaching-history.json as
    ``{"history": [...]}`` while the Python orchestrator assumed a bare
    list. The mismatch silently returned [] from _load, which meant
    every _append_coaching_history wiped prior entries and
    _already_coached always returned False — double-coaching any meeting
    the operator got on the old cron. Loader must accept both shapes."""
    history = tmp_path / "coaching-history.json"
    history.write_text(
        json.dumps({
            "history": [
                {"event_id": "legacy-1", "meeting_title": "Old meeting"},
                {"event_id": "legacy-2", "meeting_title": "Older meeting"},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "COACHING_HISTORY_FILE", history)
    loaded = mod._load_coaching_history()
    assert isinstance(loaded, list)
    assert len(loaded) == 2
    assert loaded[0]["event_id"] == "legacy-1"
    assert mod._already_coached("legacy-1") is True
    assert mod._already_coached("legacy-2") is True
    assert mod._already_coached("new-event") is False


def test_append_coaching_history_preserves_legacy_dict_shape_entries(mod, tmp_path, monkeypatch):
    """Appending a new entry to a dict-shaped legacy file must preserve
    the prior entries — it was silently wiping them before the loader
    fix landed."""
    history = tmp_path / "coaching-history.json"
    history.write_text(
        json.dumps({"history": [{"event_id": "legacy-1", "meeting_title": "x"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "COACHING_HISTORY_FILE", history)
    mod._append_coaching_history({"event_id": "new-1", "meeting_title": "y"})
    after = mod._load_coaching_history()
    ids = [e.get("event_id") for e in after]
    assert "legacy-1" in ids
    assert "new-1" in ids


def test_already_coached_true_when_event_id_present(mod, tmp_path, monkeypatch):
    history = tmp_path / "coaching-history.json"
    history.write_text(
        json.dumps([{"event_id": "evt-alexis-420", "meeting_title": "x"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "COACHING_HISTORY_FILE", history)
    assert mod._already_coached("evt-alexis-420") is True
    assert mod._already_coached("evt-other") is False


def test_already_coached_false_when_missing_file(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "COACHING_HISTORY_FILE", tmp_path / "missing.json")
    assert mod._already_coached("evt-alexis-420") is False


def test_append_coaching_history_idempotent(mod, tmp_path, monkeypatch):
    history = tmp_path / "coaching-history.json"
    monkeypatch.setattr(mod, "COACHING_HISTORY_FILE", history)

    entry = {"event_id": "evt-1", "meeting_title": "Alexis 1:1"}
    mod._append_coaching_history(entry)
    mod._append_coaching_history(entry)  # second call, same event_id
    data = json.loads(history.read_text(encoding="utf-8"))
    assert len([e for e in data if e["event_id"] == "evt-1"]) == 1


# ─── cleanup_confirmed_pending ───────────────────────────────────────


def test_cleanup_deletes_pending_whose_event_id_is_in_active(
    mod, tmp_path, monkeypatch
):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "pending-debrief-evt-a.json").write_text(
        json.dumps({"event_id": "evt-a"}), encoding="utf-8"
    )
    (cache / "pending-debrief-evt-b.json").write_text(
        json.dumps({"event_id": "evt-b"}), encoding="utf-8"
    )
    active = tmp_path / "active.md"
    active.write_text(
        "- **id:** commit-1\n- **event_id:** evt-a\n- **what:** do the thing\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active)

    deleted = mod._cleanup_confirmed_pending()
    assert deleted == 1
    assert not (cache / "pending-debrief-evt-a.json").exists()
    assert (cache / "pending-debrief-evt-b.json").exists()


def test_cleanup_tolerates_missing_active_file(mod, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "pending-debrief-evt-a.json").write_text(
        json.dumps({"event_id": "evt-a"}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "CACHE_DIR", cache)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", tmp_path / "missing.md")
    deleted = mod._cleanup_confirmed_pending()
    assert deleted == 0
    assert (cache / "pending-debrief-evt-a.json").exists()


# ─── run() happy path + invariants ───────────────────────────────────


def _patch_common(mod, tmp_path, monkeypatch, *, scan, pending_fixture=None,
                  metrics=None, meeting_cfg=None, active_text=""):
    workspace = tmp_path / "meetings-coach-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    history = workspace / "cache" / "coaching-history.json"
    active_file = tmp_path / "active.md"
    active_file.write_text(active_text, encoding="utf-8")

    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(mod, "COACHING_HISTORY_FILE", history)
    monkeypatch.setattr(mod, "ACTIVE_COMMITMENTS_FILE", active_file)
    monkeypatch.setattr(
        mod, "KRISP_LAST_401_FILE", workspace / "cache" / "krisp-last-401.json"
    )
    monkeypatch.setattr(
        mod, "KRISP_LAST_ALERT_FILE", workspace / "cache" / "krisp-last-alert.json"
    )
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-post-meeting.json"
    )
    monkeypatch.setattr(
        mod, "MEETING_CONFIG_FILE", workspace / "meeting-config.json"
    )

    if meeting_cfg is not None:
        (workspace / "meeting-config.json").write_text(
            json.dumps(meeting_cfg), encoding="utf-8"
        )

    if pending_fixture is not None:
        event_id = pending_fixture["event_id"]
        (workspace / "cache" / f"pending-debrief-{event_id}.json").write_text(
            json.dumps(pending_fixture), encoding="utf-8"
        )

    def fake_run(script, *args, **kw):
        if script == "gcal-fetch.py":
            return {"status": "ok", "events": []}
        if script == "transcript-scan.py":
            return scan
        if script == "transcript-metrics.py":
            return metrics
        return None

    monkeypatch.setattr(mod, "_run_script", fake_run)
    return workspace, history


def test_run_happy_path_sends_debrief_and_coaching(
    mod, scan_one_new, pending_alexis, metrics_alexis, meeting_config,
    tmp_path, monkeypatch
):
    workspace, history = _patch_common(
        mod, tmp_path, monkeypatch,
        scan=scan_one_new,
        pending_fixture=pending_alexis,
        metrics=metrics_alexis,
        meeting_cfg=meeting_config,
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )
    monkeypatch.setattr(mod, "llm_infer", lambda p, **kw: _fake_infer(_coaching_llm_reply()))

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 17, 15, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["processed"] == 1
    assert result["debriefs_sent"] == 1
    assert result["coaching_sent"] == 1

    # Exactly two messages: debrief + coaching
    assert len(sent) == 2
    assert "Debrief ready" in sent[0] or "Alexis" in sent[0]
    assert "Meeting Coach" in sent[1] or "CONCISION" in sent[1].upper()

    # Coaching history entry appended
    data = json.loads(history.read_text(encoding="utf-8"))
    ids = [e["event_id"] for e in data]
    assert ids == ["evt-alexis-420"]


def test_compose_coaching_feeds_transcript_dialogue_not_notes(
    mod, meeting_config, monkeypatch,
):
    """Krisp's document is notes + transcript concatenated. When we feed
    the raw blob to the LLM, the first 3000 chars are mostly notes
    (action items, key points) and the LLM keeps saying 'too little
    material to assess'. The coaching prompt must receive the TRANSCRIPT
    BODY — speaker turns — not the notes section.
    Regression: 2026-04-16 Meet & Greet coaching said 'not enough
    material' for a 25-min meeting with 19 speaker turns."""
    growth_areas = meeting_config["coaching"]["growth_areas"]
    pending = {
        "event_id": "e-talk",
        "meeting_title": "Meet & Greet",
        "transcript_text": (
            "# Meet & Greet\n### Action Items\n- Speaker_2 to schedule more calls.\n"
            "### Key Points\n- Purpose: get to know each other.\n- Steve works at Meta.\n\n"
            "**Sam Smith | 00:17**\nTesting testing.\n\n"
            "**Steve Shadman | 02:45**\nHi the operator, good to meet you. "
            "Thanks for taking the time. I lead the Monetization Ecosystem "
            "DS team at Meta which is a horizontal team that owns "
            "attribution, measurement, and the ad auction infrastructure.\n\n"
            "**Sam Smith | 03:12**\nGreat, yeah, tell me more about "
            "what you're looking for in this role.\n\n"
        ),
    }
    metrics = {"status": "ok", "metrics": {"talk_ratio": 0.46}}

    seen_prompts: list = []
    def fake_infer(prompt, **kw):
        seen_prompts.append(prompt)
        return _fake_infer(_coaching_llm_reply())
    monkeypatch.setattr(mod, "llm_infer", fake_infer)

    mod._compose_coaching_message(pending, metrics, growth_areas)
    prompt = seen_prompts[0]
    # The prompt transcript excerpt must include speaker turns, not just
    # the notes section.
    assert "**Sam Smith" in prompt
    assert "**Steve Shadman" in prompt
    assert "Monetization Ecosystem" in prompt
    # Notes should NOT be in the transcript excerpt (they're already
    # summarized as action_items/key_points on the debrief).
    assert "### Action Items" not in prompt
    assert "### Key Points" not in prompt


def test_build_transcript_data_strips_notes_prefix(monkeypatch):
    """build_transcript_data (transcript-scan.py) should anchor stored
    transcript_text at the first speaker-turn marker so downstream
    consumers (transcript-metrics, coaching) see dialogue, not notes."""
    import importlib.util
    ts_path = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "transcript-scan.py"
    spec = importlib.util.spec_from_file_location("transcript_scan", ts_path)
    ts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ts)

    transcript = {
        "id": "x",
        "title": "M",
        "participants": ["the operator", "Steve"],
        "speakers": ["Sam Smith", "Steve Shadman"],
        "key_points": ["KP"],
        "action_items": [{"title": "AI1"}],
        "text": (
            "# Meeting Title\n### Action Items\n- X\n### Key Points\n- Y\n\n"
            "## Transcript\n\n"
            "**Sam Smith | 00:10**\nHello.\n\n"
            "**Steve Shadman | 00:12**\nHi!\n"
        ),
        "source": "krisp_mcp",
    }
    data = ts.build_transcript_data(transcript)
    text = data["transcript_text"]
    assert text.startswith("**Sam Smith")
    assert "**Steve Shadman" in text
    assert "### Action Items" not in text
    assert "### Key Points" not in text


def test_parse_doc_bullets_extracts_section_content():
    """Krisp leaves meeting_notes.key_points empty for many meetings
    even when the document Markdown has rich '### Key Points' content
    (e.g. 2026-04-16 Steven Oliver interview — 4 substantive bullets
    in the doc, zero in meeting_notes metadata). The fallback parser
    must pull bullets from the doc text."""
    import importlib.util
    ts_path = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "transcript-scan.py"
    spec = importlib.util.spec_from_file_location("transcript_scan", ts_path)
    ts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ts)

    doc = (
        "# Google Meet with Steven Oliver\n"
        "### Action Items\n\n\n\n"
        "### Key Points\n"
        "- the operator interviewed with Steven Oliver for a DS lead role.\n"
        "- Steven leads horizontal DS teams across search growth.\n"
        "- Core metrics must be split by stakeholder.\n"
        "\n"
        "**Sam Smith | 00:10**\nHello.\n"
    )
    ai = ts._parse_doc_bullets(doc, "Action Items")
    kp = ts._parse_doc_bullets(doc, "Key Points")
    assert ai == []
    assert len(kp) == 3
    assert "the operator interviewed" in kp[0]
    assert "Steven leads" in kp[1]
    assert "Core metrics" in kp[2]


def test_parse_doc_bullets_stops_at_transcript_start():
    """The bullet extractor must not spill into the transcript body
    (speaker turns aren't bullets but a greedy slice could eat them)."""
    import importlib.util
    ts_path = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "transcript-scan.py"
    spec = importlib.util.spec_from_file_location("transcript_scan", ts_path)
    ts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ts)

    doc = (
        "### Key Points\n"
        "- Point one.\n"
        "- Point two.\n\n"
        "**Sam Smith | 00:10**\n- This is a dash in dialogue\n"
    )
    kp = ts._parse_doc_bullets(doc, "Key Points")
    assert kp == ["Point one.", "Point two."]


def test_build_transcript_data_falls_back_to_doc_parse():
    """When meeting_notes arrays are empty, populate krisp_action_items
    and krisp_key_points from the doc's Markdown sections."""
    import importlib.util
    ts_path = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "transcript-scan.py"
    spec = importlib.util.spec_from_file_location("transcript_scan", ts_path)
    ts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ts)

    transcript = {
        "id": "x", "title": "M",
        "participants": ["the operator", "Steve"],
        "speakers": ["Sam Smith", "Steve Shadman"],
        "key_points": [],
        "action_items": [],
        "text": (
            "# Meeting\n### Action Items\n"
            "- the operator to draft the role spec.\n"
            "### Key Points\n"
            "- Steve's team owns attribution.\n"
            "- Focus on user metrics first.\n\n"
            "**Sam Smith | 00:10**\nHi.\n"
        ),
        "source": "krisp_mcp",
    }
    data = ts.build_transcript_data(transcript)
    assert data["krisp_action_items"] == ["the operator to draft the role spec."]
    assert len(data["krisp_key_points"]) == 2
    assert "attribution" in data["krisp_key_points"][0]


def test_build_transcript_data_preserves_meeting_notes_when_populated():
    """If meeting_notes is populated, trust it — structured items carry
    {title, assignee, completed}; the doc text is a stringified view."""
    import importlib.util
    ts_path = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "transcript-scan.py"
    spec = importlib.util.spec_from_file_location("transcript_scan", ts_path)
    ts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ts)

    transcript = {
        "id": "x", "title": "M",
        "participants": [], "speakers": [],
        "key_points": ["from meeting_notes"],
        "action_items": [{"title": "structured", "assignee": "the operator"}],
        "text": "### Action Items\n- different text\n### Key Points\n- other\n\n**A | 00:01**\nX\n",
        "source": "krisp_mcp",
    }
    data = ts.build_transcript_data(transcript)
    # meeting_notes wins — structured items preserved, doc parse NOT used.
    assert data["krisp_action_items"] == [{"title": "structured", "assignee": "the operator"}]
    assert data["krisp_key_points"] == ["from meeting_notes"]


def test_build_transcript_data_transcript_cap_allows_full_meeting():
    """Feed the whole transcript to the LLM. A 25-min meeting is ~20K
    chars of dialogue; a 1-hr meeting ~40-50K. The cap must comfortably
    accommodate realistic meeting lengths (well above the prior 16K)."""
    import importlib.util
    ts_path = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "transcript-scan.py"
    spec = importlib.util.spec_from_file_location("transcript_scan", ts_path)
    ts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ts)

    big_body = "**Sam Smith | 00:01**\n" + ("word " * 8000) + "\n"
    transcript = {
        "id": "x", "title": "M",
        "participants": [], "speakers": [],
        "key_points": [], "action_items": [],
        "text": big_body,
        "source": "krisp_mcp",
    }
    data = ts.build_transcript_data(transcript)
    assert len(data["transcript_text"]) >= 40000


def test_run_suppresses_empty_debrief_delivery(
    mod, scan_one_new, meeting_config, tmp_path, monkeypatch,
):
    """Debriefs with no action items AND no key points carry zero
    signal — just a title and buttons. Skip delivery so the operator isn't
    paged with nothing to act on. The transcript is still marked
    processed; if Krisp enriches the notes later, a manual replay
    can re-stage."""
    empty_pending = {
        "event_id": "evt-alexis-420",
        "meeting_title": "Google Meet with X",
        "meeting_start": "2026-04-16T16:00:00-07:00",
        "krisp_action_items": [],
        "krisp_key_points": [],
        "krisp_speakers": ["Sam Smith", "X"],
        "transcript_text": "**Sam Smith | 00:10**\nHi.\n",
        "participants": ["Sam Smith", "X"],
        "status": "pending_review",
    }
    workspace, history = _patch_common(
        mod, tmp_path, monkeypatch,
        scan=scan_one_new,
        pending_fixture=empty_pending,
        meeting_cfg=meeting_config,
    )
    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 16, 23, 50, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["debriefs_sent"] == 0
    assert sent == []


def test_compose_coaching_retries_once_on_transient_llm_failure(
    mod, pending_alexis, metrics_alexis, meeting_config, monkeypatch,
):
    """The coaching LLM occasionally returns ok=False on transient
    auth/broker/network blips. One silent failure today (2026-04-16
    19:45 UTC) meant the operator got no coaching on a 25-min meeting. Retry
    once before giving up — a single retry buys resilience against the
    vast majority of transient errors without inflating cron cost."""
    growth_areas = meeting_config["coaching"]["growth_areas"]
    attempts: list = []
    sleep_calls: list = []

    def fake_infer(prompt, **kw):
        attempts.append(prompt)
        if len(attempts) == 1:
            return SimpleNamespace(
                ok=False, text="", error="broker timeout",
                input_tokens=0, output_tokens=0, model="fake",
            )
        return _fake_infer(_coaching_llm_reply())

    monkeypatch.setattr(mod, "llm_infer", fake_infer)
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleep_calls.append(s))

    result = mod._compose_coaching_message(pending_alexis, metrics_alexis, growth_areas)
    assert result is not None
    assert len(attempts) == 2
    assert sleep_calls  # inter-attempt backoff was observed


def test_compose_coaching_retries_once_on_json_parse_failure(
    mod, pending_alexis, metrics_alexis, meeting_config, monkeypatch,
):
    """Malformed LLM output (non-JSON or truncated) should also trigger
    one retry. Symptom today was a swallowed JSONDecodeError → None."""
    growth_areas = meeting_config["coaching"]["growth_areas"]
    attempts: list = []

    def fake_infer(prompt, **kw):
        attempts.append(prompt)
        if len(attempts) == 1:
            return SimpleNamespace(
                ok=True, text="<<not json at all>>", error=None,
                input_tokens=0, output_tokens=0, model="fake",
            )
        return _fake_infer(_coaching_llm_reply())

    monkeypatch.setattr(mod, "llm_infer", fake_infer)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    result = mod._compose_coaching_message(pending_alexis, metrics_alexis, growth_areas)
    assert result is not None
    assert len(attempts) == 2


def test_compose_coaching_returns_none_after_both_attempts_fail(
    mod, pending_alexis, metrics_alexis, meeting_config, monkeypatch,
):
    """When retry also fails, return None and let the caller count it
    as a failure. Don't retry more than once — we're on a cron budget."""
    growth_areas = meeting_config["coaching"]["growth_areas"]
    attempts: list = []

    def fake_infer(prompt, **kw):
        attempts.append(prompt)
        return SimpleNamespace(
            ok=False, text="", error="still down",
            input_tokens=0, output_tokens=0, model="fake",
        )

    monkeypatch.setattr(mod, "llm_infer", fake_infer)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    result = mod._compose_coaching_message(pending_alexis, metrics_alexis, growth_areas)
    assert result is None
    # Exactly one retry — not a retry storm.
    assert len(attempts) == 2


def test_run_skips_coaching_on_metrics_too_short_without_llm_call(
    mod, scan_one_new, pending_alexis, meeting_config, tmp_path, monkeypatch
):
    """INVARIANT: transcript-metrics.py emits {status: 'too_short'} for
    transcripts below the min_transcript_length threshold (see
    meeting-config.json). The coaching gate must recognize this and
    skip compose — otherwise _compose_coaching_message receives a dict
    without 'metrics', the LLM is called with n/a-filled prompt, returns
    junk, and coaching_llm_failures quietly ticks up while the operator sees
    NO coaching message and no warning. 2026-04-16 regression (Meet &
    Greet the operator/Steve, 5-min transcript)."""
    too_short = {
        "status": "too_short",
        "event_id": "evt-alexis-420",
        "transcript_length": 120,
        "min_length": 500,
        "message": "Transcript too short for meaningful coaching",
    }
    workspace, history = _patch_common(
        mod, tmp_path, monkeypatch,
        scan=scan_one_new,
        pending_fixture=pending_alexis,
        metrics=too_short,
        meeting_cfg=meeting_config,
    )

    sent: list[str] = []
    llm_calls: list = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )
    monkeypatch.setattr(
        mod, "llm_infer",
        lambda p, **kw: (llm_calls.append(p), _fake_infer(_coaching_llm_reply()))[1],
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 16, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    # Debrief still sends even when transcript is too short for coaching.
    assert result["status"] == "ok"
    assert result["debriefs_sent"] == 1
    assert result["coaching_sent"] == 0
    # Must NOT count as an LLM failure — it's a legitimate skip, not an error.
    assert result["coaching_llm_failures"] == 0
    # No LLM prompt was composed for coaching — silent skip.
    assert llm_calls == []
    # No coaching message went out; only the debrief did.
    assert len(sent) == 1
    assert "Debrief ready" in sent[0]


def test_run_empty_processed_is_silent(
    mod, scan_empty, pending_alexis, tmp_path, monkeypatch
):
    """INVARIANT: even if a pending-debrief-*.json file exists on disk,
    if it's NOT in scan.processed the orchestrator must NOT send it.
    This is the 2026-04-14 Alexis incident regression test."""
    workspace, history = _patch_common(
        mod, tmp_path, monkeypatch,
        scan=scan_empty,
        pending_fixture=pending_alexis,  # file exists on disk but processed=[]
        meeting_cfg={"coaching": {"enabled": True, "growth_areas": []}},
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )
    monkeypatch.setattr(mod, "llm_infer", lambda p, **kw: _fake_infer({}))

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 17, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result["processed"] == 0
    assert result["debriefs_sent"] == 0
    assert result["coaching_sent"] == 0
    assert sent == []

    # History must remain empty — no coaching entry appended
    assert not history.exists() or json.loads(history.read_text(encoding="utf-8")) == []


def test_run_coaching_dedup_skips_when_already_in_history(
    mod, scan_one_new, pending_alexis, metrics_alexis, meeting_config,
    tmp_path, monkeypatch
):
    """INVARIANT: if coaching-history.json already has an entry for
    event_id, a re-run must NOT re-send coaching (2026-04-14 bug)."""
    workspace, history = _patch_common(
        mod, tmp_path, monkeypatch,
        scan=scan_one_new,
        pending_fixture=pending_alexis,
        metrics=metrics_alexis,
        meeting_cfg=meeting_config,
    )
    # Pre-seed the history with this event_id
    history.write_text(
        json.dumps([{"event_id": "evt-alexis-420", "meeting_title": "Alexis 1:1"}]),
        encoding="utf-8",
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )
    monkeypatch.setattr(mod, "llm_infer", lambda p, **kw: _fake_infer(_coaching_llm_reply()))

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 17, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    # Debrief still sends (processed has the item)
    assert result["debriefs_sent"] == 1
    # Coaching skipped (already in history)
    assert result["coaching_sent"] == 0
    assert len(sent) == 1

    # History unchanged (still 1 entry)
    data = json.loads(history.read_text(encoding="utf-8"))
    assert len(data) == 1


def test_run_cleanup_pass_runs_and_does_not_send(
    mod, scan_empty, pending_alexis, tmp_path, monkeypatch
):
    """Cleanup pass deletes pending files whose event_id landed in
    active.md. It does NOT send anything — delivery is separate."""
    workspace, _ = _patch_common(
        mod, tmp_path, monkeypatch,
        scan=scan_empty,
        pending_fixture=pending_alexis,
        meeting_cfg={"coaching": {"enabled": True, "growth_areas": []}},
        active_text="- **id:** c-1\n- **event_id:** evt-alexis-420\n- **what:** x\n",
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )
    monkeypatch.setattr(mod, "llm_infer", lambda p, **kw: _fake_infer({}))

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 17, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["cleanup_count"] == 1
    assert result["debriefs_sent"] == 0
    assert sent == []
    assert not (workspace / "cache" / "pending-debrief-evt-alexis-420.json").exists()


# ─── Krisp 401 state machine ─────────────────────────────────────────


def test_run_krisp_401_fresh_sends_alert_and_stamps_markers(
    mod, scan_401, tmp_path, monkeypatch
):
    workspace, _ = _patch_common(
        mod, tmp_path, monkeypatch,
        scan=scan_401,
        meeting_cfg={"coaching": {"enabled": True, "growth_areas": []}},
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 17, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["krisp_401"] is True
    # Exactly one alert — no rate-limit suppression on first run
    assert len(sent) == 1
    assert "Krisp" in sent[0] and ("expired" in sent[0].lower() or "re-login" in sent[0].lower())

    # Both marker files stamped
    assert (workspace / "cache" / "krisp-last-401.json").exists()
    assert (workspace / "cache" / "krisp-last-alert.json").exists()


def test_run_krisp_401_rate_limited_within_90min_does_not_spam(
    mod, scan_401, tmp_path, monkeypatch
):
    """If a Krisp 401 alert was sent in the last 90 min, don't send
    another one on this run. Still stamps the 401 marker (heartbeat
    needs it for awareness)."""
    workspace, _ = _patch_common(
        mod, tmp_path, monkeypatch,
        scan=scan_401,
        meeting_cfg={"coaching": {"enabled": True, "growth_areas": []}},
    )
    # Pre-seed the alert marker with mtime 30 min ago
    alert_marker = workspace / "cache" / "krisp-last-alert.json"
    alert_marker.write_text('{"stamped": true}', encoding="utf-8")
    old_time = time.time() - (30 * 60)  # 30 min ago
    import os as _os
    _os.utime(str(alert_marker), (old_time, old_time))

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: sent.append(text) or True
    )

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 15, 17, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["krisp_401"] is True
    # Suppressed by rate limit
    assert sent == []
    # 401 marker still stamped (fresh)
    assert (workspace / "cache" / "krisp-last-401.json").exists()


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
