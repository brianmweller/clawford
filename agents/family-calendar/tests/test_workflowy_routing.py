"""Tests for the Mistress Mouse / Sergeant Murphy routing boundary.

Per memory project_meeting_event_routing.md: events present in
Sergeant Murphy's `workflowy-links.json` are his domain; Mistress
Mouse (family-calendar) must EXCLUDE those events from morning
briefings and reminders.

Both gcal-fetch.py and reminder-check.py implement a cross-agent
read of Murphy's workflowy-links.json via the env var
WORKFLOWY_LINKS_PATH, which these tests point at a tmp fixture.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    """Load a script from family-calendar/scripts/ as a fresh module.

    Stubs out googleapiclient + google.auth so the module imports on
    bare test environments that don't have those installed.
    """
    for mod in ("googleapiclient", "googleapiclient.discovery", "google",
                "google.auth", "google.auth.transport", "google.auth.transport.requests",
                "google.oauth2", "google.oauth2.credentials"):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"famcal_{name.replace('-','_').replace('.py','')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def workflowy_links_file(tmp_path, monkeypatch):
    """Write a fake workflowy-links.json and point the scripts at it."""
    path = tmp_path / "workflowy-links.json"
    payload = {
        "EVENT_A_is_meeting": {
            "node_id": "aaaa", "title": "Catch Up", "date": "2026-04-12",
        },
        "EVENT_B_is_meeting": {
            "node_id": "bbbb", "title": "Exploring Ballet", "date": "2026-04-12",
        },
    }
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("WORKFLOWY_LINKS_PATH", str(path))
    return path


def test_gcal_fetch_loads_workflowy_links(workflowy_links_file):
    mod = _load_script("gcal-fetch.py")
    linked = mod.load_workflowy_linked_event_ids()
    assert linked == {"EVENT_A_is_meeting", "EVENT_B_is_meeting"}


def test_gcal_fetch_returns_empty_set_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWY_LINKS_PATH", str(tmp_path / "nonexistent.json"))
    mod = _load_script("gcal-fetch.py")
    linked = mod.load_workflowy_linked_event_ids()
    assert linked == set()


def test_gcal_fetch_returns_empty_set_on_malformed_json(tmp_path, monkeypatch):
    path = tmp_path / "workflowy-links.json"
    path.write_text("not json {{")
    monkeypatch.setenv("WORKFLOWY_LINKS_PATH", str(path))
    mod = _load_script("gcal-fetch.py")
    linked = mod.load_workflowy_linked_event_ids()
    assert linked == set()


def test_reminder_check_loads_workflowy_links(workflowy_links_file):
    mod = _load_script("reminder-check.py")
    linked = mod.load_workflowy_linked_event_ids()
    assert "EVENT_A_is_meeting" in linked
    assert "EVENT_B_is_meeting" in linked
    assert "EVENT_NOT_MEETING" not in linked


def test_reminder_check_returns_empty_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWY_LINKS_PATH", str(tmp_path / "nowhere.json"))
    mod = _load_script("reminder-check.py")
    assert mod.load_workflowy_linked_event_ids() == set()


def test_parse_args_recognizes_skip_meetings_flag():
    mod = _load_script("gcal-fetch.py")
    with patch.object(sys, "argv", ["gcal-fetch.py", "--skip-meetings"]):
        target_date, days, cal_id, skip_meetings = mod.parse_args()
    assert skip_meetings is True


def test_parse_args_skip_meetings_defaults_false():
    mod = _load_script("gcal-fetch.py")
    with patch.object(sys, "argv", ["gcal-fetch.py"]):
        target_date, days, cal_id, skip_meetings = mod.parse_args()
    assert skip_meetings is False
