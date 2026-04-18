"""Tests for connector/scripts/daily-refresh.py.

daily-refresh.py is the incremental last_interaction updater cron.
It pulls fresh signal from three sources on VPS and rewrites
`last_interaction:` lines in ~/Dropbox/openclaw-backup/people/*.md
when newer dates are discovered:

  1. Gmail metadata (newer_than:14d) — From/To/Cc headers + Date
  2. Google Calendar events in [-14d, +14d] — attendee emails
     - Past events: stamp last_interaction
     - Future events: also write upcoming-meetings.json for people-scan
  3. Meetings-coach Krisp pending-debrief JSONs — but ONLY transcripts
     where len(attendees) <= 6, to avoid 40-person all-hands inflating
     everyone's "caught up" state.

The tests drive the pure helpers (_parse_field, build_email_index,
merge_signals, update_last_interaction, _load_krisp_debriefs,
_collect_upcoming_meetings) without any network calls. The Gmail/GCal
network layer is exercised through a monkeypatched build() -> fake
service.

Run: cd agents/connector && python3 -m pytest tests/test_daily_refresh.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"conn_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def dr():
    return _load_script("daily-refresh.py")


# ── _parse_field ─────────────────────────────────────────────────────


def test_parse_field_reads_standard_template(dr):
    text = (
        "# Dan Zylberglejd\n\n"
        "- **slug:** dan-zylberglejd\n"
        "- **email:** danzylber@gmail.com\n"
        "- **last_interaction:** 2026-02-11\n"
    )
    assert dr._parse_field(text, "email") == "danzylber@gmail.com"
    assert dr._parse_field(text, "last_interaction") == "2026-02-11"
    assert dr._parse_field(text, "slug") == "dan-zylberglejd"


def test_parse_field_returns_none_for_em_dash_and_missing(dr):
    text = "- **email:** —\n- **slug:** x\n"
    assert dr._parse_field(text, "email") is None
    assert dr._parse_field(text, "phone") is None


def test_parse_field_tolerates_legacy_format(dr):
    """Older files used `- **key**: value` (colon outside bold)."""
    text = "- **email**: foo@bar.com\n"
    assert dr._parse_field(text, "email") == "foo@bar.com"


# ── build_email_index ────────────────────────────────────────────────


def test_build_email_index_maps_emails_lowercased(tmp_path, dr):
    people = tmp_path / "people"
    people.mkdir()
    (people / "alice.md").write_text(
        "# Alice\n- **email:** Alice@Example.com\n- **last_interaction:** 2026-03-01\n",
        encoding="utf-8",
    )
    (people / "bob.md").write_text(
        "# Bob\n- **email:** bob@x.io\n- **last_interaction:** —\n",
        encoding="utf-8",
    )
    (people / "_template.md").write_text("# {name}\n- **email:** —\n", encoding="utf-8")
    (people / "noemail.md").write_text("# No Email\n- **email:** —\n", encoding="utf-8")

    idx = dr.build_email_index(people)
    assert set(idx.keys()) == {"alice@example.com", "bob@x.io"}
    assert idx["alice@example.com"][1] == "2026-03-01"
    assert idx["bob@x.io"][1] is None
    # Path is a real Path to the file
    assert idx["alice@example.com"][0].name == "alice.md"


def test_build_email_index_includes_alt_emails(tmp_path, dr):
    """A person with an `alt_emails:` field gets one index entry per
    email (comma-separated), all pointing at the same file. Used when
    someone has a work email in the primary `email:` slot but schedules
    personal meetings from a Gmail address, or vice versa."""
    people = tmp_path / "people"
    people.mkdir()
    (people / "andrew-patton.md").write_text(
        "# Andrew Patton\n"
        "- **email:** andrew.patton@duke.edu\n"
        "- **alt_emails:** pattonandrewj@gmail.com, a.patton@other.org\n"
        "- **last_interaction:** 2026-03-16\n",
        encoding="utf-8",
    )
    idx = dr.build_email_index(people)
    assert set(idx.keys()) == {
        "andrew.patton@duke.edu",
        "pattonandrewj@gmail.com",
        "a.patton@other.org",
    }
    # All three entries point at the same file, same last_interaction
    for addr in idx:
        assert idx[addr][0].name == "andrew-patton.md"
        assert idx[addr][1] == "2026-03-16"


def test_build_email_index_ignores_dash_alt_emails(tmp_path, dr):
    people = tmp_path / "people"
    people.mkdir()
    (people / "bob.md").write_text(
        "# Bob\n- **email:** bob@x.io\n- **alt_emails:** —\n",
        encoding="utf-8",
    )
    idx = dr.build_email_index(people)
    assert set(idx.keys()) == {"bob@x.io"}


# ── merge_signals ────────────────────────────────────────────────────


def test_merge_signals_takes_max_date_per_email(dr):
    a = {"x@y.com": "2026-03-01", "w@z.com": "2026-01-15"}
    b = {"x@y.com": "2026-04-10", "v@z.com": "2026-02-02"}
    c = {"w@z.com": "2026-04-12"}

    merged = dr.merge_signals(a, b, c)
    assert merged == {
        "x@y.com": "2026-04-10",
        "w@z.com": "2026-04-12",
        "v@z.com": "2026-02-02",
    }


def test_merge_signals_lowercases_and_skips_empty(dr):
    a = {"Foo@Bar.com": "2026-04-01", "empty@x.com": ""}
    merged = dr.merge_signals(a)
    assert merged == {"foo@bar.com": "2026-04-01"}


# ── update_last_interaction ──────────────────────────────────────────


def test_update_last_interaction_writes_newer_date(tmp_path, dr):
    fp = tmp_path / "alice.md"
    fp.write_text(
        "# Alice\n- **email:** a@x.com\n- **last_interaction:** 2026-02-01\n- **notes:** hi\n",
        encoding="utf-8",
    )
    assert dr.update_last_interaction(fp, "2026-04-10") is True
    text = fp.read_text(encoding="utf-8")
    assert "- **last_interaction:** 2026-04-10" in text
    assert "- **last_interaction:** 2026-02-01" not in text
    assert "- **notes:** hi" in text  # other lines untouched


def test_update_last_interaction_skips_older_date(tmp_path, dr):
    fp = tmp_path / "alice.md"
    original = "- **last_interaction:** 2026-04-10\n"
    fp.write_text(original, encoding="utf-8")
    assert dr.update_last_interaction(fp, "2026-03-01") is False
    assert fp.read_text(encoding="utf-8") == original


def test_update_last_interaction_skips_same_date(tmp_path, dr):
    fp = tmp_path / "alice.md"
    fp.write_text("- **last_interaction:** 2026-04-10\n", encoding="utf-8")
    assert dr.update_last_interaction(fp, "2026-04-10") is False


def test_update_last_interaction_appends_when_line_missing(tmp_path, dr):
    fp = tmp_path / "alice.md"
    fp.write_text("# Alice\n- **email:** a@x.com\n", encoding="utf-8")
    assert dr.update_last_interaction(fp, "2026-04-14") is True
    assert "- **last_interaction:** 2026-04-14" in fp.read_text(encoding="utf-8")


def test_update_last_interaction_writes_when_existing_is_dash(tmp_path, dr):
    fp = tmp_path / "alice.md"
    fp.write_text("- **last_interaction:** —\n", encoding="utf-8")
    assert dr.update_last_interaction(fp, "2026-04-14") is True
    assert "- **last_interaction:** 2026-04-14" in fp.read_text(encoding="utf-8")


# ── gmessages signals ───────────────────────────────────────────────


def test_normalize_phone_strips_everything_non_digit(dr):
    assert dr._normalize_phone("+1 (650) 537-9785") == "6505379785"
    assert dr._normalize_phone("267-808-3359") == "2678083359"
    assert dr._normalize_phone("(415) 555.1234") == "4155551234"


def test_normalize_phone_strips_leading_us_country_code(dr):
    assert dr._normalize_phone("16505379785") == "6505379785"
    assert dr._normalize_phone("1-650-537-9785") == "6505379785"


def test_normalize_phone_preserves_intl(dr):
    """92xxxxxxxxx is Pakistan, not US — must stay intact."""
    assert dr._normalize_phone("92267868522") == "92267868522"


def test_normalize_phone_empty_and_none(dr):
    assert dr._normalize_phone("") == ""
    assert dr._normalize_phone(None) == ""
    assert dr._normalize_phone("—") == ""


def test_build_phone_index_reads_people_files(tmp_path, dr):
    people = tmp_path / "people"
    people.mkdir()
    (people / "mohit.md").write_text(
        "# Mohit\n- **email:** m@x.com\n- **phone:** +1 (650) 537-9785\n"
        "- **last_interaction:** 2026-03-20\n",
        encoding="utf-8",
    )
    (people / "noph.md").write_text(
        "# No Phone\n- **email:** x@y.com\n- **phone:** —\n",
        encoding="utf-8",
    )

    idx = dr.build_phone_index(people)
    assert set(idx.keys()) == {"6505379785"}
    assert idx["6505379785"][0].name == "mohit.md"
    assert idx["6505379785"][1] == "2026-03-20"


def test_load_gmessages_signals_returns_phone_to_date(tmp_path, dr):
    cache = tmp_path / "mined-gmessages.json"
    cache.write_text(json.dumps({
        "status": "ok",
        "mined_at": "2026-04-14T10:00:00+00:00",
        "contacts": [
            {"name": "Dan Z", "phone": "+1 650 555 1111", "last_message_date": "2026-04-10"},
            {"name": "Yendrick", "phone": "650-555-2222", "last_message_date": "2026-04-11"},
            {"name": "Ghost", "phone": "", "last_message_date": "2026-04-12"},
        ],
    }))
    signals = dr._load_gmessages_signals(cache)
    assert signals == {
        "6505551111": "2026-04-10",
        "6505552222": "2026-04-11",
    }


def test_load_gmessages_signals_missing_file_returns_empty(tmp_path, dr):
    assert dr._load_gmessages_signals(tmp_path / "nope.json") == {}


def test_load_gmessages_signals_raises_on_malformed_json(tmp_path, dr):
    """2026-04-15: the old swallow-to-empty behavior hid connector
    gmessages corruption for days. The loader now propagates the parse
    error so daily-refresh.py's sources_failed tracking catches it and
    flips overall status to 'degraded'."""
    cache = tmp_path / "mined-gmessages.json"
    cache.write_text("not json at all {")
    with pytest.raises(json.JSONDecodeError):
        dr._load_gmessages_signals(cache)


def test_load_gmessages_by_name_strips_parenthetical_suffix(tmp_path, dr):
    """Google Messages displays contacts as 'Dan Zylberglejd (Netflix)'
    — the parenthetical employer hint is helpful in the UI but breaks
    name-match against the person file's plain 'Dan Zylberglejd' H1.
    The loader must normalize both so they match."""
    cache = tmp_path / "mined-gmessages.json"
    cache.write_text(json.dumps({
        "contacts": [
            {"name": "Dan Zylberglejd (Netflix)", "phone": "", "last_message_date": "2026-04-11"},
            {"name": "Mayra (Cleaning)", "phone": "", "last_message_date": "2026-04-07"},
            {"name": "Louise (Violet's Mom)", "phone": "", "last_message_date": "2026-04-12"},
        ],
    }))
    by_name = dr._load_gmessages_by_name(cache)
    # Both the bare name AND the (...)-stripped version are exposed
    # so the index can match either spelling.
    assert by_name["dan zylberglejd"] == "2026-04-11"
    assert by_name["mayra"] == "2026-04-07"
    assert by_name["louise"] == "2026-04-12"
    # The original full name is still present in case the person file
    # actually uses the full string.
    assert by_name["dan zylberglejd (netflix)"] == "2026-04-11"


def test_load_gmessages_by_name_returns_lowercased_name_to_date(tmp_path, dr):
    cache = tmp_path / "mined-gmessages.json"
    cache.write_text(json.dumps({
        "contacts": [
            {"name": "Dan Zylberglejd", "phone": "", "last_message_date": "2026-04-10"},
            {"name": "YENDRICK Z.", "phone": "", "last_message_date": "2026-04-11"},
            {"name": "", "phone": "+1 650 555 1111", "last_message_date": "2026-04-12"},
        ],
    }))
    by_name = dr._load_gmessages_by_name(cache)
    assert by_name == {
        "dan zylberglejd": "2026-04-10",
        "yendrick z.": "2026-04-11",
    }


def test_build_name_index_lowercases_h1(tmp_path, dr):
    people = tmp_path / "people"
    people.mkdir()
    (people / "dan.md").write_text(
        "# Dan Zylberglejd\n- **slug:** dan-zylberglejd\n- **email:** dz@x.com\n"
        "- **last_interaction:** 2026-02-11\n",
        encoding="utf-8",
    )
    idx = dr.build_name_index(people)
    assert "dan zylberglejd" in idx
    assert idx["dan zylberglejd"][0].name == "dan.md"


def test_run_falls_back_to_name_match_when_phone_missing(tmp_path, dr, monkeypatch):
    people = tmp_path / "people"
    people.mkdir()
    (people / "dan.md").write_text(
        "# Dan Zylberglejd\n- **slug:** dan-zylberglejd\n- **email:** dz@x.com\n"
        "- **phone:** —\n- **last_interaction:** 2026-02-11\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "connector-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "cache" / "mined-gmessages.json").write_text(json.dumps({
        "contacts": [
            {"name": "Dan Zylberglejd", "phone": "", "last_message_date": "2026-04-10"},
        ],
    }))
    mc_cache = tmp_path / "mc" / "cache"
    mc_cache.mkdir(parents=True)

    monkeypatch.setattr(dr, "BRAIN_PEOPLE", people)
    monkeypatch.setattr(dr, "WORKSPACE", workspace)
    monkeypatch.setattr(dr, "UPCOMING_CACHE", workspace / "upcoming-meetings.json")
    monkeypatch.setattr(dr, "GMESSAGES_CACHE", workspace / "cache" / "mined-gmessages.json")
    monkeypatch.setattr(dr, "MC_CACHE", mc_cache)
    monkeypatch.setattr(dr, "_build_google_services", lambda: (object(), object()))
    monkeypatch.setattr(
        dr, "_collect_gcal_signals", lambda svc, lookback_days, lookahead_days: ({}, {})
    )
    monkeypatch.setattr(
        dr, "_collect_gmail_signals", lambda svc, days, operator_emails: {}
    )
    monkeypatch.setattr(dr, "_operator_emails", lambda: set())

    result = dr.run()
    assert result["people_updated"] == 1
    text = (people / "dan.md").read_text(encoding="utf-8")
    assert "- **last_interaction:** 2026-04-10" in text


def test_run_continues_when_one_person_file_is_read_only(tmp_path, dr, monkeypatch):
    """2026-04-18 regression: Dropbox sync can park a single file in
    transient Errno-30 / Read-only filesystem state. Before the fix, one
    stuck file raised OSError out of update_last_interaction and aborted
    the whole batch — so every OTHER person on the same run (Kyle
    Kloster yesterday) never got stamped either. The fix: swallow per-
    file OSError, surface the failure count, keep the batch going."""
    people = tmp_path / "people"
    people.mkdir()
    stuck = people / "stuck.md"
    stuck.write_text(
        "# Stuck Person\n- **email:** stuck@x.com\n- **phone:** (650) 555-0000\n"
        "- **last_interaction:** 2026-02-01\n",
        encoding="utf-8",
    )
    kyle = people / "kyle-kloster.md"
    kyle.write_text(
        "# Kyle Kloster\n- **email:** kyle@x.com\n- **phone:** —\n"
        "- **last_interaction:** 2026-02-11\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir()
    (workspace / "cache").mkdir()
    (workspace / "cache" / "mined-gmessages.json").write_text(json.dumps({
        "contacts": [
            {"name": "Stuck Person", "phone": "", "last_message_date": "2026-04-18"},
            {"name": "Kyle Kloster", "phone": "", "last_message_date": "2026-04-17"},
        ],
    }))
    mc_cache = tmp_path / "mc" / "cache"
    mc_cache.mkdir(parents=True)

    monkeypatch.setattr(dr, "BRAIN_PEOPLE", people)
    monkeypatch.setattr(dr, "WORKSPACE", workspace)
    monkeypatch.setattr(dr, "UPCOMING_CACHE", workspace / "upcoming-meetings.json")
    monkeypatch.setattr(dr, "GMESSAGES_CACHE", workspace / "cache" / "mined-gmessages.json")
    monkeypatch.setattr(dr, "MC_CACHE", mc_cache)
    monkeypatch.setattr(dr, "_build_google_services", lambda: (object(), object()))
    monkeypatch.setattr(
        dr, "_collect_gcal_signals", lambda svc, lookback_days, lookahead_days: ({}, {})
    )
    monkeypatch.setattr(
        dr, "_collect_gmail_signals", lambda svc, days, operator_emails: {}
    )
    monkeypatch.setattr(dr, "_operator_emails", lambda: set())

    orig_update = dr.update_last_interaction

    def flaky_update(fp, new_date):
        if fp.name == "stuck.md":
            raise OSError(30, "Read-only file system", str(fp))
        return orig_update(fp, new_date)

    monkeypatch.setattr(dr, "update_last_interaction", flaky_update)

    result = dr.run()

    assert kyle.read_text(encoding="utf-8").find("- **last_interaction:** 2026-04-17") >= 0, (
        "Kyle must be stamped even though stuck.md errored"
    )
    assert result["status"] == "degraded", result
    write_failures = result.get("write_failures", [])
    assert any("stuck.md" in f.get("path", "") for f in write_failures), (
        f"stuck.md write failure must surface in result, got {write_failures}"
    )
    assert result["people_updated"] >= 1


def test_run_applies_gmessages_signals_via_phone_match(tmp_path, dr, monkeypatch):
    """Integration: a gmessages cache with a phone signal updates the
    matching person file even when Gmail/GCal have nothing to say."""
    people = tmp_path / "people"
    people.mkdir()
    (people / "dan.md").write_text(
        "# Dan\n- **email:** dan@x.com\n- **phone:** (650) 555-1111\n"
        "- **last_interaction:** 2026-02-11\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir()
    (workspace / "cache").mkdir()
    (workspace / "cache" / "mined-gmessages.json").write_text(json.dumps({
        "contacts": [
            {"name": "Dan Z", "phone": "+1 650-555-1111", "last_message_date": "2026-04-10"},
        ],
    }))
    mc_cache = tmp_path / "mc" / "cache"
    mc_cache.mkdir(parents=True)

    monkeypatch.setattr(dr, "BRAIN_PEOPLE", people)
    monkeypatch.setattr(dr, "WORKSPACE", workspace)
    monkeypatch.setattr(dr, "UPCOMING_CACHE", workspace / "upcoming-meetings.json")
    monkeypatch.setattr(dr, "GMESSAGES_CACHE", workspace / "cache" / "mined-gmessages.json")
    monkeypatch.setattr(dr, "MC_CACHE", mc_cache)
    monkeypatch.setattr(dr, "_build_google_services", lambda: (object(), object()))
    monkeypatch.setattr(
        dr, "_collect_gcal_signals", lambda svc, lookback_days, lookahead_days: ({}, {})
    )
    monkeypatch.setattr(
        dr, "_collect_gmail_signals", lambda svc, days, operator_emails: {}
    )
    monkeypatch.setattr(dr, "_operator_emails", lambda: set())

    result = dr.run()
    assert result["status"] == "ok"
    assert result["people_updated"] == 1
    assert result["gmessages_signals"] == 1
    text = (people / "dan.md").read_text(encoding="utf-8")
    assert "- **last_interaction:** 2026-04-10" in text


# ── _load_krisp_debriefs ────────────────────────────────────────────


def test_load_krisp_debriefs_keeps_small_meetings(tmp_path, dr):
    cache = tmp_path / "meetings-coach-workspace" / "cache"
    cache.mkdir(parents=True)

    small = {
        "meeting_start": {"dateTime": "2026-04-10T15:00:00-07:00"},
        "attendees": [
            {"email": "alice@x.com", "name": "Alice"},
            {"email": "bob@y.com", "name": "Bob"},
        ],
    }
    big = {
        "meeting_start": {"dateTime": "2026-04-11T10:00:00-07:00"},
        "attendees": [{"email": f"u{i}@x.com"} for i in range(12)],
    }
    (cache / "pending-debrief-small.json").write_text(json.dumps(small))
    (cache / "pending-debrief-big.json").write_text(json.dumps(big))

    signals = dr._load_krisp_debriefs(cache, attendee_cap=6)

    assert signals == {
        "alice@x.com": "2026-04-10",
        "bob@y.com": "2026-04-10",
    }
    # Nothing from the 12-attendee meeting
    assert not any("u0@x.com" in k for k in signals)


def test_load_krisp_debriefs_returns_empty_when_missing(tmp_path, dr):
    assert dr._load_krisp_debriefs(tmp_path / "nonexistent", attendee_cap=6) == {}


def test_load_krisp_debriefs_tolerates_string_meeting_start(tmp_path, dr):
    """Older debriefs stored meeting_start as a flat ISO string."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "pending-debrief-foo.json").write_text(json.dumps({
        "meeting_start": "2026-04-08T09:30:00Z",
        "attendees": [{"email": "c@x.com"}],
    }))
    signals = dr._load_krisp_debriefs(cache, attendee_cap=6)
    assert signals == {"c@x.com": "2026-04-08"}


