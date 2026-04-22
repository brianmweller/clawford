"""Tests for agents/connector/scripts/recruiter_callback_lib.py.

Contract:
- handle_recruiter_callback(action, arg, *, people_dir, workspace_dir,
  queue_path, now_iso) routes a `recruiter:<action>:<thread_id>`
  callback to the right operation and returns a structured dict.
- Keep creates `people/<slug>.md`, appends a line to
  `cache/kept-recruiters.jsonl`, and leaves the queue alone (the
  auto-compose processed-log prevents re-processing; subsequent inbounds
  from the same sender route as `queued` via email_to_slug).
- Reject appends a line to `cache/rejected-recruiters.jsonl` with the
  sender email; inbox_triage's short-circuit uses this to drop future
  inbounds as `skipped_rejected_recruiter`.
- Never raises on bad input; returns `{"status": "error", ...}` instead.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "recruiter_callback_lib", _SCRIPTS_DIR / "recruiter_callback_lib.py",
)
rc_lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc_lib)


# ---------------------------------------------------------------------------
# Fixture scaffolding
# ---------------------------------------------------------------------------


def _sample_queue_entry(
    thread_id: str = "t-abc123",
    from_email: str = "jane@lever.co",
    from_header: str = '"Jane Recruiter" <jane@lever.co>',
    subject: str = "Opportunity at FooCorp",
    matched_domain: str = "lever.co",
) -> dict:
    return {
        "thread_id": thread_id,
        "from_email": from_email,
        "from_header": from_header,
        "subject": subject,
        "date": "Mon, 21 Apr 2026 12:00:00 -0700",
        "snippet": "Your background looks strong for...",
        "in_reply_to_message_id": "<msg-id@mail.gmail.com>",
        "status": "queued_cold_recruiter",
        "recruiter_signal_confidence": 0.85,
        "recruiter_signal_reason": "domain_match",
        "recruiter_matched_domain": matched_domain,
    }


def _seed(tmp_path: Path, entries: list[dict] | None = None) -> tuple[Path, Path, Path]:
    """Create people_dir, queue_path (with entries), workspace_dir (with cache/).
    Returns (people_dir, queue_path, workspace_dir)."""
    people_dir = tmp_path / "brain" / "people"
    people_dir.mkdir(parents=True)

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "cache").mkdir(parents=True)

    queue_path = workspace_dir / "cache" / "triage-queue.json"
    queue = {"queued": entries or [_sample_queue_entry()]}
    queue_path.write_text(json.dumps(queue), encoding="utf-8")

    return people_dir, queue_path, workspace_dir


# ---------------------------------------------------------------------------
# derive_person_fields_from_queue_entry
# ---------------------------------------------------------------------------


def test_derive_fields_prefers_display_name_over_email_local():
    entry = _sample_queue_entry(
        from_email="jane@lever.co",
        from_header='"Jane Recruiter" <jane@lever.co>',
    )
    fields = rc_lib.derive_person_fields_from_queue_entry(entry)
    assert fields["name"] == "Jane Recruiter"
    assert fields["slug"] == "jane-recruiter"
    assert fields["email"] == "jane@lever.co"
    assert "recruiter" in fields["circles"]
    assert "professional-outer" in fields["circles"]
    assert fields["relationship_type"] == "recruiter"
    assert fields["recruiter_signal_domain"] == "lever.co"
    assert fields["source_thread_id"] == "t-abc123"


def test_derive_fields_falls_back_to_email_local_when_no_display_name():
    entry = _sample_queue_entry(
        from_email="noreply@ashbyhq.com",
        from_header="noreply@ashbyhq.com",
    )
    fields = rc_lib.derive_person_fields_from_queue_entry(entry)
    # No quoted display name; fall back to email local part.
    assert fields["name"] == "noreply"
    assert fields["slug"] == "noreply-ashbyhq-com"


def test_derive_fields_slug_is_domain_qualified_when_ambiguous_local_part():
    """A common first-name local part ('jane') should not collide with an
    existing jane-doe people file. We qualify with the domain to avoid
    collisions for recruiter stubs."""
    entry = _sample_queue_entry(
        from_email="jane@lever.co",
        from_header="jane@lever.co",  # no display name
    )
    fields = rc_lib.derive_person_fields_from_queue_entry(entry)
    # Without a display name, use email-local + domain so jane@lever.co and
    # jane@greenhouse-mail.io produce distinct slugs.
    assert fields["slug"] == "jane-lever-co"


# ---------------------------------------------------------------------------
# keep_recruiter
# ---------------------------------------------------------------------------


def test_keep_creates_person_file_with_expected_fields(tmp_path: Path, monkeypatch):
    people_dir, queue_path, workspace_dir = _seed(tmp_path)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))

    result = rc_lib.handle_recruiter_callback(
        "keep", "t-abc123",
        people_dir=people_dir,
        workspace_dir=workspace_dir,
        queue_path=queue_path,
        now_iso="2026-04-21T18:00:00Z",
    )

    assert result["status"] == "ok"
    assert result["action"] == "keep"
    assert result["slug"] == "jane-recruiter"

    person_file = people_dir / "jane-recruiter.md"
    assert person_file.exists()
    content = person_file.read_text(encoding="utf-8")
    assert "# Jane Recruiter" in content
    assert "email:** jane@lever.co" in content
    assert "recruiter" in content
    assert "professional-outer" in content
    assert "relationship_type:** recruiter" in content


def test_keep_stamps_last_interaction_from_queue_entry_date(
    tmp_path: Path, monkeypatch
):
    """Cold-recruiter keep must stamp last_interaction=<inbound email date>.
    Without it, people-scan.py treats the new person as days_since=999 and
    surfaces them in the next morning nudge as 909 days overdue
    (regression 2026-04-22: Michelle Leist)."""
    people_dir, queue_path, workspace_dir = _seed(tmp_path)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))

    rc_lib.handle_recruiter_callback(
        "keep", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-22T18:00:00Z",
    )

    content = (people_dir / "jane-recruiter.md").read_text(encoding="utf-8")
    # Queue entry date is "Mon, 21 Apr 2026 12:00:00 -0700" — parsed to 2026-04-21.
    assert "last_interaction:** 2026-04-21" in content


def test_keep_falls_back_to_now_date_when_queue_date_missing(
    tmp_path: Path, monkeypatch
):
    """Missing/malformed queue `date` must fall back to now_iso's calendar
    date rather than leave last_interaction blank (which re-creates the
    999-days bug)."""
    entry = _sample_queue_entry()
    entry.pop("date", None)
    people_dir, queue_path, workspace_dir = _seed(tmp_path, entries=[entry])
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))

    rc_lib.handle_recruiter_callback(
        "keep", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-22T18:00:00Z",
    )

    content = (people_dir / "jane-recruiter.md").read_text(encoding="utf-8")
    assert "last_interaction:** 2026-04-22" in content


def test_keep_appends_to_kept_jsonl(tmp_path: Path, monkeypatch):
    people_dir, queue_path, workspace_dir = _seed(tmp_path)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))

    rc_lib.handle_recruiter_callback(
        "keep", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T18:00:00Z",
    )

    log_path = workspace_dir / "cache" / "kept-recruiters.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["slug"] == "jane-recruiter"
    assert entry["from_email"] == "jane@lever.co"
    assert entry["thread_id"] == "t-abc123"
    assert entry["kept_at"] == "2026-04-21T18:00:00Z"


def test_keep_returns_already_kept_on_second_call(tmp_path: Path, monkeypatch):
    people_dir, queue_path, workspace_dir = _seed(tmp_path)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))

    rc_lib.handle_recruiter_callback(
        "keep", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T18:00:00Z",
    )
    result2 = rc_lib.handle_recruiter_callback(
        "keep", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T19:00:00Z",
    )

    assert result2["status"] == "already_kept"
    assert result2["slug"] == "jane-recruiter"


def test_keep_returns_not_found_for_unknown_thread_id(tmp_path: Path, monkeypatch):
    people_dir, queue_path, workspace_dir = _seed(tmp_path)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))

    result = rc_lib.handle_recruiter_callback(
        "keep", "t-nonexistent",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T18:00:00Z",
    )

    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# reject_recruiter
# ---------------------------------------------------------------------------


def test_reject_appends_to_rejected_jsonl_without_creating_person(tmp_path: Path):
    people_dir, queue_path, workspace_dir = _seed(tmp_path)

    result = rc_lib.handle_recruiter_callback(
        "reject", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T18:00:00Z",
    )

    assert result["status"] == "ok"
    assert result["action"] == "reject"

    # No person file created.
    assert list(people_dir.glob("*.md")) == []

    # Rejected jsonl has the sender.
    rej_path = workspace_dir / "cache" / "rejected-recruiters.jsonl"
    assert rej_path.exists()
    lines = rej_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["from_email"] == "jane@lever.co"
    assert entry["thread_id"] == "t-abc123"
    assert entry["rejected_at"] == "2026-04-21T18:00:00Z"


def test_reject_is_idempotent_on_second_call(tmp_path: Path):
    people_dir, queue_path, workspace_dir = _seed(tmp_path)

    rc_lib.handle_recruiter_callback(
        "reject", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T18:00:00Z",
    )
    result2 = rc_lib.handle_recruiter_callback(
        "reject", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T19:00:00Z",
    )

    assert result2["status"] == "already_rejected"

    rej_path = workspace_dir / "cache" / "rejected-recruiters.jsonl"
    lines = rej_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # Still only one entry.


# ---------------------------------------------------------------------------
# load_rejected_recruiters — used by inbox_triage short-circuit
# ---------------------------------------------------------------------------


def test_load_rejected_returns_set_of_emails(tmp_path: Path):
    rej_path = tmp_path / "rejected-recruiters.jsonl"
    rej_path.write_text(
        json.dumps({"from_email": "a@greenhouse-mail.io", "thread_id": "t1",
                    "rejected_at": "2026-04-21T18:00:00Z"}) + "\n"
        + json.dumps({"from_email": "b@lever.co", "thread_id": "t2",
                      "rejected_at": "2026-04-21T18:01:00Z"}) + "\n",
        encoding="utf-8",
    )

    loaded = rc_lib.load_rejected_recruiters(rej_path)
    assert loaded == {"a@greenhouse-mail.io", "b@lever.co"}


def test_load_rejected_returns_empty_set_when_file_absent(tmp_path: Path):
    loaded = rc_lib.load_rejected_recruiters(tmp_path / "does-not-exist.jsonl")
    assert loaded == set()


def test_load_rejected_normalizes_casing(tmp_path: Path):
    rej_path = tmp_path / "rejected-recruiters.jsonl"
    rej_path.write_text(
        json.dumps({"from_email": "Mixed.Case@Lever.CO", "thread_id": "t1",
                    "rejected_at": "2026-04-21T18:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    loaded = rc_lib.load_rejected_recruiters(rej_path)
    assert loaded == {"mixed.case@lever.co"}


# ---------------------------------------------------------------------------
# Unknown action / error surface
# ---------------------------------------------------------------------------


def test_unknown_action_returns_structured_error(tmp_path: Path):
    people_dir, queue_path, workspace_dir = _seed(tmp_path)
    result = rc_lib.handle_recruiter_callback(
        "bogus", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "error"
    assert "unknown" in result["detail"].lower()


def test_malformed_queue_json_returns_error(tmp_path: Path):
    people_dir, queue_path, workspace_dir = _seed(tmp_path)
    queue_path.write_text("{not valid json", encoding="utf-8")

    result = rc_lib.handle_recruiter_callback(
        "keep", "t-abc123",
        people_dir=people_dir, workspace_dir=workspace_dir,
        queue_path=queue_path, now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "error"
