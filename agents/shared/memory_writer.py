"""agents/shared/memory_writer.py — persist new rules into MEMORY.md.

Each agent's MEMORY.md is loaded into its system prompt on every
conversation (see dispatcher._build_system_prompt). Without a writer,
rules like "always get 2 gallons of milk" die with the 20-turn
conversation window. This module appends them durably under a category
heading so future conversations see the rule.

Contract:
    append_rule(agent_id, rule, category) -> dict
        Append `rule` to agents/<agent_id>/MEMORY.md under `## <category>`,
        creating the file, category, or agent dir as needed.
        Returns {"status": "ok", "agent_id": ..., "category": ..., "rule": ...}
        on success, {"status": "error", "error": ...} on failure.

Not LLM-callable directly — wrapped by each agent's propose_remember
tool behind the pending-action + inline-button confirmation pattern,
so a prompt injection can't silently poison memory.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def _repo_root() -> Path:
    override = os.environ.get("CLAWFORD_REPO_ROOT")
    if override:
        return Path(override)
    # Fallback: this file is at agents/shared/, so parents[2] = repo root
    return Path(__file__).resolve().parents[2]


def _memory_path(agent_id: str) -> Path:
    return _repo_root() / "agents" / agent_id / "MEMORY.md"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def append_rule(agent_id: str, rule: str, category: str) -> dict:
    """Append a rule to the agent's MEMORY.md under the given category.

    Creates the file and/or category heading as needed. Preserves all
    existing content verbatim — rules are never rewritten, only added.
    """
    if not rule or not rule.strip():
        return {"status": "error", "error": "rule cannot be empty"}
    if not category or not category.strip():
        return {"status": "error", "error": "category cannot be empty"}

    rule = rule.strip()
    category = category.strip()
    path = _memory_path(agent_id)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"status": "error", "error": f"could not create agent dir: {exc}"}

    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"status": "error", "error": f"could not read MEMORY.md: {exc}"}
    else:
        existing = f"# MEMORY.md — {agent_id}\n\nPersistent lessons learned from experience.\n"

    heading = f"## {category}"
    bullet = f"- ({_timestamp()}) {rule}"

    if heading in existing:
        # Append under existing heading — find the next blank line or
        # next ## heading after it, insert before that.
        lines = existing.splitlines()
        new_lines: list[str] = []
        inserted = False
        in_target_section = False
        for i, line in enumerate(lines):
            new_lines.append(line)
            if line.strip() == heading:
                in_target_section = True
                continue
            if in_target_section and not inserted:
                # Insert when we hit the next ## heading (end of section)
                # or at end of file.
                next_is_heading = (
                    i + 1 < len(lines) and lines[i + 1].startswith("## ")
                )
                if next_is_heading:
                    new_lines.append(bullet)
                    inserted = True
                    in_target_section = False
        if not inserted:
            # Section was at end of file — just append at the end
            new_lines.append(bullet)
        updated = "\n".join(new_lines)
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        # New category — append heading + rule at end
        sep = "" if existing.endswith("\n") else "\n"
        if not existing.endswith("\n\n"):
            sep = "\n" if existing.endswith("\n") else "\n\n"
        updated = existing + sep + f"\n{heading}\n\n{bullet}\n"

    try:
        path.write_text(updated, encoding="utf-8")
    except (OSError, PermissionError) as exc:
        return {"status": "error", "error": f"write failed: {exc}"}

    return {
        "status": "ok",
        "agent_id": agent_id,
        "category": category,
        "rule": rule,
        "memory_path": str(path),
    }


def propose_pending_remember(agent_id: str, rule: str, category: str = "General") -> dict:
    """Stage a 'remember' action. The dispatcher auto-attaches
    [💾 Remember] [Skip] buttons. On confirm, the rule appends to
    MEMORY.md. Used by each agent's propose_remember tool."""
    import pending_actions  # local import to avoid circular deps
    return pending_actions.stage(
        agent_id, "remember",
        {"rule": rule, "category": category},
        f"Remember: '{rule}' under '{category}'",
        confirm_label="\U0001f4be Remember",
        cancel_label="Skip",
    )
