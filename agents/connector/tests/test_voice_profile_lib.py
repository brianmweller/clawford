"""Pure helpers for voice-profile-build.py.

bucket_people_by_circle: reads people/*.md and returns circle → [email].
build_profile_extraction_prompt: constructs the LLM prompt from samples.
parse_profile_response: parses the LLM's JSON response, strips fences,
validates schema.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from voice_profile_lib import (  # type: ignore
    bucket_people_by_circle,
    build_profile_extraction_prompt,
    parse_profile_response,
)


# --- bucket_people_by_circle ---

def test_bucket_single_circle(tmp_path: Path):
    people = tmp_path / "people"
    people.mkdir()
    (people / "alice.md").write_text(
        "# Alice\n- **slug:** alice\n- **email:** alice@ex.com\n"
        "- **circles:** family-inner\n",
        encoding="utf-8",
    )
    result = bucket_people_by_circle(people)
    assert "family-inner" in result
    assert "alice@ex.com" in result["family-inner"]


def test_bucket_multi_circle_person(tmp_path: Path):
    people = tmp_path / "people"
    people.mkdir()
    (people / "bob.md").write_text(
        "# Bob\n- **slug:** bob\n- **email:** bob@ex.com\n"
        "- **circles:** friends-close, professional-inner\n",
        encoding="utf-8",
    )
    result = bucket_people_by_circle(people)
    assert "bob@ex.com" in result.get("friends-close", [])
    assert "bob@ex.com" in result.get("professional-inner", [])


def test_bucket_skips_people_without_email(tmp_path: Path):
    people = tmp_path / "people"
    people.mkdir()
    (people / "noemail.md").write_text(
        "# No Email\n- **slug:** noemail\n- **email:** —\n"
        "- **circles:** family-inner\n",
        encoding="utf-8",
    )
    (people / "withemail.md").write_text(
        "# With Email\n- **slug:** withemail\n- **email:** x@ex.com\n"
        "- **circles:** family-inner\n",
        encoding="utf-8",
    )
    result = bucket_people_by_circle(people)
    assert result.get("family-inner") == ["x@ex.com"]


def test_bucket_skips_template_files(tmp_path: Path):
    people = tmp_path / "people"
    people.mkdir()
    (people / "_template.md").write_text(
        "# T\n- **slug:** _template\n- **email:** t@ex.com\n"
        "- **circles:** family-inner\n",
        encoding="utf-8",
    )
    (people / "real.md").write_text(
        "# Real\n- **slug:** real\n- **email:** real@ex.com\n"
        "- **circles:** family-inner\n",
        encoding="utf-8",
    )
    result = bucket_people_by_circle(people)
    assert result["family-inner"] == ["real@ex.com"]


def test_bucket_skips_unknown_circle_marker(tmp_path: Path):
    # "unknown" is the placeholder auto-generated stubs use; don't include
    # those in voice training — they're sparse and miscalibrated by design.
    people = tmp_path / "people"
    people.mkdir()
    (people / "stub.md").write_text(
        "# Stub\n- **slug:** stub\n- **email:** s@ex.com\n"
        "- **circles:** unknown\n",
        encoding="utf-8",
    )
    result = bucket_people_by_circle(people)
    assert "unknown" not in result


# --- build_profile_extraction_prompt ---

def test_prompt_includes_circle_name_and_samples():
    samples = [
        {"to": "alice@ex.com", "date": "2026-04-01", "body": "Thanks! the operator"},
        {"to": "bob@ex.com", "date": "2026-04-02", "body": "Yep -- see you Sat."},
    ]
    prompt = build_profile_extraction_prompt("family-inner", samples)
    assert "family-inner" in prompt
    assert "Thanks! the operator" in prompt
    assert "see you Sat." in prompt


def test_prompt_requests_structured_json_output():
    prompt = build_profile_extraction_prompt("friends-close", [])
    # Schema keys the LLM must fill
    for key in [
        "typical_greeting", "typical_signoff", "register",
        "common_patterns", "anti_patterns",
        "sample_opening_phrases", "distinctive_traits",
    ]:
        assert key in prompt, f"schema missing: {key}"


def test_prompt_caps_sample_count_in_display():
    # Prompts with 100+ samples would blow context; prompt builder must
    # not silently swallow huge inputs. For now, we simply include them
    # all and trust the caller to cap at ~30-50 upstream.
    samples = [{"to": f"x{i}@ex.com", "date": "2026-04-01", "body": f"msg {i}"}
               for i in range(10)]
    prompt = build_profile_extraction_prompt("family-inner", samples)
    for i in range(10):
        assert f"msg {i}" in prompt


# --- parse_profile_response ---

def _valid_profile_json(**overrides):
    base = {
        "typical_greeting": "Hey {name},",
        "typical_signoff": "Love, the operator",
        "register": "intimate",
        "common_patterns": [
            "uses contractions heavily",
            "double-dash for em-dashes",
            "sentence fragments OK",
        ],
        "anti_patterns": ["formal salutations", "long paragraphs"],
        "sample_opening_phrases": ["Thanks!", "Yep —", "Got it."],
        "distinctive_traits": "Terse, warm, punctuation-driven rhythm.",
    }
    base.update(overrides)
    return json.dumps(base)


def test_parse_valid_response():
    result = parse_profile_response(_valid_profile_json())
    assert "error" not in result
    assert result["register"] == "intimate"
    assert "formal salutations" in result["anti_patterns"]


def test_parse_strips_markdown_fences():
    fenced = f"```json\n{_valid_profile_json()}\n```"
    result = parse_profile_response(fenced)
    assert "error" not in result
    assert result["typical_signoff"] == "Love, the operator"


def test_parse_malformed_json_returns_error():
    result = parse_profile_response("not json")
    assert "error" in result


def test_parse_missing_required_field_returns_error():
    raw = json.loads(_valid_profile_json())
    del raw["typical_greeting"]
    result = parse_profile_response(json.dumps(raw))
    assert "error" in result
