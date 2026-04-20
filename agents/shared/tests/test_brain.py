"""Tests for agents/shared/brain.py — shared-brain filesystem helpers.

Tests use tmp_path + env-var overrides to point the brain module at
a sandbox instead of touching the real Dropbox-synced or git-tracked
locations. Atomic writes, parent directory creation, and append-line
semantics are exercised here so the contract is pinned.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload_brain():
    for mod in list(sys.modules):
        if mod == "brain" or mod.startswith("brain."):
            del sys.modules[mod]
    import brain  # type: ignore
    return brain


@pytest.fixture
def sandboxed_brain(tmp_path, monkeypatch):
    """Point the brain module at tmp_path for both Dropbox and git roots."""
    dropbox_root = tmp_path / "dropbox-brain"
    git_root = tmp_path / "git-brain"
    dropbox_root.mkdir()
    git_root.mkdir()
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(dropbox_root))
    monkeypatch.setenv("CLAWFORD_BRAIN_GIT_ROOT", str(git_root))
    return {"dropbox": dropbox_root, "git": git_root}


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def test_dropbox_brain_root_from_env(sandboxed_brain):
    brain = _reload_brain()
    assert brain.dropbox_brain_root() == sandboxed_brain["dropbox"]


def test_dropbox_brain_root_default_is_expanduser(monkeypatch, tmp_path):
    """Without CLAWFORD_BRAIN_DROPBOX_ROOT, fall back to
    Path.home() / Dropbox / openclaw-backup (the convention used by
    every existing brain-aware script in the fleet)."""
    monkeypatch.delenv("CLAWFORD_BRAIN_DROPBOX_ROOT", raising=False)
    brain = _reload_brain()
    root = brain.dropbox_brain_root()
    assert root.name == "openclaw-backup"
    assert root.parent.name == "Dropbox"


def test_git_brain_root_from_env(sandboxed_brain):
    brain = _reload_brain()
    assert brain.git_brain_root() == sandboxed_brain["git"]


def test_git_brain_root_default_resolves_repo_ops_brain(monkeypatch):
    """Default git_brain_root is agents/shared/brain.py → ../../ops/brain."""
    monkeypatch.delenv("CLAWFORD_BRAIN_GIT_ROOT", raising=False)
    brain = _reload_brain()
    root = brain.git_brain_root()
    # Should end in ops/brain relative to the repo root
    parts = root.parts
    assert "ops" in parts
    assert parts[parts.index("ops") + 1] == "brain"


# ---------------------------------------------------------------------------
# Text read / write / append
# ---------------------------------------------------------------------------


def test_read_text_success(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["dropbox"] / "facts" / "life.md").parent.mkdir()
    (sandboxed_brain["dropbox"] / "facts" / "life.md").write_text("42", encoding="utf-8")

    assert brain.read_text("facts/life.md") == "42"


def test_read_text_raises_on_missing(sandboxed_brain):
    brain = _reload_brain()
    with pytest.raises(FileNotFoundError):
        brain.read_text("nope/missing.md")


def test_write_text_creates_parent_dirs(sandboxed_brain):
    brain = _reload_brain()
    brain.write_text("deep/nested/dir/file.md", "hello")
    assert (sandboxed_brain["dropbox"] / "deep" / "nested" / "dir" / "file.md").read_text() == "hello"


def test_write_text_overwrites_existing(sandboxed_brain):
    brain = _reload_brain()
    brain.write_text("foo.md", "first")
    brain.write_text("foo.md", "second")
    assert (sandboxed_brain["dropbox"] / "foo.md").read_text() == "second"


def test_append_text_creates_file_if_missing(sandboxed_brain):
    brain = _reload_brain()
    brain.append_text("queues/events.log", "event-1")
    assert (sandboxed_brain["dropbox"] / "queues" / "events.log").read_text() == "event-1\n"


def test_append_text_appends_to_existing(sandboxed_brain):
    brain = _reload_brain()
    brain.append_text("queues/events.log", "event-1")
    brain.append_text("queues/events.log", "event-2")
    content = (sandboxed_brain["dropbox"] / "queues" / "events.log").read_text()
    assert content == "event-1\nevent-2\n"


def test_append_text_preserves_existing_trailing_newline(sandboxed_brain):
    """If the caller passes a line that already has a trailing newline,
    don't add a second one."""
    brain = _reload_brain()
    brain.append_text("q.log", "line-with-newline\n")
    assert (sandboxed_brain["dropbox"] / "q.log").read_text() == "line-with-newline\n"


# ---------------------------------------------------------------------------
# JSON read / write (atomic)
# ---------------------------------------------------------------------------


def test_read_json_success(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["dropbox"] / "data.json").write_text('{"x": 1}', encoding="utf-8")

    assert brain.read_json("data.json") == {"x": 1}


