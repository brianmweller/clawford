#!/usr/bin/env python3
"""Tests for obsidian-briefing wikilink conversion."""

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "obsidian-briefing"
sys.path.insert(0, str(SCRIPTS_DIR))

from wikilinks import apply_wikilinks


class TestApplyWikilinks(unittest.TestCase):
    """Tests for converting known names to Obsidian [[wikilinks]]."""

    def test_known_name_becomes_wikilink(self):
        text = "Avery has gymnastics at 9:40"
        result = apply_wikilinks(text)
        self.assertIn("[[avery-smith-rivera|Avery]]", result)

    def test_unknown_name_unchanged(self):
        text = "Dr. Patel appointment at 2pm"
        result = apply_wikilinks(text)
        self.assertEqual(text, result)

    def test_multiple_names_in_text(self):
        text = "Sam picks up Avery, Alex takes Jordan"
        result = apply_wikilinks(text)
        self.assertIn("[[sam-smith|Sam]]", result)
        self.assertIn("[[avery-smith-rivera|Avery]]", result)
        self.assertIn("[[alex-rivera|Alex]]", result)
        self.assertIn("[[jordan-smith-rivera|Jordan]]", result)

    def test_name_at_word_boundary_only(self):
        """Should not match 'Brianson' or 'Evelina'."""
        text = "Brianson met Evelina at the park"
        result = apply_wikilinks(text)
        self.assertNotIn("[[sam-smith", result)
        self.assertNotIn("[[avery-smith-rivera", result)

    def test_marta_becomes_wikilink(self):
        text = "Jamie picks up at 3:30"
        result = apply_wikilinks(text)
        self.assertIn("[[jamie-park|Jamie]]", result)

    def test_does_not_double_link(self):
        """If text already contains a wikilink, don't re-link the display name."""
        text = "[[avery-smith-rivera|Avery]] and Avery's friend"
        result = apply_wikilinks(text)
        # The first Avery should stay as-is; the second should be linked
        # But we should not nest links: [[avery-smith-rivera|[[avery-smith-rivera|Avery]]]]
        self.assertNotIn("[[avery-smith-rivera|[[", result)


if __name__ == "__main__":
    unittest.main()
