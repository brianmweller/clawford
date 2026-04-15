"""Tests for agents/connector/scripts/notes-triage-alert.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`connector:notes-triage`. Runs the existing I/O script notes-triage.py
to fetch untriaged inbox notes, skips IDs already in pending-triage.json,
LLM-classifies each new note into {fact | commitment | task | shopping
| unclear}, and sends one Telegram message presenting up to 10 items
with /confirm and /dismiss N commands.

LLM use (per logic-gate rule): legitimate — note bodies are free-form
natural language and categorization judgment is the point.

TDD: tests land before the implementation.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "connector" / "scripts" / "notes-triage-alert.py"
FIXTURES = Path(__file__).parent / "fixtures" / "notes-triage-alert"


def _load():
    spec = importlib.util.spec_from_file_location("notes_triage_alert", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def triage_output():
    with open(FIXTURES / "notes-triage-output.json", encoding="utf-8") as f:
        return json.load(f)


def _fake_infer_result(category: str, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        ok=True,
        text=json.dumps({"category": category, "reason": reason}),
        error=None,
        input_tokens=0,
        output_tokens=0,
        model="fake",
    )


# ─── classify_note ───────────────────────────────────────────────────


def test_classify_note_returns_category_and_reason(mod, monkeypatch):
    monkeypatch.setattr(
        mod, "llm_infer",
        lambda prompt, **kw: _fake_infer_result("fact", "mentions health")
    )
    result, failed = mod.classify_note({"content": "x", "id": "1"})
    assert failed is False
    assert result["category"] == "fact"
    assert result["reason"] == "mentions health"


def test_classify_note_falls_through_to_unclear_on_llm_failure(mod, monkeypatch):
    monkeypatch.setattr(
        mod, "llm_infer",
        lambda prompt, **kw: SimpleNamespace(
            ok=False, text=None, error="down", input_tokens=0, output_tokens=0, model=None
        ),
    )
    result, failed = mod.classify_note({"content": "x", "id": "1"})
    assert failed is True
    assert result["category"] == "unclear"


def test_classify_note_rejects_unknown_category(mod, monkeypatch):
    monkeypatch.setattr(
        mod, "llm_infer",
        lambda prompt, **kw: _fake_infer_result("garbage", "")
    )
    result, failed = mod.classify_note({"content": "x", "id": "1"})
    # Malformed category gets coerced to "unclear"
    assert result["category"] == "unclear"


def test_classify_note_strips_markdown_fence(mod, monkeypatch):
    """If the LLM wraps its JSON in ```json ... ```, parser unwraps."""
    fenced = SimpleNamespace(
        ok=True,
        text='```json\n{"category": "task", "reason": "Sam needs to"}\n```',
        error=None, input_tokens=0, output_tokens=0, model="fake",
    )
    monkeypatch.setattr(mod, "llm_infer", lambda prompt, **kw: fenced)
    result, failed = mod.classify_note({"content": "x", "id": "1"})
    assert failed is False
    assert result["category"] == "task"


# ─── format_message ──────────────────────────────────────────────────


def test_format_message_includes_count_and_categories(mod):
    classified = [
        ({"id": "note-001", "content": "Priya's chemo is Monday"},
         {"category": "fact", "reason": "health note about Priya"}),
        ({"id": "note-002", "content": "Grab paper towels"},
         {"category": "shopping", "reason": "household item"}),
    ]
    msg = mod.format_message(classified)
    assert "2 new" in msg or "2 " in msg
    assert "Priya's chemo" in msg
    assert "fact" in msg
    assert "Grab paper towels" in msg
    assert "shopping" in msg
    assert "/confirm" in msg
    assert "/dismiss" in msg


def test_format_message_adds_hidden_footer_when_truncated(mod):
    classified = [
        ({"id": f"n-{i}", "content": f"note {i}"},
         {"category": "task", "reason": "todo"})
        for i in range(10)
    ]
    msg = mod.format_message(classified, hidden=3)
    assert "3 more" in msg


# ─── run() orchestration ─────────────────────────────────────────────


def test_run_silent_when_no_untriaged_notes(mod, tmp_path, monkeypatch):
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "cache").mkdir()
    triage_file = workspace / "pending-triage.json"
    triage_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "TRIAGE_FILE", triage_file)
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-notes-triage.json"
    )

    monkeypatch.setattr(
        mod, "_run_script",
        lambda *a, **kw: {"status": "ok", "untriaged": [], "count": 0}
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message",
        lambda tok, chat, text, **kw: sent.append(text) or True,
    )

    result = mod.run()
    assert result["status"] == "ok"
    assert result["new_notes"] == 0
    assert result["sent"] == 0
    assert sent == []


def test_run_sends_when_new_notes_exist(mod, triage_output, tmp_path, monkeypatch):
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "cache").mkdir()
    triage_file = workspace / "pending-triage.json"
    triage_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "TRIAGE_FILE", triage_file)
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-notes-triage.json"
    )

    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: triage_output)

    def _content_section(prompt: str) -> str:
        """Extract only the 'Note to categorize:' tail so category
        example text in the prompt template doesn't contaminate matching."""
        marker = "Note to categorize:"
        idx = prompt.rfind(marker)
        return prompt[idx + len(marker):] if idx != -1 else prompt

    def fake_infer(prompt, **kw):
        section = _content_section(prompt)
        if "chemo" in section:
            return _fake_infer_result("fact", "health note about Priya")
        if "portfolio" in section:
            return _fake_infer_result("commitment", "the operator to Jay, Friday")
        if "dentist" in section:
            return _fake_infer_result("task", "book an appointment")
        if "paper towels" in section:
            return _fake_infer_result("shopping", "household item")
        return _fake_infer_result("unclear", "")

    monkeypatch.setattr(mod, "llm_infer", fake_infer)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message",
        lambda tok, chat, text, **kw: sent.append(text) or True,
    )

    result = mod.run()
    assert result["status"] == "ok"
    assert result["new_notes"] == 5
    assert result["sent"] == 1  # one Telegram message with batched items
    # All 5 notes appear in the message
    assert "chemo" in sent[0]
    assert "paper towels" in sent[0]
    assert "fact" in sent[0]
    assert "shopping" in sent[0]