# ── gcal / gmail via fake service ───────────────────────────────────


class _FakeGCalEvents:
    def __init__(self, pages):
        self._pages = pages

    def list(self, **kwargs):
        return self

    def execute(self):
        return self._pages.pop(0) if self._pages else {"items": []}


class _FakeGCalService:
    def __init__(self, pages):
        self._pages = pages

    def events(self):
        return _FakeGCalEvents(self._pages)


def test_collect_gcal_signals_splits_past_and_future(dr, monkeypatch):
    today = datetime.now(timezone.utc).date()
    past_iso = (today - timedelta(days=3)).isoformat()
    future_iso = (today + timedelta(days=5)).isoformat()

    events_page = {
        "items": [
            {
                "summary": "1:1 with Alice",
                "start": {"date": past_iso},
                "attendees": [
                    {"email": "alice@x.com", "self": False},
                    {"email": "operator@you.com", "self": True},
                ],
            },
            {
                "summary": "Kickoff with Bob",
                "start": {"dateTime": f"{future_iso}T15:00:00-07:00"},
                "attendees": [
                    {"email": "bob@y.com", "self": False},
                ],
            },
        ]
    }
    fake = _FakeGCalService([events_page, {}])

    past, upcoming = dr._collect_gcal_signals(fake, lookback_days=14, lookahead_days=14)

    assert past == {"alice@x.com": past_iso}
    assert upcoming == {"bob@y.com": future_iso}


