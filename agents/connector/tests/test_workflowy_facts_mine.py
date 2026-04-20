"""Tests for agents/connector/scripts/workflowy-facts-mine.py.

Daily cron that pulls the operator's Workflowy tree, finds nodes whose text
mentions a known person by name, and runs the shared extractor on
each. Unlike Gmail/Krisp, Workflowy has no explicit subject — subjects
are inferred by name match against the people/ directory.

The Workflowy REST export is monkeypatched in tests; the name-matcher
and orchestrator are exercised directly.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"wfm_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def lib():
    return _load_script("workflowy_facts_mine_lib.py")


@pytest.fixture
def miner():
    return _load_script("workflowy-facts-mine.py")


# ─── Name → slug index ───────────────────────────────────────────────


def _write_person(people_dir: Path, slug: str, name: str, **extra) -> Path:
    lines = [
        f"# {name}",
        "",
        f"- **slug:** {slug}",
        f"- **full_name:** {name}",
    ]
    for k, v in extra.items():
        lines.append(f"- **{k}:** {v}")
    path = people_dir / f"{slug}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_build_name_to_slug_includes_full_name_and_stem(lib, tmp_path):
    people = tmp_path / "people"
    people.mkdir()
    _write_person(people, "sarah-chen", "Sarah Chen")
    _write_person(people, "mike-jones", "Mike Jones")
    idx = lib.build_name_to_slug_index(people)
    # Both full_name and slug should resolve
    assert idx.get("sarah chen") == "sarah-chen"
    assert idx.get("mike jones") == "mike-jones"


def test_build_name_to_slug_skips_underscore_files(lib, tmp_path):
    people = tmp_path / "people"
    people.mkdir()
    _write_person(people, "sarah-chen", "Sarah Chen")
    (people / "_template.md").write_text(
        "- **slug:** template\n- **full_name:** Test\n", encoding="utf-8"
    )
    idx = lib.build_name_to_slug_index(people)
    assert "template" not in idx.values()


# ─── find_mentioned_slugs ────────────────────────────────────────────


def test_find_mentioned_slugs_matches_whole_word(lib):
    idx = {"sarah chen": "sarah-chen", "mike jones": "mike-jones"}
    mentions = lib.find_mentioned_slugs(
        "Had coffee with Sarah Chen today — she's hiring.", idx
    )
    assert mentions == {"sarah-chen"}


def test_find_mentioned_slugs_matches_multiple(lib):
    idx = {"sarah chen": "sarah-chen", "mike jones": "mike-jones"}
    mentions = lib.find_mentioned_slugs(
        "Mike Jones and Sarah Chen both recommended the book.", idx
    )
    assert mentions == {"sarah-chen", "mike-jones"}


def test_find_mentioned_slugs_case_insensitive(lib):
    idx = {"sarah chen": "sarah-chen"}
    mentions = lib.find_mentioned_slugs("sarah chen called", idx)
    assert mentions == {"sarah-chen"}


def test_find_mentioned_slugs_requires_whole_word(lib):
    """'Sarah' alone shouldn't match 'Sarah Chen' index — reduces
    false positives from common first names."""
    idx = {"sarah chen": "sarah-chen"}
    mentions = lib.find_mentioned_slugs("sarahcheng was here", idx)
    assert mentions == set()


def test_find_mentioned_slugs_empty_text(lib):
    idx = {"sarah chen": "sarah-chen"}
    assert lib.find_mentioned_slugs("", idx) == set()
    assert lib.find_mentioned_slugs(None, idx) == set()


# ─── Orchestrator ────────────────────────────────────────────────────


def _fake_extract_returns(facts: list[dict]):
    def _fn(**kw):
        return facts
    return _fn


def test_run_skips_nodes_without_mentions(miner, tmp_path, monkeypatch):
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)
    _write_person(people_dir, "sarah-chen", "Sarah Chen")

    nodes = [
        {"id": "n1", "name": "Buy groceries"},
        {"id": "n2", "name": "Call plumber"},
    ]
    monkeypatch.setattr(miner, "_fetch_nodes", lambda api_key, max_nodes: nodes)

    extract_calls = [0]
    def tracker(**kw):
        extract_calls[0] += 1
        return []
    monkeypatch.setattr(miner, "extract_facts_from_text", tracker)

    result = miner.run(
        api_key="tok",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_nodes=100,
        commit=True,
    )
    assert result["nodes_scanned"] == 2
    assert result["nodes_skipped_no_mentions"] == 2
    assert extract_calls[0] == 0


def test_run_writes_fact_from_mention(miner, tmp_path, monkeypatch):
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)
    _write_person(people_dir, "sarah-chen", "Sarah Chen")

    nodes = [
        {"id": "node-abc", "name": "Sarah Chen is fundraising Series B"},
    ]
    monkeypatch.setattr(miner, "_fetch_nodes", lambda api_key, max_nodes: nodes)

    monkeypatch.setattr(miner, "extract_facts_from_text", _fake_extract_returns([
        {
            "subject": "sarah-chen",
            "category": "event",
            "content": "Fundraising Series B",
            "confidence": 0.75,
            "audience_scope": ["professional"],
            "source_detail": "workflowy:node-abc",
            "idempotency_key": "workflowy-node-abc-sarah-chen-xyz",
            "needs_review": False,
            "reason": "stated in note",
        }
    ]))

    result = miner.run(
        api_key="tok",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_nodes=100,
        commit=True,
    )
    assert result["facts_minted"] == 1
    assert list(facts_dir.glob("*.md"))


def test_run_dry_run_does_not_write(miner, tmp_path, monkeypatch):
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)
    _write_person(people_dir, "sarah-chen", "Sarah Chen")

    nodes = [{"id": "node-d", "name": "Sarah Chen is hiring"}]
    monkeypatch.setattr(miner, "_fetch_nodes", lambda api_key, max_nodes: nodes)
    monkeypatch.setattr(miner, "extract_facts_from_text", _fake_extract_returns([
        {
            "subject": "sarah-chen",
            "category": "identity",
            "content": "x",
            "confidence": 0.9,
            "audience_scope": ["professional"],
            "source_detail": "workflowy:node-d",
            "idempotency_key": "workflowy-node-d-sarah-chen-xyz",
            "needs_review": False,
            "reason": "y",
        }
    ]))

    result = miner.run(
        api_key="tok",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_nodes=100,
        commit=False,
    )
    assert result["facts_minted"] == 1
    assert not facts_dir.exists() or not any(facts_dir.glob("*.md"))


def test_run_no_api_key_returns_degraded(miner, tmp_path, monkeypatch):
    """When the Workflowy API key is missing, the cron should report
    status=degraded rather than crashing or silently succeeding."""
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)

    result = miner.run(
        api_key="",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_nodes=100,
        commit=True,
    )
    assert result["status"] == "degraded"
    assert "api_key" in result.get("reason", "").lower()


def test_run_emits_script_contract_envelope(miner, tmp_path, monkeypatch):
    facts_dir = tmp_path / "brain" / "facts"
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)

    monkeypatch.setattr(miner, "_fetch_nodes", lambda api_key, max_nodes: [])

    result = miner.run(
        api_key="tok",
        people_dir=people_dir,
        facts_dir=facts_dir,
        cursor_path=tmp_path / "cursor.json",
        max_nodes=100,
        commit=True,
    )
    for key in (
        "status", "nodes_scanned", "nodes_skipped_no_mentions",
        "facts_minted", "skipped_dup", "facts_flagged_low_conf",
    ):
        assert key in result, f"missing key: {key}"


def test_main_always_exits_zero(miner, monkeypatch, capsys):
    def boom(**kw):
        raise RuntimeError("simulated")
    monkeypatch.setattr(miner, "run", boom)
    rc = miner.main([])
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert envelope["status"] == "error"
