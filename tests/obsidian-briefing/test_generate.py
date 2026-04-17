#!/usr/bin/env python3
"""Integration tests for obsidian-briefing generate.py."""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "obsidian-briefing"
sys.path.insert(0, str(SCRIPTS_DIR))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

from generate import generate_briefing


class TestGenerateBriefing(unittest.TestCase):
    """Integration tests: fixtures in → markdown out."""

    def _generate(self, morning_briefing="morning-briefing-normal.txt",
                  fleet_health="fleet-health.json",
                  commitments="commitments-active.md",
                  tasks="tasks-queue.md"):
        """Helper to generate a briefing from fixtures."""
        outdir = tempfile.mkdtemp()
        outfile = Path(outdir) / "test-briefing.md"
        generate_briefing(
            morning_briefing_path=FIXTURES / morning_briefing,
            fleet_health_path=FIXTURES / fleet_health,
            commitments_path=FIXTURES / commitments,
            tasks_path=FIXTURES / tasks,
            output_path=outfile,
            briefing_date=date(2026, 4, 8),
        )
        return outfile.read_text(encoding="utf-8")

    def test_has_yaml_frontmatter(self):
        output = self._generate()
        self.assertTrue(output.startswith("---\n"))
        self.assertIn("date: 2026-04-08", output)
        self.assertIn("type: daily-briefing", output)

    def test_has_all_sections(self):
        output = self._generate()
        self.assertIn("## Schedule", output)
        self.assertIn("## Agent Status", output)
        self.assertIn("## Open Commitments", output)
        self.assertIn("## Open Tasks", output)

    def test_has_schedule_content(self):
        output = self._generate()
        self.assertIn("9:30", output)
        self.assertIn("Deer Hollow", output)

    def test_has_agent_status_table(self):
        output = self._generate()
        self.assertIn("healthy", output)
        self.assertIn("degraded", output)

    def test_has_commitments(self):
        output = self._generate()
        self.assertIn("Send the revised proposal", output)
        self.assertIn("Deer Hollow parking", output)
        # Should NOT include the completed commitment
        self.assertNotIn("Book restaurant for anniversary", output)

    def test_has_tasks(self):
        output = self._generate()
        self.assertIn("Confirm nanny availability", output)
        # Should NOT include the done task
        self.assertNotIn("thank-you note to Alice", output)

    def test_graceful_degradation_missing_briefing(self):
        """Missing morning briefing → other sections still render."""
        output = self._generate(morning_briefing="nonexistent.txt")
        self.assertIn("## Schedule", output)
        self.assertIn("no schedule data", output.lower())
        # Other sections should still be present
        self.assertIn("## Agent Status", output)
        self.assertIn("## Open Commitments", output)

    def test_graceful_degradation_empty_brain(self):
        """Empty commitments + tasks → sections present but no items."""
        output = self._generate(commitments="commitments-empty.md",
                                tasks="tasks-empty.md")
        self.assertIn("## Open Commitments", output)
        self.assertIn("## Open Tasks", output)

    def test_idempotent(self):
        """Running twice produces identical output."""
        output1 = self._generate()
        output2 = self._generate()
        self.assertEqual(output1, output2)

    def test_wikilinks_in_schedule(self):
        """Known names in the schedule should become wikilinks."""
        output = self._generate()
        self.assertIn("[[avery-smith-rivera|Avery]]", output)

    def test_wikilinks_in_commitments(self):
        """Known names in commitments should become wikilinks."""
        output = self._generate()
        # Sharon appears in the open commitment "Send Deer Hollow parking info"
        self.assertIn("[[sharon-chen|Sharon]]", output)


if __name__ == "__main__":
    unittest.main()
