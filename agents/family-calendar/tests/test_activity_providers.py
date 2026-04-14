"""Tests for activity-email-check.py provider loading.

The script queries Gmail for emails from a set of providers. Real
provider names (school, swim class, ballet, school-notification
platform) are PII and must not live in the tracked script. They load
from the gitignored `calendar-config.json` at runtime, with a
sanitized fallback baked into the module so that tests and the
committed script never expose real names.

Run: cd agents/family-calendar && python3 -m pytest tests/test_activity_providers.py -v
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script():
    path = SCRIPTS_DIR / "activity-email-check.py"
    spec = importlib.util.spec_from_file_location("famcal_activity_email_check", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def aec():
    return _load_script()


def test_load_providers_reads_activity_providers_from_config(aec, tmp_path):
    config = tmp_path / "calendar-config.json"
    config.write_text(json.dumps({
        "calendars": [],
        "activity_providers": [
            {"name": "School", "query": "from:school newer_than:2d"},
            {"name": "Swim", "query": "from:swim newer_than:2d"},
        ],
    }))
    providers = aec.load_providers(str(config))
    assert len(providers) == 2
    assert providers[0]["name"] == "School"
    assert providers[1]["query"] == "from:swim newer_than:2d"


def test_load_providers_falls_back_when_key_missing(aec, tmp_path):
    config = tmp_path / "calendar-config.json"
    config.write_text(json.dumps({"calendars": []}))
    providers = aec.load_providers(str(config))
    assert providers == aec.DEFAULT_PROVIDERS


def test_load_providers_falls_back_when_config_missing(aec, tmp_path):
    missing = tmp_path / "does-not-exist.json"
    providers = aec.load_providers(str(missing))
    assert providers == aec.DEFAULT_PROVIDERS


def test_load_providers_falls_back_when_activity_providers_empty(aec, tmp_path):
    config = tmp_path / "calendar-config.json"
    config.write_text(json.dumps({"activity_providers": []}))
    providers = aec.load_providers(str(config))
    assert providers == aec.DEFAULT_PROVIDERS


def test_load_providers_falls_back_when_config_unparseable(aec, tmp_path):
    config = tmp_path / "calendar-config.json"
    config.write_text("not valid json {")
    providers = aec.load_providers(str(config))
    assert providers == aec.DEFAULT_PROVIDERS


def test_default_providers_are_sanitized(aec):
    """DEFAULT_PROVIDERS must stay sanitized — every entry must use the
    `Example …` placeholder convention so the tracked script never
    contains a real provider name. Real names live in the gitignored
    calendar-config.json and load at runtime.
    """
    for provider in aec.DEFAULT_PROVIDERS:
        assert provider["name"].startswith("Example "), (
            f"DEFAULT_PROVIDERS entry {provider!r} is not sanitized"
        )
        assert "Example" in provider["query"], (
            f"DEFAULT_PROVIDERS query {provider!r} is not sanitized"
        )
