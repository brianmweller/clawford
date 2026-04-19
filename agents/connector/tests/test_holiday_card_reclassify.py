"""Tests for agents/connector/scripts/holiday-card-reclassify.py.

TDD: tests land before implementation. Exercises pin-matching against
a tmp people directory and asserts upsert semantics (merge, create,
no-op, idempotent).

See plan: C:\\Users\\operator\\.claude\\plans\\to-be-clear-the-gentle-swan.md
"""
from __future__ import annotations

import csv
import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "holiday-card-reclassify.py"
)


@pytest.fixture
def rc():
    spec = importlib.util.spec_from_file_location("hc_reclassify", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def people_dir(tmp_path):
    d = tmp_path / "people"
    d.mkdir()
    return d


def _write_person(dir_: Path, slug: str, *, email="—", circles="professional-outer", alt_emails="", name=None):
    name = name or " ".join(p.capitalize() for p in slug.replace("-", " ").split())
    content = (
        f"# {name}\n\n"
        f"- **slug:** {slug}\n"
        f"- **circles:** {circles}\n"
        f"- **relationship:** colleague\n"
        f"- **relationship_type:** colleague\n"
        f"- **preferred_channel:** iMessage\n"
        f"- **email:** {email}\n"
        f"- **alt_emails:** {alt_emails}\n"
        f"- **phone:** —\n"
        f"- **last_interaction:** 2026-02-01\n"
    )
    (dir_ / f"{slug}.md").write_text(content, encoding="utf-8")


def _write_pins(tmp_path: Path, rows: list[dict]) -> Path:
    csv_path = tmp_path / "holiday-card-pins.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "email"])
        w.writeheader()
        w.writerows(rows)
    return csv_path


def _read_circles(dir_: Path, slug: str) -> str:
    prefix = "- **circles:**"
    for line in (dir_ / f"{slug}.md").read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


# ── Matching ──────────────────────────────────────────────────


def test_match_by_email_exact(rc, people_dir, tmp_path):
    _write_person(people_dir, "drew-branden", email="drew.branden@example.com")
    pins = _write_pins(tmp_path, [{"name": "Drew Branden", "email": "drew.branden@example.com"}])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert report["matched_by_email"] == 1
    assert _read_circles(people_dir, "drew-branden") == "professional-outer, professional-inner"


def test_match_by_email_case_insensitive(rc, people_dir, tmp_path):
    _write_person(people_dir, "drew-branden", email="Drew.Branden@EXAMPLE.COM")
    pins = _write_pins(tmp_path, [{"name": "Drew Branden", "email": "drew.branden@example.com"}])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert report["matched_by_email"] == 1


def test_match_by_alt_email(rc, people_dir, tmp_path):
    _write_person(people_dir, "ellie-mertz", email="ellie@example.com", alt_emails="ellie.mertz@example.com")
    pins = _write_pins(tmp_path, [{"name": "Ellie Mertz", "email": "ellie.mertz@example.com"}])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert report["matched_by_email"] == 1


def test_match_by_name_slug_when_emails_differ(rc, people_dir, tmp_path):
    # .md has personal gmail, CSV has airbnb — common mining-vs-holiday-card split.
    _write_person(people_dir, "dan-zylberglejd", email="danzylber@gmail.com")
    pins = _write_pins(tmp_path, [{"name": "Dan Zylberglejd", "email": "dan.zylberglejd@example.com"}])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert report["matched_by_name"] == 1
    assert "professional-inner" in _read_circles(people_dir, "dan-zylberglejd")


def test_duplicate_csv_rows_deduped(rc, people_dir, tmp_path):
    _write_person(people_dir, "huiji-gao", email="huiji.gao@example.com")
    pins = _write_pins(tmp_path, [
        {"name": "Huiji Gao", "email": "huiji.gao@example.com"},
        {"name": "Huiji Gao", "email": "huiji.gao@example.com"},
    ])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    # Duplicate collapses; one upsert counted.
    assert report["matched_by_email"] == 1
    assert report["duplicates_in_csv"] == 1


def test_ambiguous_name_match_skipped_with_warning(rc, people_dir, tmp_path):
    # Two people with slug starting "jason-smith" — both match the name slug.
    # For simplicity, simulate by using same exact slug match: actually must be
    # exactly-same slug collision which is impossible for files. Instead
    # test by giving two .md files with different slugs whose name fields
    # collide. We detect ambiguity by normalized-name collision.
    _write_person(people_dir, "jason-smith-a", email="jason@example.com", name="Jason Smith")
    _write_person(people_dir, "jason-smith-b", email="js@example.com", name="Jason Smith")
    pins = _write_pins(tmp_path, [{"name": "Jason Smith", "email": "jsmith@example.com"}])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert report["ambiguous"] == 1
    # Neither file should have been modified.
    assert _read_circles(people_dir, "jason-smith-a") == "professional-outer"
    assert _read_circles(people_dir, "jason-smith-b") == "professional-outer"


def test_name_with_roman_suffix_stripped(rc, people_dir, tmp_path):
    _write_person(people_dir, "casimir-ksiazek", email="ck@example.com", name="Casimir Ksiazek")
    pins = _write_pins(tmp_path, [{"name": "Casimir Ksiazek III", "email": "somewhere@example.com"}])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert report["matched_by_name"] == 1


# ── Stub creation ──────────────────────────────────────────────


def test_creates_stub_for_missing_person(rc, people_dir, tmp_path):
    pins = _write_pins(tmp_path, [{"name": "Brand New", "email": "brand.new@example.com"}])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert report["created"] == 1
    stub = people_dir / "brand-new.md"
    assert stub.exists()
    content = stub.read_text(encoding="utf-8")
    assert "- **circles:** professional-inner" in content
    assert "- **email:** brand.new@example.com" in content


def test_stub_slug_strips_roman_suffix(rc, people_dir, tmp_path):
    pins = _write_pins(tmp_path, [{"name": "Hank Smith III", "email": "hank@example.com"}])
    rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert (people_dir / "hank-smith.md").exists()


# ── Merge / no-op semantics ────────────────────────────────────


def test_merges_with_existing_non_outer_circle(rc, people_dir, tmp_path):
    _write_person(people_dir, "leanne-bradley", email="leanne.bradley@example.com", circles="friends-close")
    pins = _write_pins(tmp_path, [{"name": "Leanne Bradley", "email": "leanne.bradley@example.com"}])
    rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    circles = _read_circles(people_dir, "leanne-bradley")
    assert "friends-close" in circles
    assert "professional-inner" in circles


def test_noop_when_already_professional_inner(rc, people_dir, tmp_path):
    _write_person(people_dir, "charlie-farrell", email="charlie.farrell@example.com", circles="professional-inner")
    pins = _write_pins(tmp_path, [{"name": "Charlie Farrell", "email": "charlie.farrell@example.com"}])
    report = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert report["already_pinned"] == 1
    assert report["matched_by_email"] == 0
    assert _read_circles(people_dir, "charlie-farrell") == "professional-inner"


def test_idempotent_rerun(rc, people_dir, tmp_path):
    _write_person(people_dir, "drew-branden", email="drew.branden@example.com")
    pins = _write_pins(tmp_path, [{"name": "Drew Branden", "email": "drew.branden@example.com"}])
    r1 = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    r2 = rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    assert r1["matched_by_email"] == 1
    assert r2["matched_by_email"] == 0
    assert r2["already_pinned"] == 1


def test_dry_run_writes_nothing(rc, people_dir, tmp_path):
    _write_person(people_dir, "drew-branden", email="drew.branden@example.com")
    pins = _write_pins(tmp_path, [{"name": "Drew Branden", "email": "drew.branden@example.com"}])
    rc.run(pins_csv=pins, people_dir=people_dir, write=False)
    assert _read_circles(people_dir, "drew-branden") == "professional-outer"


def test_rewrites_only_circles_line(rc, people_dir, tmp_path):
    _write_person(people_dir, "drew-branden", email="drew.branden@example.com")
    original = (people_dir / "drew-branden.md").read_text(encoding="utf-8")
    pins = _write_pins(tmp_path, [{"name": "Drew Branden", "email": "drew.branden@example.com"}])
    rc.run(pins_csv=pins, people_dir=people_dir, write=True)
    after = (people_dir / "drew-branden.md").read_text(encoding="utf-8")
    # Only the circles line should differ.
    orig_lines = original.splitlines()
    after_lines = after.splitlines()
    assert len(orig_lines) == len(after_lines)
    diffs = [i for i, (a, b) in enumerate(zip(orig_lines, after_lines)) if a != b]
    assert len(diffs) == 1
    assert "circles" in orig_lines[diffs[0]]
