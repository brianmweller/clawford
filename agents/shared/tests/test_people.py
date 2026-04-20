"""people — `recent_observations` append helper.

Context: miners write to `brain/facts/`, which is the durable store.
Structured people cards (`brain/people/<slug>.md`) are a human-
readable summary — 387 files today, each a short YAML-ish block
with slug, circles, relationship, social_distance, notes. The cards
were drifting apart from the facts stream because nothing wrote
back to them. This helper appends a one-line bullet to a
`## Recent observations` section — additive surfacing, not a
structured-field upsert. Keeps the card in sync with new facts
without duplicating storage.

Append-only with a 10-entry trim: the card stays scannable even
after hundreds of observations accumulate.
"""
from __future__ import annotations

import sys
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import people  # type: ignore


def _seed_person(people_dir: Path, slug: str, body: str = "") -> Path:
    people_dir.mkdir(parents=True, exist_ok=True)
    path = people_dir / f"{slug}.md"
    default_body = (
        f"# {slug.replace('-', ' ').title()}\n\n"
        f"- **slug:** {slug}\n"
        f"- **circles:** professional-outer\n"
        f"- **relationship:** colleague\n"
    )
    path.write_text(body or default_body, encoding="utf-8")
    return path


def test_append_observation_creates_section_on_first_entry(tmp_path: Path) -> None:
    people_dir = tmp_path / "people"
    path = _seed_person(people_dir, "sarah-chen")
    result = people.append_observation(
        people_dir=people_dir,
        slug="sarah-chen",
        content="Moving to Austin in June",
        source="gmail:abc123",
        timestamp="2026-04-20",
    )
    assert result["status"] == "appended"
    text = path.read_text(encoding="utf-8")
    assert "## Recent observations" in text
    assert "Moving to Austin in June" in text
    assert "2026-04-20" in text
    assert "gmail:abc123" in text


def test_append_observation_appends_to_existing_section(tmp_path: Path) -> None:
    """A second call adds a new bullet without re-creating the header."""
    people_dir = tmp_path / "people"
    _seed_person(people_dir, "sarah-chen")
    people.append_observation(
        people_dir=people_dir, slug="sarah-chen",
        content="First obs", source="gmail:1", timestamp="2026-04-19",
    )
    people.append_observation(
        people_dir=people_dir, slug="sarah-chen",
        content="Second obs", source="gmail:2", timestamp="2026-04-20",
    )
    text = (people_dir / "sarah-chen.md").read_text(encoding="utf-8")
    assert text.count("## Recent observations") == 1
    assert "First obs" in text
    assert "Second obs" in text


def test_append_observation_trims_to_ten_most_recent(tmp_path: Path) -> None:
    """Cards stay scannable: keep the 10 most recent observations,
    drop older ones. Order preserved (oldest first within the
    retained window)."""
    people_dir = tmp_path / "people"
    _seed_person(people_dir, "sarah-chen")
    for i in range(15):
        people.append_observation(
            people_dir=people_dir, slug="sarah-chen",
            content=f"Obs {i}", source=f"gmail:{i}",
            timestamp=f"2026-04-{i + 1:02d}",
        )
    text = (people_dir / "sarah-chen.md").read_text(encoding="utf-8")
    # Obs 0..4 (first 5) should be trimmed; Obs 5..14 retained. Match
    # the full bullet token with a trailing space so "Obs 1" doesn't
    # substring-match "Obs 10".
    for i in range(5):
        assert f"Obs {i} " not in text, f"Obs {i} should have been trimmed"
    for i in range(5, 15):
        assert f"Obs {i} " in text, f"Obs {i} should be retained"


def test_append_observation_no_person_file_degraded(tmp_path: Path) -> None:
    """Miner encounters a subject that has no people card yet — we
    don't want to auto-create a card from a single observation (those
    are the low-signal entries that fact-extractor should be
    filtering anyway). Return degraded silently."""
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    result = people.append_observation(
        people_dir=people_dir, slug="ghost-person",
        content="X", source="gmail:0", timestamp="2026-04-20",
    )
    assert result["status"] == "skipped"
    assert result.get("reason") == "person file absent"
    assert not (people_dir / "ghost-person.md").exists()


def test_append_observation_preserves_existing_body(tmp_path: Path) -> None:
    """The existing card body must not be mangled when the section
    is added. Fields above the new section stay in place."""
    people_dir = tmp_path / "people"
    body = (
        "# Sarah Chen\n\n"
        "- **slug:** sarah-chen\n"
        "- **circles:** professional-outer\n"
        "- **relationship:** vendor\n"
        "- **notes:** Long-time partner at Acme.\n"
    )
    _seed_person(people_dir, "sarah-chen", body=body)
    people.append_observation(
        people_dir=people_dir, slug="sarah-chen",
        content="Test", source="x:1", timestamp="2026-04-20",
    )
    text = (people_dir / "sarah-chen.md").read_text(encoding="utf-8")
    assert "- **slug:** sarah-chen" in text
    assert "- **circles:** professional-outer" in text
    assert "- **relationship:** vendor" in text
    assert "- **notes:** Long-time partner at Acme." in text


def test_append_observation_appends_below_existing_section_with_other_content(
    tmp_path: Path,
) -> None:
    """If the card has ## Recent observations AND other H2 sections
    after it (unlikely but possible for hand-edited cards), append
    only adds to the Recent observations section, leaving other
    sections in place."""
    people_dir = tmp_path / "people"
    body = (
        "# Sarah Chen\n\n"
        "- **slug:** sarah-chen\n\n"
        "## Recent observations\n"
        "- 2026-04-15 · Previous obs (source: gmail:old)\n\n"
        "## Notes archive\n"
        "Hand-edited by the operator, keep intact.\n"
    )
    _seed_person(people_dir, "sarah-chen", body=body)
    people.append_observation(
        people_dir=people_dir, slug="sarah-chen",
        content="New obs", source="gmail:new", timestamp="2026-04-20",
    )
    text = (people_dir / "sarah-chen.md").read_text(encoding="utf-8")
    assert "Previous obs" in text
    assert "New obs" in text
    # The "Notes archive" section must be preserved intact.
    assert "## Notes archive" in text
    assert "Hand-edited by the operator, keep intact." in text


def test_append_observation_writes_atomically(tmp_path: Path, monkeypatch) -> None:
    """Atomic tmp+replace: a crash mid-write shouldn't leave a
    truncated card. Verify by checking no .tmp file is left behind
    after success."""
    people_dir = tmp_path / "people"
    _seed_person(people_dir, "sarah-chen")
    people.append_observation(
        people_dir=people_dir, slug="sarah-chen",
        content="X", source="s:1", timestamp="2026-04-20",
    )
    # No .tmp leftover.
    assert not (people_dir / "sarah-chen.md.tmp").exists()
