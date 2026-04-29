"""Red/green tests for promote_to_people_brain — the shared helper used
by inbox_triage_lib (cold-recruiter detection) and gmail_sent_mine_lib
(the operator-engaged recipients) to grow the people brain automatically."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))


def test_promote_creates_minimal_stub(tmp_path, monkeypatch):
    """Promoting an unknown sender writes a minimal people/<slug>.md
    with the supplied circles + auto_created tag. Idempotent on re-run."""
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path))
    from people_promote_lib import promote_to_people_brain  # type: ignore

    result = promote_to_people_brain(
        from_email="abigail.mintert@coinbase.com",
        from_header='"Abigail Mintert" <abigail.mintert@coinbase.com>',
        circles="professional-outer",
        source="sent_recipient",
        last_interaction="2026-04-28",
    )
    assert result["status"] == "created"
    assert result["slug"] == "abigail-mintert"
    fp = tmp_path / "people" / "abigail-mintert.md"
    assert fp.exists()
    text = fp.read_text(encoding="utf-8")
    assert "abigail.mintert@coinbase.com" in text
    assert "auto_created" in text
    assert "sent_recipient" in text
    assert "Abigail Mintert" in text


def test_promote_is_idempotent(tmp_path, monkeypatch):
    """A second call for the same slug is a no-op; existing file
    content is preserved (the operator may have edited it)."""
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path))
    from people_promote_lib import promote_to_people_brain  # type: ignore

    promote_to_people_brain(
        from_email="abigail.mintert@coinbase.com",
        from_header='"Abigail Mintert" <abigail.mintert@coinbase.com>',
        circles="professional-outer",
        source="sent_recipient",
    )
    # the operator edits — append a notes line
    fp = tmp_path / "people" / "abigail-mintert.md"
    edited = fp.read_text(encoding="utf-8") + "- **notes:** my edits\n"
    fp.write_text(edited, encoding="utf-8")

    result = promote_to_people_brain(
        from_email="abigail.mintert@coinbase.com",
        from_header='"Abigail Mintert" <abigail.mintert@coinbase.com>',
        circles="recruiter, professional-outer",  # different circle this time
        source="triage_recruiter",
    )
    assert result["status"] == "already_exists"
    assert result["slug"] == "abigail-mintert"
    # the operator's edits + original content preserved verbatim
    assert "my edits" in fp.read_text(encoding="utf-8")
    # Original source tag preserved
    assert "sent_recipient" in fp.read_text(encoding="utf-8")


def test_promote_uses_email_local_part_when_no_display_name(tmp_path, monkeypatch):
    """When the From header is bare email (no quoted name), the slug
    incorporates the domain so jane@a.com and jane@b.com don't collide."""
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path))
    from people_promote_lib import promote_to_people_brain  # type: ignore

    result = promote_to_people_brain(
        from_email="jane@coinbase.com",
        from_header="jane@coinbase.com",
        circles="professional-outer",
        source="sent_recipient",
    )
    assert result["status"] == "created"
    assert result["slug"] == "jane-coinbase-com"


def test_promote_lowercases_email(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path))
    from people_promote_lib import promote_to_people_brain  # type: ignore

    result = promote_to_people_brain(
        from_email="Abigail.Mintert@COINBASE.COM",
        from_header='"Abigail Mintert" <Abigail.Mintert@COINBASE.COM>',
        circles="professional-outer",
        source="sent_recipient",
    )
    assert result["status"] == "created"
    fp = tmp_path / "people" / "abigail-mintert.md"
    text = fp.read_text(encoding="utf-8")
    assert "abigail.mintert@coinbase.com" in text


def test_promote_relationship_type_recorded(tmp_path, monkeypatch):
    """When relationship_type passed, it lands in the file."""
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path))
    from people_promote_lib import promote_to_people_brain  # type: ignore

    promote_to_people_brain(
        from_email="recruiter@greenhouse-mail.io",
        from_header="Jane Recruiter <recruiter@greenhouse-mail.io>",
        circles="recruiter, professional-outer",
        source="triage_recruiter",
        relationship_type="recruiter",
    )
    fp = tmp_path / "people" / "jane-recruiter.md"
    text = fp.read_text(encoding="utf-8")
    assert "relationship_type" in text
    assert "recruiter" in text


def test_promote_skips_brian_self_emails(tmp_path, monkeypatch):
    """Edge case: never auto-create a stub for the operator's own addresses
    (could happen if sent-mine accidentally feeds the operator's email back)."""
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path))
    from people_promote_lib import promote_to_people_brain  # type: ignore

    result = promote_to_people_brain(
        from_email="sam.smith@example.com",
        from_header="Sam Smith <sam.smith@example.com>",
        circles="professional-outer",
        source="sent_recipient",
        skip_emails={"sam.smith@example.com", "sam.smith+backup@example.com"},
    )
    assert result["status"] == "skipped_self"
    assert not (tmp_path / "people" / "sam-smith.md").exists()


def test_promote_returns_path_for_caller_logging(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path))
    from people_promote_lib import promote_to_people_brain  # type: ignore

    result = promote_to_people_brain(
        from_email="alice@example.com",
        from_header='"Alice Smith" <alice@example.com>',
        circles="professional-outer",
        source="sent_recipient",
    )
    assert "path" in result
    assert result["path"].endswith("alice-smith.md")