def test_write_json_happy_path(sandboxed_brain):
    brain = _reload_brain()
    brain.write_json("out.json", {"a": 1, "b": [2, 3]})

    loaded = json.loads((sandboxed_brain["dropbox"] / "out.json").read_text())
    assert loaded == {"a": 1, "b": [2, 3]}


def test_write_json_is_atomic_no_tmp_leftover(sandboxed_brain):
    """Atomic write means: after success, only the target file exists,
    not a .tmp sibling. A crashed write should leave the target intact
    (not tested here — but we verify no tmp leakage on success)."""
    brain = _reload_brain()
    brain.write_json("state.json", {"version": 2})

    dropbox_files = list(sandboxed_brain["dropbox"].iterdir())
    tmp_files = [f for f in dropbox_files if f.suffix == ".tmp" or ".json.tmp" in f.name]
    assert tmp_files == []


def test_write_json_creates_parent_dirs(sandboxed_brain):
    brain = _reload_brain()
    brain.write_json("deep/nested/report.json", {"ok": True})
    assert (sandboxed_brain["dropbox"] / "deep" / "nested" / "report.json").exists()


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def test_read_fleet_health_uses_canonical_path(sandboxed_brain):
    """fleet-health.json lives at the dropbox brain root — not in a
    subdirectory. Matches existing convention in ops/scripts/fleet-health.py."""
    brain = _reload_brain()
    (sandboxed_brain["dropbox"] / "fleet-health.json").write_text(
        json.dumps({"agents": {}}), encoding="utf-8"
    )
    data = brain.read_fleet_health()
    assert data == {"agents": {}}


def test_write_fleet_health_uses_canonical_path(sandboxed_brain):
    brain = _reload_brain()
    brain.write_fleet_health({"agents": {"fix-it": {"status": "ok"}}})
    loaded = json.loads(
        (sandboxed_brain["dropbox"] / "fleet-health.json").read_text()
    )
    assert loaded["agents"]["fix-it"]["status"] == "ok"


def test_agent_status_path_returns_per_agent_dir(sandboxed_brain):
    brain = _reload_brain()
    path = brain.agent_status_path("news-digest")
    assert path == sandboxed_brain["dropbox"] / "agents" / "news-digest"


def test_agent_config_path_returns_per_file_path(sandboxed_brain):
    brain = _reload_brain()
    soul = brain.agent_config_path("shopping", "SOUL.md")
    assert soul == sandboxed_brain["dropbox"] / "agents" / "shopping" / "SOUL.md"
    mem = brain.agent_config_path("fix-it", "MEMORY.md")
    assert mem == sandboxed_brain["dropbox"] / "agents" / "fix-it" / "MEMORY.md"
    mf = brain.agent_config_path("connector", "manifest.json")
    assert mf == sandboxed_brain["dropbox"] / "agents" / "connector" / "manifest.json"


def test_agent_config_path_rejects_path_traversal(sandboxed_brain):
    """Defense against tool-supplied filename with ../ segments."""
    brain = _reload_brain()
    import pytest
    with pytest.raises(ValueError):
        brain.agent_config_path("shopping", "../../etc/passwd")
    with pytest.raises(ValueError):
        brain.agent_config_path("shopping", "sub/dir/SOUL.md")
    with pytest.raises(ValueError):
        brain.agent_config_path("../other", "SOUL.md")


# ---------------------------------------------------------------------------
# Git-side reads
# ---------------------------------------------------------------------------


def test_read_git_text_uses_git_root(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["git"] / "rules" / "core.md").parent.mkdir()
    (sandboxed_brain["git"] / "rules" / "core.md").write_text("# rules", encoding="utf-8")

    assert brain.read_git_text("rules/core.md") == "# rules"


def test_read_git_json_uses_git_root(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["git"] / "schemas.json").write_text('{"v": 1}', encoding="utf-8")

    assert brain.read_git_json("schemas.json") == {"v": 1}


# ---------------------------------------------------------------------------
# People helpers (get_person, list_persons, create_person_file)
# ---------------------------------------------------------------------------


