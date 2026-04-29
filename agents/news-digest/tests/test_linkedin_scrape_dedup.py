"""Tests for linkedin-scrape.py::_dedup_notifications.

Pinpoints the every-other-day silent loss of the profile-view rollup:
the legacy main()-inline dedup hashed every notification's headline
text against linkedin-seen.json. profile_view headlines repeat across
days whenever LinkedIn surfaces the same top viewer ("Edith Ho viewed
your profile. See all views."), so the hash matched and the entire
enriched payload (detail_names) was discarded before
_build_profile_view_summary ever saw it.

the operator, 2026-04-28: 'I'm not getting my summarized LinkedIn profile
views. Again!'
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _item_hash(text: str) -> str:
    """Mirror of linkedin-scrape.py::item_hash. Local copy lets tests
    seed the seen set without first importing the module under test."""
    return hashlib.md5(text.strip()[:200].encode()).hexdigest()[:16]


def _load():
    path = SCRIPTS_DIR / "linkedin-scrape.py"
    spec = importlib.util.spec_from_file_location("linkedin_scrape", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


def test_profile_view_bypasses_seen_dedup(mod):
    """Even when the headline hash is already in `seen`, the
    profile_view notification with detail_names must pass through."""
    text = "Edith Ho viewed your profile. See all views."
    seen = {_item_hash(text): "2026-04-27T10:33:00+00:00"}
    notif = {
        "type": "profile_view",
        "text": text,
        "time_ago": "1h",
        "detail_names": [
            {"name": "Edith Ho", "time_ago": "12h ago",
             "title": "", "is_anonymous": False},
            {"name": "Aanchal Aggarwal", "time_ago": "22h ago",
             "title": "", "is_anonymous": False},
        ],
    }

    fresh, added = mod._dedup_notifications([notif], seen)

    assert len(fresh) == 1, (
        "profile_view notification was filtered despite carrying the "
        "authoritative detail_names list"
    )
    assert fresh[0].get("type") == "profile_view"
    assert len(fresh[0].get("detail_names", [])) == 2


def test_non_profile_view_still_deduped(mod):
    """Reactions, mentions, comments — text-hash dedup remains correct
    for these. Their headline text uniquely identifies the event."""
    text = "Pat Smith reacted to your post."
    seen = {_item_hash(text): "2026-04-27T10:33:00+00:00"}
    notif = {"type": "reaction", "text": text, "time_ago": "2h"}

    fresh, added = mod._dedup_notifications([notif], seen)

    assert fresh == [], (
        "non-profile_view notification with seen hash should be filtered"
    )
    assert added == {}, "filtered notif must not enter the seen set again"


def test_profile_view_does_not_add_to_seen(mod):
    """profile_view bypasses the hash check AND must not add the
    headline to `added`. Otherwise the rollup occupies the 500-entry
    seen-budget and pushes legitimate post hashes out, cascading
    unrelated regressions."""
    notif = {
        "type": "profile_view",
        "text": "Mike Develin and 1 other person viewed your profile. See all views.",
        "time_ago": "30m",
        "detail_names": [
            {"name": "Mike Develin", "time_ago": "30m ago",
             "title": "", "is_anonymous": False},
        ],
    }

    fresh, added = mod._dedup_notifications([notif], {})

    assert len(fresh) == 1
    assert added == {}, (
        "profile_view notif must not pollute the seen set; got: " + repr(added)
    )


def test_mixed_batch_dedups_only_non_profile_view(mod):
    """Realistic batch: one profile_view (must pass), one fresh
    reaction (must pass and add hash), one stale reaction (must drop)."""
    fresh_reaction = "Sam reacted to your post about LLMs."
    stale_reaction = "Old Bob commented on your update."
    seen = {_item_hash(stale_reaction): "2026-04-27T10:33:00+00:00"}

    notifs = [
        {"type": "profile_view",
         "text": "Edith Ho viewed your profile. See all views.",
         "time_ago": "1h",
         "detail_names": [{"name": "Edith Ho", "time_ago": "1h ago"}]},
        {"type": "reaction", "text": fresh_reaction, "time_ago": "30m"},
        {"type": "comment", "text": stale_reaction, "time_ago": "2h"},
    ]

    fresh, added = mod._dedup_notifications(notifs, seen)

    types_kept = sorted(n.get("type") for n in fresh)
    assert types_kept == ["profile_view", "reaction"], types_kept
    assert list(added.keys()) == [_item_hash(fresh_reaction)]


def test_timestamp_attached_to_kept_notifications(mod):
    """The legacy code attaches a parsed `timestamp` to every fresh
    notification. The helper must preserve that side effect so the
    downstream artice builder doesn't NPE on `n["timestamp"]`."""
    notif = {"type": "profile_view",
             "text": "Someone at Google viewed your profile.",
             "time_ago": "2h",
             "detail_names": [{"name": "X", "time_ago": "2h ago"}]}

    fresh, _ = mod._dedup_notifications([notif], {})

    assert "timestamp" in fresh[0], (
        "kept notification must have a parsed timestamp attached"
    )
