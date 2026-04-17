"""Configuration for obsidian-briefing generator."""

from pathlib import Path

# --- VPS paths (defaults; overridden by generate_briefing() args in tests) ---

BRAIN_ROOT = Path.home() / "Dropbox" / "openclaw-backup"
MORNING_BRIEFING_PATH = (
    Path.home() / ".openclaw" / "family-calendar-workspace" / "cache" / "morning-briefing.txt"
)
FLEET_HEALTH_PATH = BRAIN_ROOT / "fleet-health.json"
COMMITMENTS_PATH = BRAIN_ROOT / "commitments" / "active.md"
TASKS_PATH = BRAIN_ROOT / "tasks" / "queue.md"
OUTPUT_DIR = BRAIN_ROOT / "obsidian" / "briefings"

# --- Agent roster (for status table) ---

AGENT_ROSTER = [
    {"name": "fix-it", "display": "Mr Fixit", "emoji": "🦊🔧"},
    {"name": "family-calendar", "display": "Mistress Mouse", "emoji": "🐭📅"},
    {"name": "news-digest", "display": "Lowly Worm", "emoji": "🐛📰"},
    {"name": "shopping", "display": "Hilda Hippo", "emoji": "🦛🛒"},
    {"name": "meetings-coach", "display": "Sergeant Murphy", "emoji": "🐷🔍"},
    {"name": "connector", "display": "Huckle Cat", "emoji": "🐱🤝"},
]

# --- Wikilink aliases: display name → people file slug ---

WIKILINK_ALIASES = {
    "Sam": "sam-smith",
    "Alex": "alex-rivera",
    "Avery": "avery-smith-rivera",
    "Jordan": "jordan-smith-rivera",
    "Jamie": "jamie-park",
    "Sharon": "sharon-chen",
    "Samuel": "samuel-chen",
}
