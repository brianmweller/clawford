"""Tests for the Mistress Mouse / Sergeant Murphy routing boundary.

Rule (memory: project_meeting_event_routing.md, revised 2026-04-18):
videoconference link = Murphy's meeting, no link = Mouse's event.
Mutually exclusive. Both gcal-fetch.py and reminder-check.py must
apply the same predicate.

The filename is retained for git-history continuity — these tests
used to check Workflowy-link gating (pre-2026-04-18 boundary).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    """Load a script from family-calendar/scripts/ as a fresh module.

    Stubs googleapiclient + google.auth so the import succeeds in
    bare test environments. Also injects REPO_ROOT onto sys.path so
    the `from agents.shared.meeting_classifier import ...` shim
    resolves against the real shared module.
    """
    for mod in ("googleapiclient", "googleapiclient.discovery", "google",
                "google.auth", "google.auth.transport", "google.auth.transport.requests",
                "google.oauth2", "google.oauth2.credentials"):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"famcal_{name.replace('-','_').replace('.py','')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_gcal_fetch_imports_shared_classifier():
    """The module must pick up has_videoconference_link from the shared
    library so Mouse and Murphy share one source of truth."""
    mod = _load_script("gcal-fetch.py")
    assert callable(mod.has_videoconference_link)
    assert mod.has_videoconference_link({"hangoutLink": "https://meet.google.com/x"}) is True
    assert mod.has_videoconference_link({"summary": "in-person"}) is False


def test_reminder_check_imports_shared_classifier():
    mod = _load_script("reminder-check.py")
    assert callable(mod.has_videoconference_link)
    assert mod.has_videoconference_link({"hangoutLink": "https://meet.google.com/x"}) is True


def test_workflowy_loader_is_gone_from_gcal_fetch():
    """2026-04-18: routing no longer keys off Workflowy link presence.
    The loader was load-bearing under the old rule — guard against a
    revert by asserting it's deleted."""
    mod = _load_script("gcal-fetch.py")
    assert not hasattr(mod, "load_workflowy_linked_event_ids")


def test_workflowy_loader_is_gone_from_reminder_check():
    mod = _load_script("reminder-check.py")
    assert not hasattr(mod, "load_workflowy_linked_event_ids")


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