def test_collect_gcal_signals_ignores_self_attendee(dr):
    today = datetime.now(timezone.utc).date()
    past_iso = (today - timedelta(days=1)).isoformat()
    fake = _FakeGCalService([{
        "items": [{
            "summary": "Solo focus block",
            "start": {"date": past_iso},
            "attendees": [{"email": "operator@you.com", "self": True}],
        }],
    }, {}])
    past, upcoming = dr._collect_gcal_signals(fake, lookback_days=14, lookahead_days=14)
    assert past == {}
    assert upcoming == {}


class _FakeGmailMessages:
    def __init__(self, ids, messages_by_id):
        self._ids = ids
        self._messages = messages_by_id
        self._pending_get_id = None

    def list(self, **kwargs):
        return types.SimpleNamespace(
            execute=lambda: {"messages": [{"id": i} for i in self._ids]}
        )

    def get(self, userId, id, format, metadataHeaders):
        msg = self._messages[id]
        return types.SimpleNamespace(execute=lambda m=msg: m)


class _FakeGmailUsers:
    def __init__(self, ids, messages_by_id):
        self._ids = ids
        self._messages = messages_by_id

    def messages(self):
        return _FakeGmailMessages(self._ids, self._messages)


class _FakeGmailService:
    def __init__(self, ids, messages_by_id):
        self._users = _FakeGmailUsers(ids, messages_by_id)

    def users(self):
        return self._users


