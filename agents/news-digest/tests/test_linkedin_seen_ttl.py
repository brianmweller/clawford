"""Tests for linkedin-scrape.py::load_seen / save_seen — time-based
eviction of linkedin-seen.json.

The legacy implementation kept the last 500 hashes via
`sorted(hashes)[-500:]`, evicting by alphabetical hex value rather
than recency. A hash from a year ago survived if its hex sorted late;
a fresh hash from yesterday could be evicted if its hex sorted early.

New shape: `{hashes: {<hash>: <iso_seen_at>}, updated_at: iso}` with
a 7-day TTL applied on load. The 500 hard-cap stays as a runaway-write
safety net (oldest-first when it trips).

Backward-compat: legacy list-of-strings shape is admitted on load
with `now_iso` per entry — they self-expire 7 days later.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load():
    path = SCRIPTS_DIR / "linkedin-scrape.py"
    spec = importlib.util.spec_from_file_location("linkedin_scrape", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod_with_tmp_seen(tmp_path, monkeypatch):
    mod = _load()
    seen_file = tmp_path / "linkedin-seen.json"
    monkeypatch.setattr(mod, "SEEN_FILE", seen_file)
    return mod, seen_file


def test_legacy_list_shape_loads(mod_with_tmp_seen):
    """A pre-migration linkedin-seen.json — `{"hashes": [...]}` — must
    load as a dict, with each legacy hash assigned a fresh timestamp
    so the TTL clock starts now."""
    mod, seen_file = mod_with_tmp_seen
    seen_file.write_text(
        json.dumps({"hashes": ["abc123", "def456", "789xyz"]}),
        encoding="utf-8",
    )

    seen = mod.load_seen()

    assert isinstance(seen, dict), f"load_seen must return dict, got {type(seen)}"
    assert set(seen.keys()) == {"abc123", "def456", "789xyz"}
    # Each legacy entry must have a parseable ISO timestamp.
    for h, ts in seen.items():
        datetime.fromisoformat(ts)  # raises if malformed


def test_old_entries_evicted_on_load(mod_with_tmp_seen):
    """Entries older than the 7-day TTL must be dropped on load."""
    mod, seen_file = mod_with_tmp_seen
    now = datetime.now(timezone.utc)
    fresh_ts = (now - timedelta(days=1)).isoformat()
    stale_ts = (now - timedelta(days=8)).isoformat()
    seen_file.write_text(
        json.dumps({
            "hashes": {"fresh-hash": fresh_ts, "stale-hash": stale_ts},
        }),
        encoding="utf-8",
    )

    seen = mod.load_seen()

    assert "fresh-hash" in seen
    assert "stale-hash" not in seen, (
        "entry older than 7-day TTL should be evicted on load"
    )


def test_save_caps_at_500_oldest_first(mod_with_tmp_seen):
    """When the dict exceeds 500 entries, save_seen evicts oldest-
    first (by timestamp), keeping the 500 most recent. This is the
    runaway-write safety net; in steady state the TTL handles it.

    Use names whose alphabetical order is the INVERSE of insertion
    order so the legacy alphabetical-sort eviction can't pass this
    test by accident."""
    mod, seen_file = mod_with_tmp_seen
    base = datetime.now(timezone.utc) - timedelta(days=3)
    # 600 entries: oldest gets the lexicographically LARGEST name,
    # newest gets the SMALLEST. Legacy alphabetical sort would
    # therefore keep the OLDEST 500, exactly opposite the desired
    # behaviour.
    seen = {
        f"hash{599 - i:04d}": (base + timedelta(seconds=i)).isoformat()
        for i in range(600)
    }

    mod.save_seen(seen)
    reloaded = mod.load_seen()

    assert isinstance(reloaded, dict), (
        f"save+load must round-trip as dict; got {type(reloaded)}"
    )
    assert len(reloaded) == 500, (
        f"save_seen must enforce 500-entry cap; got {len(reloaded)}"
    )
    # The 100 oldest (by timestamp) must be evicted. They were inserted
    # at i=0..99, with names hash0599..hash0500.
    for i in range(100):
        old_name = f"hash{599 - i:04d}"
        assert old_name not in reloaded, (
            f"oldest-by-timestamp {old_name} should have been evicted; "
            "looks like alphabetical-sort eviction (legacy bug) is still active"
        )
    # The 500 newest must remain. They were inserted at i=100..599,
    # with names hash0499..hash0000.
    for i in range(100, 600):
        new_name = f"hash{599 - i:04d}"
        assert new_name in reloaded


def test_malformed_timestamp_treated_as_fresh(mod_with_tmp_seen):
    """A malformed ts on disk shouldn't drop the hash — best-effort
    continuity. The next save_seen call rewrites a clean ISO string."""
    mod, seen_file = mod_with_tmp_seen
    seen_file.write_text(
        json.dumps({
            "hashes": {"bad-ts-hash": "not-an-iso-string"},
        }),
        encoding="utf-8",
    )

    seen = mod.load_seen()

    assert isinstance(seen, dict), (
        f"load_seen must return dict, got {type(seen)}"
    )
    assert "bad-ts-hash" in seen, (
        "malformed timestamp must be treated as fresh, not silently dropped"
    )
    # Must surface a real ISO string after the load — not the corrupt one.
    ts = seen["bad-ts-hash"]
    datetime.fromisoformat(ts)  # raises if still malformed


def test_empty_seen_file_returns_empty_dict(mod_with_tmp_seen):
    """No file on disk → empty dict, no crash."""
    mod, _ = mod_with_tmp_seen
    seen = mod.load_seen()
    assert isinstance(seen, dict)
    assert len(seen) == 0


def test_corrupted_json_returns_empty_dict(mod_with_tmp_seen):
    """File exists but is unreadable JSON → empty dict, scrape proceeds
    treating everything as fresh (acceptable one-shot loss vs hard
    failure)."""
    mod, seen_file = mod_with_tmp_seen
    seen_file.write_text("{not valid json", encoding="utf-8")
    seen = mod.load_seen()
    assert isinstance(seen, dict)
    assert len(seen) == 0


def test_save_then_load_round_trip(mod_with_tmp_seen):
    """End-to-end: save a small dict, load it back, get the same data."""
    mod, _ = mod_with_tmp_seen
    now = datetime.now(timezone.utc)
    seen = {
        "hash-a": (now - timedelta(hours=1)).isoformat(),
        "hash-b": (now - timedelta(hours=2)).isoformat(),
    }

    mod.save_seen(seen)
    reloaded = mod.load_seen()

    assert reloaded == seen