def _write_person(root: Path, slug: str, name: str, circles: str, **extra) -> None:
    lines = [f"# {name}", "", f"- **slug:** {slug}", f"- **circles:** {circles}"]
    for k, v in extra.items():
        lines.append(f"- **{k}:** {v}")
    (root / "people").mkdir(exist_ok=True)
    (root / "people" / f"{slug}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_get_person_by_slug_returns_fields(sandboxed_brain):
    brain = _reload_brain()
    _write_person(
        sandboxed_brain["dropbox"], "priya-rivera", "Priya Rivera",
        "family-inner", email="priya@example.com", tone="warm",
    )

    p = brain.get_person("priya-rivera")
    assert p is not None
    assert p["slug"] == "priya-rivera"
    assert p["name"] == "Priya Rivera"
    assert p["fields"]["email"] == "priya@example.com"
    assert p["fields"]["circles"] == "family-inner"
    assert p["fields"]["tone"] == "warm"
    assert p["path"].endswith("priya-rivera.md")


def test_get_person_by_display_name_slugifies(sandboxed_brain):
    """Callers pass "Priya Rivera" — helper slugifies to priya-rivera."""
    brain = _reload_brain()
    _write_person(sandboxed_brain["dropbox"], "priya-rivera", "Priya Rivera", "family-inner")

    p = brain.get_person("Priya Rivera")
    assert p is not None
    assert p["slug"] == "priya-rivera"


def test_get_person_returns_none_on_miss(sandboxed_brain):
    brain = _reload_brain()
    (sandboxed_brain["dropbox"] / "people").mkdir()

    assert brain.get_person("nonexistent-person") is None


def test_get_person_first_name_fallback_single_match(sandboxed_brain):
    """'Priya' should resolve to priya-rivera when that's the only match."""
    brain = _reload_brain()
    _write_person(sandboxed_brain["dropbox"], "priya-rivera", "Priya Rivera", "family-inner")
    _write_person(sandboxed_brain["dropbox"], "mike-chen", "Mike Chen", "work")

    p = brain.get_person("Priya")
    assert p is not None
    assert p["slug"] == "priya-rivera"


def test_get_person_first_name_fallback_ambiguous_returns_none(sandboxed_brain):
    """Two Alices → ambiguous → None; caller must disambiguate."""
    brain = _reload_brain()
    _write_person(sandboxed_brain["dropbox"], "alice-johnson", "Alice Johnson", "work")
    _write_person(sandboxed_brain["dropbox"], "alice-wong", "Alice Wong", "friends")

    assert brain.get_person("Alice") is None


def test_list_persons_all_circles(sandboxed_brain):
    brain = _reload_brain()
    _write_person(sandboxed_brain["dropbox"], "a-one", "A One", "family-inner")
    _write_person(sandboxed_brain["dropbox"], "b-two", "B Two", "work")
    _write_person(sandboxed_brain["dropbox"], "c-three", "C Three", "family-extended")

    persons = brain.list_persons()
    slugs = {p["slug"] for p in persons}
    assert slugs == {"a-one", "b-two", "c-three"}


def test_list_persons_filtered_by_circle(sandboxed_brain):
    brain = _reload_brain()
    _write_person(sandboxed_brain["dropbox"], "a-one", "A One", "family-inner")
    _write_person(sandboxed_brain["dropbox"], "b-two", "B Two", "work, friends")
    _write_person(sandboxed_brain["dropbox"], "c-three", "C Three", "family-inner, close")

    inner = brain.list_persons(circle="family-inner")
    slugs = {p["slug"] for p in inner}
    assert slugs == {"a-one", "c-three"}


def test_list_persons_empty_dir(sandboxed_brain):
    brain = _reload_brain()
    assert brain.list_persons() == []


def test_create_person_file_writes_frontmatter(sandboxed_brain):
    brain = _reload_brain()
    result = brain.create_person_file(
        "Sarah Example", "friends", tone="warm", email="sarah@example.com",
    )
    assert result["slug"] == "sarah-example"
    path = sandboxed_brain["dropbox"] / "people" / "sarah-example.md"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "# Sarah Example" in content
    assert "- **slug:** sarah-example" in content
    assert "- **circles:** friends" in content
    assert "- **tone:** warm" in content
    assert "- **email:** sarah@example.com" in content


def test_create_person_file_raises_on_duplicate(sandboxed_brain):
    brain = _reload_brain()
    brain.create_person_file("Sarah Example", "friends")
    with pytest.raises(FileExistsError):
        brain.create_person_file("Sarah Example", "friends")


# ---------------------------------------------------------------------------
# Inbox notes
# ---------------------------------------------------------------------------


def test_append_inbox_note_creates_file_with_entry(sandboxed_brain):
    brain = _reload_brain()
    result = brain.append_inbox_note("connector", "call mom next week")
    path = sandboxed_brain["dropbox"] / "notes" / "inbox.md"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "- **content:** call mom next week" in content
    assert "- **agent:** connector" in content
    assert "- **triaged:** false" in content
    assert "- **id:** connector-" in content
    assert result["id"].startswith("connector-")


def test_append_inbox_note_appends_multiple_entries(sandboxed_brain):
    brain = _reload_brain()
    r1 = brain.append_inbox_note("connector", "first note")
    r2 = brain.append_inbox_note("connector", "second note")
    assert r1["id"] != r2["id"]
    content = (sandboxed_brain["dropbox"] / "notes" / "inbox.md").read_text(encoding="utf-8")
    assert "first note" in content
    assert "second note" in content
    # Entries are separated by --- dividers
    assert content.count("---") >= 1


def test_append_inbox_note_respects_triaged_flag(sandboxed_brain):
    brain = _reload_brain()
    brain.append_inbox_note("connector", "already triaged", triaged=True)
    content = (sandboxed_brain["dropbox"] / "notes" / "inbox.md").read_text(encoding="utf-8")
    assert "- **triaged:** true" in content