def test_collect_gmail_signals_extracts_from_to_cc(dr):
    msgs = {
        "m1": {
            "payload": {
                "headers": [
                    {"name": "From", "value": "Dan <danzylber@gmail.com>"},
                    {"name": "To", "value": "operator@you.com"},
                    {"name": "Date", "value": "Mon, 07 Apr 2026 14:05:00 -0700"},
                ]
            }
        },
        "m2": {
            "payload": {
                "headers": [
                    {"name": "From", "value": "operator@you.com"},
                    {"name": "To", "value": "Mohit <mohit@x.com>, other@y.com"},
                    {"name": "Cc", "value": "cc@z.com"},
                    {"name": "Date", "value": "Wed, 09 Apr 2026 10:00:00 -0700"},
                ]
            }
        },
    }
    fake = _FakeGmailService(["m1", "m2"], msgs)

    operator_emails = {"operator@you.com"}
    signals = dr._collect_gmail_signals(fake, days=14, operator_emails=operator_emails)

    assert signals == {
        "danzylber@gmail.com": "2026-04-07",
        "mohit@x.com": "2026-04-09",
        "other@y.com": "2026-04-09",
        "cc@z.com": "2026-04-09",
    }


def test_collect_gmail_signals_skips_brian_self(dr):
    msgs = {
        "m1": {"payload": {"headers": [
            {"name": "From", "value": "operator@you.com"},
            {"name": "To", "value": "operator@you.com"},
            {"name": "Date", "value": "Mon, 07 Apr 2026 14:05:00 -0700"},
        ]}},
    }
    fake = _FakeGmailService(["m1"], msgs)
    assert dr._collect_gmail_signals(fake, days=14, operator_emails={"operator@you.com"}) == {}


