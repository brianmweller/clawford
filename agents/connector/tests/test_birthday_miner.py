"""Tests for birthday-miner.py pure-function helpers.

The mining script itself shells out to Google Calendar; these tests
cover the parsing + person-matching logic that's pure and unit-testable.
The GCal fetch is mocked at the boundary (build_google_services).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(AGENT_DIR))


def _load_miner():
    """Import the birthday-miner script by path (name contains hyphen)."""
    path = AGENT_DIR / "scripts" / "birthday-miner.py"
    spec = importlib.util.spec_from_file_location("birthday_miner", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def miner():
    return _load_miner()


# ---------------------------------------------------------------------------
# extract_name_from_title — the fuzzy recipient-name parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Priya's Birthday", "Priya"),
        ("Sarah's birthday 🎂", "Sarah"),
        ("Mike Chen's Birthday", "Mike Chen"),
        ("Mom's B-day", "Mom"),
        ("Dad bday", "Dad"),
        ("Birthday: Alex", "Alex"),
        ("🎂 Jane", "Jane"),
    ],
)
def test_extract_name_from_title_happy_paths(miner, title, expected):
    assert miner.extract_name_from_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Random meeting",
        "Birthday",  # no person attached
    ],
)
def test_extract_name_from_title_no_name(miner, title):
    assert miner.extract_name_from_title(title) is None


# ---------------------------------------------------------------------------
# process_events — orchestration (GCal output → upsert_fact calls)
# ---------------------------------------------------------------------------


def test_process_events_upserts_for_matched_persons(miner, tmp_path, monkeypatch):
    # Sandbox the brain so person lookups hit our test fixtures
    brain_root = tmp_path / "brain"
    (brain_root / "people").mkdir(parents=True)
    (brain_root / "people" / "priya-rivera.md").write_text(
        "# Priya Rivera\n\n- **slug:** priya-rivera\n- **circles:** family-inner\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))

    for mod in list(sys.modules):
        if mod in ("brain",):
            del sys.modules[mod]

    events = [
        {
            "summary": "Priya's Birthday",
            "start": {"date": "2026-09-15"},
        },
        {
            "summary": "Random meeting",
            "start": {"dateTime": "2026-09-15T10:00:00-07:00"},
        },
        {
            "summary": "Unknown Person's Birthday",
            "start": {"date": "2026-12-01"},
        },
    ]

    facts_dir = brain_root / "facts"
    summary = miner.process_events(events, facts_dir=facts_dir)
    assert summary["matched"] == 1
    assert summary["updated"] == 1
    assert summary["scanned"] == 3
    # Priya's fact landed. Facts are filed under recorded_at's month
    # (when the fact was written), not the birthday's month — a fact
    # about a September birthday recorded in April lives in 2026-04.md.
    month_files = list(facts_dir.glob("*.md"))
    assert len(month_files) == 1
    contents = month_files[0].read_text(encoding="utf-8")
    assert "priya-rivera" in contents
    assert "2026-09-15" in contents


def test_process_events_is_idempotent(miner, tmp_path, monkeypatch):
    brain_root = tmp_path / "brain"
    (brain_root / "people").mkdir(parents=True)
    (brain_root / "people" / "sarah-chen.md").write_text(
        "# Sarah Chen\n\n- **slug:** sarah-chen\n- **circles:** friends\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))

    for mod in list(sys.modules):
        if mod in ("brain",):
            del sys.modules[mod]

    events = [{"summary": "Sarah Chen's Birthday", "start": {"date": "2026-05-22"}}]

    facts_dir = brain_root / "facts"
    s1 = miner.process_events(events, facts_dir=facts_dir)
    s2 = miner.process_events(events, facts_dir=facts_dir)
    assert s1["updated"] == 1
    assert s2["updated"] == 0  # second pass — no new write
    assert s2["skipped"] == 1


def test_process_events_skips_events_without_date(miner, tmp_path, monkeypatch):
    brain_root = tmp_path / "brain"
    (brain_root / "people").mkdir(parents=True)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))

    for mod in list(sys.modules):
        if mod in ("brain",):
            del sys.modules[mod]

    events = [
        {"summary": "Priya's Birthday"},  # no start
        {"summary": "Sarah's Birthday", "start": {}},  # empty start
    ]
    summary = miner.process_events(events, facts_dir=brain_root / "facts")
    assert summary["matched"] == 0
    assert summary["updated"] == 0
