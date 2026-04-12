"""
holiday_card.py — Load Sam's holiday card list as a curated signal.

The CSV at ~/Downloads/Holiday card List - Sheet1.csv is a manually curated
list of 120 people Sam considers important enough to send a holiday card.
Used by llm-enrich as a hard "not an acquaintance" signal.

Some entries have email only, some have name only (phone contacts), some
have both. We match against contacts using both lookups.
"""

import csv
import os
import re
from pathlib import Path

HOLIDAY_CARD_CSV = Path(os.path.expanduser(
    "~/Downloads/Holiday card List - Sheet1.csv"
))


def _normalize_name(name):
    """Lowercase + strip whitespace for lookup keys."""
    return (name or "").strip().lower()


def _normalize_email(email):
    return (email or "").strip().lower()


def load_holiday_card():
    """Return (email_set, name_set) from the holiday card CSV.

    email_set: set of lowercased, trimmed email addresses
    name_set:  set of lowercased, trimmed full names

    Returns empty sets if the CSV is missing (non-fatal).
    """
    email_set = set()
    name_set = set()

    if not HOLIDAY_CARD_CSV.exists():
        return email_set, name_set

    with open(HOLIDAY_CARD_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = _normalize_name(row.get("Name"))
            email = _normalize_email(row.get("Email"))
            if email:
                email_set.add(email)
            if name:
                name_set.add(name)

    return email_set, name_set


def is_on_list(contact, email_set, name_set):
    """Return True if the contact matches a holiday card entry."""
    email = _normalize_email(contact.get("email"))
    if email and email in email_set:
        return True

    name = _normalize_name(contact.get("name"))
    if name and name in name_set:
        return True

    # Check display_names too — SMS contacts often carry phone contact names
    for dn in contact.get("display_names", []) or []:
        if _normalize_name(dn) in name_set:
            return True

    return False