# ── _extract_emails ─────────────────────────────────────────────────


def test_extract_emails_handles_multiple_formats(dr):
    header = 'Dan <dan@x.com>, "Mohit K." <mohit@y.com>, plain@z.com'
    emails = dr._extract_emails(header)
    assert emails == ["dan@x.com", "mohit@y.com", "plain@z.com"]


def test_extract_emails_empty(dr):
    assert dr._extract_emails("") == []
    assert dr._extract_emails(None) == []


# ── _parse_rfc2822_date ─────────────────────────────────────────────


def test_parse_rfc2822_date_standard_format(dr):
    assert dr._parse_rfc2822_date("Mon, 07 Apr 2026 14:05:00 -0700") == "2026-04-07"


def test_parse_rfc2822_date_invalid_returns_none(dr):
    assert dr._parse_rfc2822_date("not a date") is None
    assert dr._parse_rfc2822_date("") is None


# ── run() integration: wires everything, writes people + upcoming ──


def test_run_writes_updates_and_upcoming_cache(tmp_path, dr, monkeypatch):
    # Stub brain people dir
    people = tmp_path / "people"
    people.mkdir()
    (people / "dan-zylberglejd.md").write_text(
        "# Dan Zylberglejd\n- **email:** danzylber@gmail.com\n- **last_interaction:** 2026-02-11\n",
        encoding="utf-8",
    )
    (people / "mohit-kothari.md").write_text(
        "# Mohit Kothari\n- **email:** mohit@y.com\n- **last_interaction:** 2026-03-20\n",
        encoding="utf-8",
    )

    # Stub workspace
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir()

    # Stub meetings-coach workspace (empty cache = no krisp signals)
    mc_cache = tmp_path / "meetings-coach-workspace" / "cache"
    mc_cache.mkdir(parents=True)

    # Monkeypatch module paths
    monkeypatch.setattr(dr, "BRAIN_PEOPLE", people)
    monkeypatch.setattr(dr, "WORKSPACE", workspace)
    monkeypatch.setattr(dr, "UPCOMING_CACHE", workspace / "upcoming-meetings.json")
    monkeypatch.setattr(dr, "MC_CACHE", mc_cache)

    # Stub gmail / gcal collectors with fixed outputs
    today = datetime.now(timezone.utc).date()
    past_iso = (today - timedelta(days=2)).isoformat()
    future_iso = (today + timedelta(days=3)).isoformat()

    monkeypatch.setattr(dr, "_build_google_services", lambda: (object(), object()))
    monkeypatch.setattr(
        dr,
        "_collect_gcal_signals",
        lambda svc, lookback_days, lookahead_days: (
            {"danzylber@gmail.com": past_iso},  # past
            {"mohit@y.com": future_iso},  # upcoming
        ),
    )
    monkeypatch.setattr(
        dr,
        "_collect_gmail_signals",
        lambda svc, days, operator_emails: {"danzylber@gmail.com": past_iso},
    )
    monkeypatch.setattr(dr, "_operator_emails", lambda: {"operator@you.com"})

    result = dr.run()

    assert result["status"] == "ok"
    assert result["people_updated"] == 1
    # Dan got stamped with past_iso
    text = (people / "dan-zylberglejd.md").read_text(encoding="utf-8")
    assert f"- **last_interaction:** {past_iso}" in text
    # Mohit unchanged (still has his later date)
    assert "- **last_interaction:** 2026-03-20" in (
        people / "mohit-kothari.md"
    ).read_text(encoding="utf-8")
    # Upcoming cache has Mohit's future event
    upcoming = json.loads((workspace / "upcoming-meetings.json").read_text())
    assert upcoming["emails"]["mohit@y.com"] == future_iso
    assert "generated_at" in upcoming


