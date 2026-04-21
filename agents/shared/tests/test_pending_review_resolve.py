"""Tests for agents/shared/pending_review_resolve.py — the approve/reject
helpers that promote or reject pending-review facts.

approve moves a fact from `<facts_dir>/_pending_review.md` into the
canonical `<facts_dir>/YYYY-MM.md` file with confidence bumped to 0.95
and `operator_confirmed_at` stamped.

reject removes from `_pending_review.md` and appends to a new
`<facts_dir>/_rejected.md` so the miner can dedupe subsequent proposals.
"""
from __future__ import annotations

import sys
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import pending_review_resolve  # type: ignore


_SAMPLE_PENDING = """\
# Pending review — low-confidence mined facts

---
- **id:** connector-jane-doe-xyz
- **subject:** jane-doe
- **category:** preference
- **confidence:** 0.45
- **audience_scope:** ["personal"]
- **source:** gmail:msg-abc
- **reason:** low-conf extraction
- **content:** Jane prefers tea over coffee.
"""


def _seed(tmp_path: Path) -> Path:
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "_pending_review.md").write_text(_SAMPLE_PENDING, encoding="utf-8")
    return facts_dir


# ─── load_pending_fact ───────────────────────────────────────────────


def test_load_pending_fact_returns_parsed_fields(tmp_path: Path):
    facts_dir = _seed(tmp_path)
    f = pending_review_resolve.load_pending_fact(facts_dir, "connector-jane-doe-xyz")
    assert f is not None
    assert f["subject"] == "jane-doe"
    assert "tea" in f["content"]
    assert f["confidence"] == 0.45


def test_load_pending_fact_returns_none_when_absent(tmp_path: Path):
    facts_dir = _seed(tmp_path)
    assert pending_review_resolve.load_pending_fact(facts_dir, "ghost-id") is None


def test_load_pending_fact_returns_none_when_file_missing(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    assert pending_review_resolve.load_pending_fact(facts_dir, "any") is None


# ─── approve ─────────────────────────────────────────────────────────


def test_approve_moves_fact_into_canonical_month_file(tmp_path: Path):
    facts_dir = _seed(tmp_path)
    result = pending_review_resolve.approve(
        facts_dir, "connector-jane-doe-xyz",
        recorded_at="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "approved"
    # New fact landed in the month file
    month_text = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "connector-jane-doe-xyz" in month_text
    assert "Jane prefers tea over coffee." in month_text
    # Confidence bumped to 0.95
    assert "- **confidence:** 0.95" in month_text
    # Operator confirmation stamp present
    assert "- **operator_confirmed_at:**" in month_text
    # Removed from pending-review
    pending = (facts_dir / "_pending_review.md").read_text(encoding="utf-8")
    assert "connector-jane-doe-xyz" not in pending


def test_approve_returns_not_found_when_id_absent(tmp_path: Path):
    facts_dir = _seed(tmp_path)
    result = pending_review_resolve.approve(
        facts_dir, "ghost", recorded_at="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "not_found"


def test_approve_is_idempotent_on_already_promoted_fact(tmp_path: Path):
    facts_dir = _seed(tmp_path)
    pending_review_resolve.approve(
        facts_dir, "connector-jane-doe-xyz", recorded_at="2026-04-21T18:00:00Z",
    )
    # Second call — fact is no longer in pending → not_found
    result = pending_review_resolve.approve(
        facts_dir, "connector-jane-doe-xyz", recorded_at="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "not_found"


# ─── reject ──────────────────────────────────────────────────────────


def test_reject_writes_to_rejected_file(tmp_path: Path):
    facts_dir = _seed(tmp_path)
    result = pending_review_resolve.reject(
        facts_dir, "connector-jane-doe-xyz", rejected_at="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "rejected"
    rejected = (facts_dir / "_rejected.md").read_text(encoding="utf-8")
    assert "connector-jane-doe-xyz" in rejected
    assert "Jane prefers tea over coffee." in rejected
    assert "- **rejected_at:** 2026-04-21T18:00:00Z" in rejected
    # And removed from pending
    pending = (facts_dir / "_pending_review.md").read_text(encoding="utf-8")
    assert "connector-jane-doe-xyz" not in pending


def test_reject_returns_not_found_when_id_absent(tmp_path: Path):
    facts_dir = _seed(tmp_path)
    result = pending_review_resolve.reject(
        facts_dir, "ghost", rejected_at="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "not_found"


def test_reject_preserves_rejected_file_across_multiple_rejects(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    pending = facts_dir / "_pending_review.md"
    pending.write_text(
        _SAMPLE_PENDING
        + "\n---\n"
        + "- **id:** connector-alex-abc\n"
          "- **subject:** alex-reyes\n"
          "- **category:** event\n"
          "- **confidence:** 0.4\n"
          "- **audience_scope:** [\"professional\"]\n"
          "- **source:** gmail:alex-msg\n"
          "- **content:** Alex mentioned a new client.\n",
        encoding="utf-8",
    )
    pending_review_resolve.reject(facts_dir, "connector-jane-doe-xyz",
                                  rejected_at="2026-04-21T18:00:00Z")
    pending_review_resolve.reject(facts_dir, "connector-alex-abc",
                                  rejected_at="2026-04-22T08:00:00Z")
    rejected = (facts_dir / "_rejected.md").read_text(encoding="utf-8")
    assert "connector-jane-doe-xyz" in rejected
    assert "connector-alex-abc" in rejected


# ─── _rejected.md skip-signature support ─────────────────────────────


def test_load_rejected_signatures_returns_subject_and_content_hashes(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    facts_dir_rej = facts_dir / "_rejected.md"
    facts_dir_rej.write_text(
        "# Rejected facts — training signal for miner dedupe\n"
        "\n---\n\n"
        "- **id:** connector-jane-doe-old\n"
        "- **subject:** jane-doe\n"
        "- **content:** Jane prefers tea over coffee.\n"
        "- **rejected_at:** 2026-04-21T18:00:00Z\n"
        "- **rejected_reason:** operator\n",
        encoding="utf-8",
    )
    sigs = pending_review_resolve.load_rejected_signatures(facts_dir)
    # sigs is a set of (subject, content_hash) tuples
    assert len(sigs) == 1
    sig = next(iter(sigs))
    assert sig[0] == "jane-doe"  # subject
    assert isinstance(sig[1], str) and len(sig[1]) > 0  # content hash


def test_load_rejected_signatures_empty_when_file_missing(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    assert pending_review_resolve.load_rejected_signatures(facts_dir) == set()


def test_signature_for_matches_load(tmp_path: Path):
    # The same {subject, content} pair must produce the same signature
    # whether computed by signature_for() or extracted from _rejected.md
    # by load_rejected_signatures().
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "_rejected.md").write_text(
        "# Rejected\n\n---\n\n"
        "- **id:** x\n"
        "- **subject:** jane-doe\n"
        "- **content:** Jane prefers tea over coffee.\n",
        encoding="utf-8",
    )
    sigs = pending_review_resolve.load_rejected_signatures(facts_dir)
    live_sig = pending_review_resolve.signature_for(
        subject="jane-doe", content="Jane prefers tea over coffee.",
    )
    assert live_sig in sigs
