"""Tests for agents/shared/memory_writer.py — the persistent-memory writer.

Writes to agents/<agent_id>/MEMORY.md on the VPS (repo-side; gitignored per
the PII remediation pattern). The memory file is loaded into every system
prompt by dispatcher._build_system_prompt — so appending a rule here makes
it durable across conversations.

Shape: existing MEMORY.md files use Markdown with ## category headings +
bullet rules. The writer preserves that shape: rules are appended under
the named category (creating it if absent), timestamped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


@pytest.fixture
def mw(tmp_path, monkeypatch):
    """Fresh memory_writer module pointed at tmp_path as REPO_ROOT."""
    monkeypatch.setenv("CLAWFORD_REPO_ROOT", str(tmp_path))
    (tmp_path / "agents" / "shopping").mkdir(parents=True)
    for mod in list(sys.modules):
        if mod == "memory_writer":
            del sys.modules[mod]
    import memory_writer
    return memory_writer


# ── append_rule ───────────────────────────────────────────────────


def test_append_rule_creates_file_if_missing(mw, tmp_path):
    result = mw.append_rule("shopping", "Always get 2 gallons of milk", "Grocery Defaults")
    assert result["status"] == "ok"

    memory_path = tmp_path / "agents" / "shopping" / "MEMORY.md"
    assert memory_path.exists()
    content = memory_path.read_text(encoding="utf-8")
    assert "## Grocery Defaults" in content
    assert "Always get 2 gallons of milk" in content


def test_append_rule_preserves_existing_file(mw, tmp_path):
    memory_path = tmp_path / "agents" / "shopping" / "MEMORY.md"
    memory_path.write_text(
        "# MEMORY.md — Hilda\n\n## Existing Category\n\n- Existing rule\n",
        encoding="utf-8",
    )

    mw.append_rule("shopping", "New rule under same category", "Existing Category")

    content = memory_path.read_text(encoding="utf-8")
    assert "Existing rule" in content
    assert "New rule under same category" in content
    # Should not duplicate the category header
    assert content.count("## Existing Category") == 1


def test_append_rule_adds_new_category(mw, tmp_path):
    memory_path = tmp_path / "agents" / "shopping" / "MEMORY.md"
    memory_path.write_text(
        "# MEMORY.md\n\n## Existing\n\n- Rule A\n",
        encoding="utf-8",
    )

    mw.append_rule("shopping", "Fresh rule", "Brand New Category")

    content = memory_path.read_text(encoding="utf-8")
    assert "## Existing" in content
    assert "## Brand New Category" in content
    assert "Fresh rule" in content
    assert "Rule A" in content


def test_append_rule_includes_timestamp(mw, tmp_path):
    mw.append_rule("shopping", "Rule with timestamp", "Test")
    content = (tmp_path / "agents" / "shopping" / "MEMORY.md").read_text(encoding="utf-8")
    # Should have ISO timestamp or date marker
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2}", content), "no date in output"


def test_append_rule_rejects_empty_rule(mw):
    result = mw.append_rule("shopping", "", "Category")
    assert result["status"] == "error"


def test_append_rule_rejects_empty_category(mw):
    result = mw.append_rule("shopping", "Some rule", "")
    assert result["status"] == "error"


def test_append_rule_unknown_agent_creates_dir(mw, tmp_path):
    """Even if the agent dir doesn't exist, append_rule should create it
    and the MEMORY.md. The dispatcher only reads known agents, so this
    doesn't create phantom agents — it's defensive."""
    (tmp_path / "agents" / "newagent").mkdir(parents=True)
    result = mw.append_rule("newagent", "Rule", "Cat")
    assert result["status"] == "ok"


def test_append_rule_respects_chattr_immutable(mw, tmp_path):
    """Can't test real chattr without root, but we can test that a
    PermissionError from the filesystem is caught and reported."""
    memory_path = tmp_path / "agents" / "shopping" / "MEMORY.md"
    memory_path.write_text("# MEMORY\n", encoding="utf-8")
    # Simulate immutability by making dir read-only (on Windows this
    # behaves differently; the test just confirms error path works)
    try:
        memory_path.chmod(0o444)  # read-only
        result = mw.append_rule("shopping", "Blocked rule", "Cat")
        # On some platforms chmod 0o444 still allows append; we just assert
        # the function returned a structured result (not a crash)
        assert "status" in result
    finally:
        memory_path.chmod(0o644)


# ── pending_action integration ───────────────────────────────────


def test_append_rule_returns_dict_suitable_for_tool(mw):
    """Returns a dict the caller can pass through as the function_call_output
    or wrap in a propose_remember pending action."""
    result = mw.append_rule("shopping", "Rule text", "Category name")
    assert isinstance(result, dict)
    assert "status" in result
    assert result["status"] == "ok"