def test_run_skips_people_not_in_index(tmp_path, dr, monkeypatch):
    people = tmp_path / "people"
    people.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mc_cache = tmp_path / "mc" / "cache"
    mc_cache.mkdir(parents=True)

    monkeypatch.setattr(dr, "BRAIN_PEOPLE", people)
    monkeypatch.setattr(dr, "WORKSPACE", workspace)
    monkeypatch.setattr(dr, "UPCOMING_CACHE", workspace / "upcoming-meetings.json")
    monkeypatch.setattr(dr, "MC_CACHE", mc_cache)
    monkeypatch.setattr(dr, "_build_google_services", lambda: (object(), object()))
    monkeypatch.setattr(
        dr,
        "_collect_gcal_signals",
        lambda svc, lookback_days, lookahead_days: ({"stranger@x.com": "2026-04-10"}, {}),
    )
    monkeypatch.setattr(
        dr, "_collect_gmail_signals", lambda svc, days, operator_emails: {}
    )
    monkeypatch.setattr(dr, "_operator_emails", lambda: set())

    result = dr.run()
    assert result["status"] == "ok"
    assert result["people_updated"] == 0
    assert result["signals_unmatched"] == 1


def test_run_returns_degraded_when_token_missing(tmp_path, dr, monkeypatch):
    people = tmp_path / "people"
    people.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mc_cache = tmp_path / "mc" / "cache"
    mc_cache.mkdir(parents=True)

    monkeypatch.setattr(dr, "BRAIN_PEOPLE", people)
    monkeypatch.setattr(dr, "WORKSPACE", workspace)
    monkeypatch.setattr(dr, "UPCOMING_CACHE", workspace / "upcoming-meetings.json")
    monkeypatch.setattr(dr, "MC_CACHE", mc_cache)

    def boom():
        raise RuntimeError("token.json not found")

    monkeypatch.setattr(dr, "_build_google_services", boom)
    monkeypatch.setattr(dr, "_operator_emails", lambda: set())

    result = dr.run()
    assert result["status"] == "degraded"
    assert "token" in result["alert"].lower()


# ── main() SCRIPT_CONTRACT wrapper ──────────────────────────────────


def test_main_prints_one_json_line_and_exits_zero(dr, capsys, monkeypatch):
    monkeypatch.setattr(dr, "run", lambda: {"status": "ok", "people_updated": 0})
    rc = dr.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    payload = json.loads(out)
    assert payload["status"] == "ok"


def test_main_emits_error_json_on_crash(dr, capsys, monkeypatch):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(dr, "run", boom)
    rc = dr.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "kaboom" in payload["error"]
