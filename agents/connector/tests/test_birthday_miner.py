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
        # Real-world titles from the operator's calendar (2026-04-19 bootstrap audit)
        ("Jeanette's birthday", "Jeanette"),
        ("Xiao Yu's birthday", "Xiao Yu"),
        ("Daniel's Birthday!", "Daniel"),
        # Phyllis — the rstrip("'s") bug ate the final 's' before
        ("Aunt Phyllis Birthday", "Aunt Phyllis"),
        ("Mama Yao's birthday!", "Mama Yao"),
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
        "Happy birthday!",  # generic greeting, no owner
        # To-do reminders that mention "birthday" but aren't birthday-date events
        "Get Dad birthday card",
        "Get Mom's birthday card",
        "Get Vivian birthday card",
        "Bring Magnatiles - Violet's Birthday Party",
        "Buy birthday present for Sarah",
        "Order birthday cake",
        "Remember Tom's birthday",
        # Joke / non-canonical entries with middle words between name and keyword
        "OliMom's fake birthday!",
    ],
)
def test_extract_name_from_title_filters_todos_and_generic(miner, title):
    assert miner.extract_name_from_title(title) is None


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Aunt Marcia's birthday", "Marcia"),
        ("Aunt Phyllis Birthday", "Phyllis"),
        ("Uncle Bob's birthday", "Bob"),
        ("Mama Yao's birthday!", "Yao"),
        ("Papa Joe bday", "Joe"),
        ("Grandma Ruth's birthday", "Ruth"),
        ("Grandpa Henry's B-day", "Henry"),
    ],
)
def test_strip_relational_prefix(miner, title, expected):
    """Relational titles resolve to the actual first name for slug lookup."""
    name = miner.extract_name_from_title(title)
    assert miner.strip_relational_prefix(name) == expected


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


# ---------------------------------------------------------------------------
# Aliases + manual entries (birthday-aliases.json)
# ---------------------------------------------------------------------------


def test_aliases_resolve_nickname_to_slug(miner, tmp_path, monkeypatch):
    """'Mom' in the calendar resolves to the person whose slug is in the
    aliases file — no fuzzy matching needed."""
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

    aliases = {"aliases": {"Mom": "priya-rivera"}}
    events = [{"summary": "Mom's Birthday!", "start": {"date": "2027-01-09"}}]

    summary = miner.process_events(
        events, facts_dir=brain_root / "facts", aliases_cfg=aliases,
    )
    assert summary["matched"] == 1
    assert summary["updated"] == 1


def test_aliases_take_precedence_over_slug_fallback(miner, tmp_path, monkeypatch):
    """If both an alias AND a matching person file exist, alias wins —
    lets the operator force-override ambiguous first-name lookups."""
    brain_root = tmp_path / "brain"
    (brain_root / "people").mkdir(parents=True)
    (brain_root / "people" / "emily-bruemmer.md").write_text(
        "# Emily Bruemmer\n\n- **slug:** emily-bruemmer\n- **circles:** friends\n",
        encoding="utf-8",
    )
    (brain_root / "people" / "emily-pastewka.md").write_text(
        "# Emily Pastewka\n\n- **slug:** emily-pastewka\n- **circles:** work\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))
    for mod in list(sys.modules):
        if mod in ("brain",):
            del sys.modules[mod]

    aliases = {"aliases": {"Emily": "emily-pastewka"}}
    events = [{"summary": "Emily's birthday", "start": {"date": "2026-06-15"}}]

    summary = miner.process_events(
        events, facts_dir=brain_root / "facts", aliases_cfg=aliases,
    )
    assert summary["matched"] == 1
    month_file = list((brain_root / "facts").glob("*.md"))[0]
    content = month_file.read_text(encoding="utf-8")
    assert "emily-pastewka" in content
    assert "emily-bruemmer" not in content


def test_manual_entries_are_upserted_directly(miner, tmp_path, monkeypatch):
    """manual_birthdays in the aliases file bypass GCal entirely — operator
    supplies slug + ISO date, miner writes the identity fact."""
    brain_root = tmp_path / "brain"
    (brain_root / "people").mkdir(parents=True)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))
    for mod in list(sys.modules):
        if mod in ("brain",):
            del sys.modules[mod]

    aliases = {
        "manual_birthdays": {
            "ravi-rivera": "1948-07-10",
            "priya-rivera": "1950-09-15",
        },
    }
    summary = miner.process_events(
        events=[], facts_dir=brain_root / "facts", aliases_cfg=aliases,
    )
    assert summary["updated"] == 2
    month_file = list((brain_root / "facts").glob("*.md"))[0]
    content = month_file.read_text(encoding="utf-8")
    assert "1948-07-10" in content
    assert "1950-09-15" in content


def test_manual_entry_wins_over_calendar_collision(miner, tmp_path, monkeypatch):
    """When manual and calendar both target the same slug, the operator-
    supplied date wins — calendar's next-recurrence year would otherwise
    overwrite real birth years."""
    brain_root = tmp_path / "brain"
    (brain_root / "people").mkdir(parents=True)
    (brain_root / "people" / "marcia-sokolanderson.md").write_text(
        "# Marcia Sokol-Anderson\n\n- **slug:** marcia-sokolanderson\n- **circles:** family-extended\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))
    for mod in list(sys.modules):
        if mod in ("brain",):
            del sys.modules[mod]

    # Calendar says 2026-07-27 (next recurrence); manual says 1954-07-27 (real year).
    aliases = {
        "manual_birthdays": {"marcia-sokolanderson": "1954-07-27"},
    }
    events = [{"summary": "Aunt Marcia's birthday", "start": {"date": "2026-07-27"}}]

    summary = miner.process_events(
        events, facts_dir=brain_root / "facts", aliases_cfg=aliases,
    )
    # Manual writes first (new), calendar collides and is skipped
    assert summary["updated"] == 1
    assert summary["skipped"] == 1
    month_file = list((brain_root / "facts").glob("*.md"))[0]
    content = month_file.read_text(encoding="utf-8")
    assert "1954-07-27" in content
    assert "2026-07-27" not in content


def test_manual_entries_are_idempotent(miner, tmp_path, monkeypatch):
    brain_root = tmp_path / "brain"
    (brain_root / "people").mkdir(parents=True)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(brain_root))
    for mod in list(sys.modules):
        if mod in ("brain",):
            del sys.modules[mod]

    aliases = {"manual_birthdays": {"ravi-rivera": "1948-07-10"}}
    s1 = miner.process_events(events=[], facts_dir=brain_root / "facts", aliases_cfg=aliases)
    s2 = miner.process_events(events=[], facts_dir=brain_root / "facts", aliases_cfg=aliases)
    assert s1["updated"] == 1
    assert s2["updated"] == 0
    assert s2["skipped"] == 1


def test_load_aliases_missing_file_returns_empty(miner, tmp_path):
    """Missing aliases.json is fine — returns empty cfg, miner proceeds
    with GCal-only matching."""
    cfg = miner.load_aliases_cfg(tmp_path / "does-not-exist.json")
    assert cfg == {"aliases": {}, "manual_birthdays": {}}
