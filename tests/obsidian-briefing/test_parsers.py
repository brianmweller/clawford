#!/usr/bin/env python3
"""Tests for obsidian-briefing parsers."""

import sys
import unittest
from pathlib import Path

# Add the scripts directory to path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "obsidian-briefing"
sys.path.insert(0, str(SCRIPTS_DIR))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

from parsers import (
    parse_morning_briefing,
    parse_agent_status,
    parse_commitments,
    parse_tasks,
)


class TestParseMorningBriefing(unittest.TestCase):
    """Tests for parsing Mistress Mouse's morning briefing cache."""

    def test_normal_briefing_extracts_content(self):
        result = parse_morning_briefing(FIXTURES / "morning-briefing-normal.txt")
        # Should return the briefing text (stripped of Telegram formatting chars)
        self.assertIn("9:30", result)
        self.assertIn("Avery", result)
        self.assertIn("Deer Hollow", result)
        self.assertIn("MORNING", result)
        self.assertIn("AFTERNOON", result)
        # Box-drawing characters should be stripped
        self.assertNotIn("━", result)

    def test_error_briefing_returns_error_message(self):
        result = parse_morning_briefing(FIXTURES / "morning-briefing-error.txt")
        self.assertIn("Couldn't fetch calendars", result)

    def test_missing_file_returns_fallback(self):
        result = parse_morning_briefing(FIXTURES / "nonexistent-file.txt")
        self.assertIn("no schedule data", result.lower())

    def test_normal_briefing_strips_header_line(self):
        """The '🐭📅 Family Day' header is Telegram-specific; should be stripped."""
        result = parse_morning_briefing(FIXTURES / "morning-briefing-normal.txt")
        self.assertNotIn("🐭📅 Family Day", result)

    def test_normal_briefing_strips_footer(self):
        """The '🐭 3 events · Pickup: Jamie' footer is Telegram-specific."""
        result = parse_morning_briefing(FIXTURES / "morning-briefing-normal.txt")
        self.assertNotIn("🐭 3 events", result)


class TestParseAgentStatus(unittest.TestCase):
    """Tests for parsing agent status files."""

    def test_parses_healthy_agent(self):
        agents = parse_agent_status(FIXTURES)
        fix_it = next(a for a in agents if a["name"] == "fix-it")
        self.assertEqual(fix_it["status"], "healthy")
        self.assertEqual(fix_it["last_heartbeat"], "2026-04-08T12:00:00Z")

    def test_parses_degraded_agent(self):
        agents = parse_agent_status(FIXTURES)
        shopping = next(a for a in agents if a["name"] == "shopping")
        self.assertEqual(shopping["status"], "degraded")

    def test_returns_all_agents(self):
        agents = parse_agent_status(FIXTURES)
        names = {a["name"] for a in agents}
        self.assertEqual(names, {"fix-it", "family-calendar", "news-digest", "shopping"})

    def test_empty_directory_returns_empty_list(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            agents = parse_agent_status(Path(tmpdir))
            self.assertEqual(agents, [])


class TestParseCommitments(unittest.TestCase):
    """Tests for parsing commitments/active.md."""

    def test_open_commitments_only(self):
        commitments = parse_commitments(FIXTURES / "commitments-active.md")
        # Should return only open commitments, not completed
        self.assertEqual(len(commitments), 2)
        for c in commitments:
            self.assertEqual(c["status"], "open")

    def test_commitment_fields(self):
        commitments = parse_commitments(FIXTURES / "commitments-active.md")
        bob = next(c for c in commitments if "Bob" in c["who"])
        self.assertEqual(bob["who"], "Bob Martinez")
        self.assertEqual(bob["to_whom"], "me")
        self.assertEqual(bob["what"], "Send the revised proposal")
        self.assertEqual(bob["by_when"], "2026-04-07")

    def test_empty_commitments(self):
        commitments = parse_commitments(FIXTURES / "commitments-empty.md")
        self.assertEqual(commitments, [])

    def test_missing_file_returns_empty(self):
        commitments = parse_commitments(FIXTURES / "nonexistent.md")
        self.assertEqual(commitments, [])


class TestParseTasks(unittest.TestCase):
    """Tests for parsing tasks/queue.md."""

    def test_open_tasks_only(self):
        tasks = parse_tasks(FIXTURES / "tasks-queue.md")
        # Should return only open tasks, not done
        self.assertEqual(len(tasks), 2)
        for t in tasks:
            self.assertEqual(t["status"], "open")

    def test_task_fields(self):
        tasks = parse_tasks(FIXTURES / "tasks-queue.md")
        nanny = next(t for t in tasks if "nanny" in t["description"].lower())
        self.assertEqual(nanny["assignee"], "me")
        self.assertEqual(nanny["due_date"], "2026-04-09")
        self.assertEqual(nanny["source_agent"], "family-calendar")

    def test_empty_tasks(self):
        tasks = parse_tasks(FIXTURES / "tasks-empty.md")
        self.assertEqual(tasks, [])

    def test_missing_file_returns_empty(self):
        tasks = parse_tasks(FIXTURES / "nonexistent.md")
        self.assertEqual(tasks, [])


if __name__ == "__main__":
    unittest.main()
