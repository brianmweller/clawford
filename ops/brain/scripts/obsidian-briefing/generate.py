#!/usr/bin/env python3
"""Generate daily Obsidian briefing from agent outputs and shared brain.

No LLM calls. Pure file I/O and string formatting. Zero cost.

Usage (VPS cron):
    python3 ~/Dropbox/openclaw-backup/scripts/obsidian-briefing/generate.py

Usage (local test):
    python3 scripts/obsidian-briefing/generate.py --dry-run
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from config import (
    AGENT_ROSTER,
    COMMITMENTS_PATH,
    FLEET_HEALTH_PATH,
    MORNING_BRIEFING_PATH,
    OUTPUT_DIR,
    TASKS_PATH,
)
from parsers import (
    parse_agent_status,
    parse_commitments,
    parse_morning_briefing,
    parse_tasks,
)
from wikilinks import apply_wikilinks


def generate_briefing(
    morning_briefing_path: Path = MORNING_BRIEFING_PATH,
    fleet_health_path: Path = FLEET_HEALTH_PATH,
    commitments_path: Path = COMMITMENTS_PATH,
    tasks_path: Path = TASKS_PATH,
    output_path: Path | None = None,
    briefing_date: date | None = None,
) -> str:
    """Generate the daily briefing markdown and write it to output_path.

    All parameters have defaults from config.py for VPS usage.
    Tests override them to use fixtures.
    """
    today = briefing_date or date.today()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_name = today.strftime("%A")
    month_day = today.strftime("%B %-d") if sys.platform != "win32" else today.strftime("%B %#d")

    # --- Gather data ---
    schedule_text = parse_morning_briefing(morning_briefing_path)
    agent_statuses = parse_agent_status(fleet_health_path)
    commitments = parse_commitments(commitments_path)
    tasks = parse_tasks(tasks_path)

    # --- Build sections ---
    sections = []

    # YAML frontmatter
    sections.append(f"""---
date: {today.isoformat()}
generated_at: {now}
type: daily-briefing
---""")

    # Title
    sections.append(f"# {day_name}, {month_day}")

    # Schedule
    schedule_display = apply_wikilinks(schedule_text)
    sections.append(f"## Schedule\n\n> From Mistress Mouse's 5:00 AM briefing\n\n{schedule_display}")

    # Agent Status
    status_lines = ["## Agent Status", ""]
    status_lines.append("| Agent | Status | Last Heartbeat |")
    status_lines.append("|-------|--------|---------------|")

    # Build lookup from parsed statuses
    status_map = {a["name"]: a for a in agent_statuses}
    for agent in AGENT_ROSTER:
        parsed = status_map.get(agent["name"])
        if parsed:
            status_lines.append(
                f"| {agent['emoji']} {agent['display']} "
                f"| {parsed['status']} | {parsed['last_heartbeat']} |"
            )
        else:
            status_lines.append(
                f"| {agent['emoji']} {agent['display']} | no data | — |"
            )
    sections.append("\n".join(status_lines))

    # Commitments
    commit_lines = ["## Open Commitments", ""]
    if commitments:
        for c in commitments:
            who = c.get("who", "?")
            to_whom = c.get("to_whom", "?")
            what = c.get("what", "?")
            by_when = c.get("by_when")
            deadline = f" (by {by_when})" if by_when else ""
            line = f"- **{who}** → {to_whom}: {what}{deadline}"
            commit_lines.append(apply_wikilinks(line))
    else:
        commit_lines.append("No open commitments.")
    sections.append("\n".join(commit_lines))

    # Tasks
    task_lines = ["## Open Tasks", ""]
    if tasks:
        for t in tasks:
            desc = t.get("description", "?")
            assignee = t.get("assignee", "?")
            due = t.get("due_date")
            due_str = f" (due {due})" if due else ""
            agent = t.get("source_agent", "")
            line = f"- {desc}{due_str} — assigned to {assignee} (via {agent})"
            task_lines.append(apply_wikilinks(line))
    else:
        task_lines.append("No open tasks.")
    sections.append("\n".join(task_lines))

    # --- Assemble ---
    output = "\n\n".join(sections) + "\n"

    # --- Write ---
    if output_path is None:
        output_path = OUTPUT_DIR / f"{today.isoformat()}.md"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")

    return output


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    result = generate_briefing()
    if dry_run:
        print(result)
