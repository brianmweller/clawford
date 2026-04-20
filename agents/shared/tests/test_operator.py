"""Tests for agents.shared.operator — operator identity loader.

The operator module abstracts "who is the human running this fleet" away
from tracked code. Real values live in ~/.clawford/operator.json; a
committed .example template documents the shape.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.shared.operator import Operator, load_operator


@pytest.fixture
def sample_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "operator.json"
    cfg.write_text(
        json.dumps(
            {
                "emails": [
                    "sam.smith@example.com",
                    "Sam.Smith+work@example.com",
                    "sam@example.org",
                ],
                "name_variants": ["Sam Smith", "SAM", "Samuel Smith"],
                "google_email": "Sam.Smith@example.com",
            }
        ),
        encoding="utf-8",
    )
    return cfg


def test_load_returns_operator_with_lowercased_emails(sample_config: Path):
    op = load_operator(sample_config)
    assert isinstance(op, Operator)
    assert "sam.smith@example.com" in op.emails
    assert "sam.smith+work@example.com" in op.emails
    assert "sam@example.org" in op.emails


def test_load_lowercases_and_strips_name_variants(sample_config: Path):
    op = load_operator(sample_config)
    assert "sam smith" in op.name_variants
    assert "sam" in op.name_variants
    assert "samuel smith" in op.name_variants


def test_load_lowercases_google_email(sample_config: Path):
    op = load_operator(sample_config)
    assert op.google_email == "sam.smith@example.com"


def test_operator_is_frozen(sample_config: Path):
    op = load_operator(sample_config)
    with pytest.raises((AttributeError, TypeError)):
        op.google_email = "other@example.com"  # type: ignore[misc]


def test_emails_is_frozenset(sample_config: Path):
    op = load_operator(sample_config)
    assert isinstance(op.emails, frozenset)
    with pytest.raises((AttributeError, TypeError)):
        op.emails.add("new@example.com")  # type: ignore[attr-defined]


def test_missing_file_raises_with_hint(tmp_path: Path):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(FileNotFoundError) as exc:
        load_operator(missing)
    msg = str(exc.value)
    assert "operator" in msg.lower()
    assert ".example" in msg


def test_env_var_overrides_default_path(sample_config: Path, monkeypatch):
    monkeypatch.setenv("CLAWFORD_OPERATOR_CONFIG", str(sample_config))
    op = load_operator()
    assert "sam.smith@example.com" in op.emails


def test_explicit_path_beats_env_var(sample_config: Path, tmp_path: Path, monkeypatch):
    other = tmp_path / "other.json"
    other.write_text(
        json.dumps(
            {
                "emails": ["jordan@example.com"],
                "name_variants": ["Jordan"],
                "google_email": "jordan@example.com",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWFORD_OPERATOR_CONFIG", str(sample_config))
    op = load_operator(other)
    assert "jordan@example.com" in op.emails
    assert "sam.smith@example.com" not in op.emails


def test_empty_name_variants_allowed(tmp_path: Path):
    cfg = tmp_path / "operator.json"
    cfg.write_text(
        json.dumps(
            {
                "emails": ["sam@example.com"],
                "google_email": "sam@example.com",
            }
        ),
        encoding="utf-8",
    )
    op = load_operator(cfg)
    assert op.name_variants == frozenset()
    assert op.slugs == frozenset()


def test_loads_slugs_lowercased(tmp_path: Path):
    cfg = tmp_path / "operator.json"
    cfg.write_text(
        json.dumps(
            {
                "emails": ["sam@example.com"],
                "slugs": ["Sam-Smith", "SAM", "sam-m-smith"],
                "google_email": "sam@example.com",
            }
        ),
        encoding="utf-8",
    )
    op = load_operator(cfg)
    assert op.slugs == frozenset({"sam-smith", "sam", "sam-m-smith"})
