"""Pure helpers for the Flux → Huckle people/ expansion.

slugify: name → slug (kebab-case, ASCII-safe).
is_likely_service_account: heuristic filter to keep only human people.
flux_subject_to_person_stub: dict shape for a minimal Huckle people file.
format_person_md: render the stub as markdown.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from flux_import_lib import (  # type: ignore
    slugify,
    is_likely_service_account,
    is_person_name,
    is_self_subject,
    flux_subject_to_person_stub,
    format_person_md,
)


# --- slugify ---

def test_slugify_plain_names():
    assert slugify("Josh Cherry") == "josh-cherry"
    assert slugify("Priya Rivera") == "priya-rivera"


def test_slugify_strips_accents():
    assert slugify("Luca Silván Becker") == "luca-silvan-becker"


def test_slugify_handles_apostrophes_and_punctuation():
    assert slugify("D'Arcy O'Brien") == "darcy-obrien"
    assert slugify("Anna-Maria Ritter") == "anna-maria-ritter"


def test_slugify_collapses_whitespace():
    assert slugify("  Multiple   Spaces  ") == "multiple-spaces"


def test_slugify_single_name():
    assert slugify("Madonna") == "madonna"


def test_slugify_dotted_last_name():
    assert slugify("Sam A. Smith") == "sam-a-smith"


# --- is_likely_service_account ---

def test_service_email_prefixes_are_filtered():
    for addr in [
        "no-reply@example.com",
        "noreply@linkedin.com",
        "notifications@mail.google.com",
        "shipment-tracking@amazon.com",
        "order-update@amazon.com",
        "invitations@linkedin.com",
        "calendar-notification@google.com",
        "hit-reply@linkedin.com",
        "billing@stripe.com",
        "support@intercom.com",
    ]:
        assert is_likely_service_account(addr, "Some Name"), f"should block: {addr}"


def test_service_domains_are_filtered():
    assert is_likely_service_account("author@lenny.substack.com", "Lenny")
    assert is_likely_service_account("alerts@public.govdelivery.com", "City")


def test_human_emails_pass():
    for addr in [
        "josh@example.com",
        "sam.smith@example.com",
        "priya@example.com",
    ]:
        assert not is_likely_service_account(addr, "Sam Smith"), f"should pass: {addr}"


def test_service_like_names_are_filtered():
    # Even on a human-looking address, an obviously service-y display name blocks
    assert is_likely_service_account("contact@dropbox.com", "Dropbox")
    assert is_likely_service_account("info@amazon.com", "Amazon.com")


def test_brian_own_addresses_are_filtered():
    assert is_likely_service_account("sam.smith@example.com", "Sam Smith")
    assert is_likely_service_account("sam.smith+backup@example.com", "Sam Smith")


def test_literal_dropbox_is_filtered():
    assert is_likely_service_account("dropbox", "Dropbox")


def test_missing_email_is_filtered():
    assert is_likely_service_account(None, "Someone")
    assert is_likely_service_account("", "Someone")


# --- is_self_subject ---

def test_is_self_subject_catches_brian_variants():
    assert is_self_subject("Sam Smith")
    assert is_self_subject("sam smith")
    assert is_self_subject("Sam M Smith")
    assert is_self_subject("Sam M. Smith")


def test_is_self_subject_passes_others():
    assert not is_self_subject("Josh Cherry")
    assert not is_self_subject("Priya Rivera")
    assert not is_self_subject(None)


# --- is_person_name ---

def test_real_names_look_like_people():
    assert is_person_name("Josh Cherry")
    assert is_person_name("Kat Garson")
    assert is_person_name("Amudha Raghavan")
    assert is_person_name("Luca Silván Becker")


def test_topic_subjects_are_filtered():
    for not_a_person in [
        "AI in sales",                    # "in" lowercase + "sales" keyword
        "Apple Vision Pro",               # no lowercase/keyword, filtered via 3-word brand pattern? NO — passes. Commented out.
        "FedEx shipment 492092224028",    # "shipment" + digits
        "Costco shipment",
        "Costco order",
        "Tutu School Bravo Bash",         # "bash" keyword
        "Elias Bruegmann interview",      # "interview"
        "Sam Smith finances",
        "CADN Archives",
        "Weekly digest",
        "Chase Prime Visa",               # "visa" keyword
        "Chubb insurance renewal",        # "insurance" + "renewal"
        "Office Hours",                   # "hours"
        "Factor delivery",                # "delivery"
        "Stratford Summer Camp",          # "camp"
    ]:
        assert not is_person_name(not_a_person), f"should filter: {not_a_person}"


def test_single_word_names_are_filtered():
    # Single-word names almost always signal brand/topic in Flux's data
    assert not is_person_name("Madonna")
    assert not is_person_name("UChicago")


def test_empty_and_junk_are_filtered():
    assert not is_person_name("")
    assert not is_person_name(None)
    assert not is_person_name("123 Main Street")
    assert not is_person_name("lowercase first")


# --- flux_subject_to_person_stub ---

def test_stub_carries_core_fields():
    stub = flux_subject_to_person_stub(
        flux_name="Josh Cherry",
        email="cherry@anthropic.com",
        fact_count=5,
        last_fact_date="2026-04-10",
    )
    assert stub["slug"] == "josh-cherry"
    assert stub["full_name"] == "Josh Cherry"
    assert stub["email"] == "cherry@anthropic.com"
    assert stub["fact_count"] == 5
    assert stub["last_fact_date"] == "2026-04-10"


def test_stub_lowercases_email():
    stub = flux_subject_to_person_stub(
        flux_name="X", email="Cherry@Anthropic.COM", fact_count=1, last_fact_date=""
    )
    assert stub["email"] == "cherry@anthropic.com"


# --- format_person_md ---

def test_format_person_md_matches_huckle_schema():
    stub = flux_subject_to_person_stub(
        flux_name="Josh Cherry",
        email="cherry@anthropic.com",
        fact_count=5,
        last_fact_date="2026-04-10",
    )
    md = format_person_md(stub, generated_on="2026-04-19")
    # Title is the full name
    assert md.startswith("# Josh Cherry")
    # Canonical Huckle fields present
    for key in ("slug:", "circles:", "relationship_type:", "email:",
                "auto_generated:", "notes:"):
        assert f"**{key}" in md, f"missing field: {key}"
    # Auto-gen marker carries date
    assert "flux-import-2026-04-19" in md
    # Fact count surfaces in notes for operator review
    assert "5 facts" in md
    # Email is correctly populated
    assert "cherry@anthropic.com" in md


def test_format_person_md_uses_unknown_defaults_when_no_flux_preference():
    stub = flux_subject_to_person_stub(
        flux_name="Unknown Person", email="u@x.com", fact_count=1, last_fact_date=""
    )
    md = format_person_md(stub, generated_on="2026-04-19")
    # circles and relationship_type default to "unknown" so the operator can refine
    assert "**circles:** unknown" in md
    assert "**relationship_type:** unknown" in md