def test_run_skips_already_presented(mod, triage_output, tmp_path, monkeypatch):
    """Notes whose id is already in pending-triage.json should be
    filtered out before classification."""
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "cache").mkdir()
    triage_file = workspace / "pending-triage.json"
    triage_file.write_text(
        json.dumps(
            [
                {"id": "note-001", "created_at": "2026-04-13T20:00:00Z"},
                {"id": "note-003", "created_at": "2026-04-14T08:20:00Z"},
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "TRIAGE_FILE", triage_file)
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-notes-triage.json"
    )
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: triage_output)

    infer_calls: list[str] = []

    def fake_infer(prompt, **kw):
        infer_calls.append(prompt)
        return _fake_infer_result("task", "")

    monkeypatch.setattr(mod, "llm_infer", fake_infer)

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message",
        lambda tok, chat, text, **kw: sent.append(text) or True,
    )

    result = mod.run()
    assert result["status"] == "ok"
    # 5 untriaged in fixture − 2 already pending = 3 new
    assert result["new_notes"] == 3
    # Only 3 classifications happened
    assert len(infer_calls) == 3
    # Skipped notes should NOT appear in the sent message
    assert "chemo" not in sent[0]  # note-001
    assert "dentist" not in sent[0]  # note-003
    assert "portfolio" in sent[0]  # note-002
    assert "paper towels" in sent[0]  # note-004


def test_run_appends_new_ids_to_pending_triage(mod, triage_output, tmp_path, monkeypatch):
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "cache").mkdir()
    triage_file = workspace / "pending-triage.json"
    triage_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "TRIAGE_FILE", triage_file)
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-notes-triage.json"
    )
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: triage_output)
    monkeypatch.setattr(
        mod, "llm_infer",
        lambda prompt, **kw: _fake_infer_result("task", "")
    )
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message", lambda tok, chat, text, **kw: True
    )

    result = mod.run()
    assert result["status"] == "ok"

    with open(triage_file, encoding="utf-8") as f:
        pending = json.load(f)
    ids = {e["id"] for e in pending}
    assert ids == {"note-001", "note-002", "note-003", "note-004", "note-005"}


def test_run_caps_batch_at_max_size(mod, tmp_path, monkeypatch):
    """More than max_batch_size new notes → show first N, report N_more."""
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "cache").mkdir()
    triage_file = workspace / "pending-triage.json"
    triage_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "TRIAGE_FILE", triage_file)
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-notes-triage.json"
    )

    big_payload = {
        "status": "ok",
        "untriaged": [
            {"id": f"n-{i}", "content": f"note {i}", "created_at": "2026-04-14T08:00:00Z"}
            for i in range(15)
        ],
        "count": 15,
    }
    monkeypatch.setattr(mod, "_run_script", lambda *a, **kw: big_payload)
    monkeypatch.setattr(
        mod, "llm_infer",
        lambda prompt, **kw: _fake_infer_result("task", "")
    )

    sent: list[str] = []
    monkeypatch.setattr(mod, "resolve_credentials", lambda env: ("tok", "chat"))
    monkeypatch.setattr(
        mod, "send_message",
        lambda tok, chat, text, **kw: sent.append(text) or True,
    )

    result = mod.run()
    assert result["status"] == "ok"
    # 10 shown, 5 hidden
    assert result["new_notes"] == 10
    assert result.get("hidden", 0) == 5
    assert "5 more" in sent[0]


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
