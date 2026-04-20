"""Pure helpers for facts-scope-augment.py.

Locates Huckle-native facts that don't yet carry an audience_scope tag,
builds a batch LLM prompt for classification, parses the response, and
rewrites the facts/YYYY-MM.md files in place with the new scope lines.
Idempotent — re-running against already-tagged facts is a no-op.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from agents.shared.facts import parse_facts_file
from agents.shared.fact_extraction import VALID_SCOPE_TAGS  # noqa: F401 — re-exported


def load_untagged_facts(facts_dir: Path) -> list[dict]:
    """Return all facts across facts_dir whose audience_scope is missing."""
    if not facts_dir.exists():
        return []
    out: list[dict] = []
    for path in sorted(facts_dir.glob("*.md")):
        for fact in parse_facts_file(path):
            if fact.get("audience_scope") is None:
                fact["_source_path"] = str(path)
                out.append(fact)
    return out


def build_scope_classifier_prompt(batch: list[dict]) -> str:
    """Build one LLM prompt that classifies all facts in the batch at once.
    Cheaper than one call per fact; LLM returns a JSON map {id: [tags]}."""
    fact_lines = []
    for f in batch:
        fact_lines.append(
            f"- id: {f.get('id')}\n"
            f"  subject: {f.get('subject')}\n"
            f"  category: {f.get('category', '')}\n"
            f"  content: {f.get('content', '')}\n"
        )
    facts_block = "\n".join(fact_lines) or "(empty batch)"

    valid = ", ".join(sorted(VALID_SCOPE_TAGS))

    return f"""Classify the audience_scope for each of these facts. Output JSON only.

audience_scope is a list of tags that gate which kind of recipient a fact
can be shared with. A fact tagged ["family"] may appear in drafts to
family members but must NOT appear in drafts to colleagues or vendors.
A fact tagged ["personal", "family"] is visible to BOTH personal
contacts AND family. A fact tagged ["professional"] is work-only.

Valid tags (pick 1-3 per fact): {valid}

Rules of thumb:
- Health, family composition, home, relationships → ["personal", "family"]
- Work role, employer, job search status, project scope → ["professional"]
- School info about kids → ["personal", "family"]
- Financial positions, estate planning → ["personal", "financial"]
- Information from a peer about work → ["professional"]
- Identity facts that are public (title, employer) → ["professional"]
- Identity facts that are intimate (health, beliefs, marital status) → ["personal"]
- If truly generic (harmless in any context), use ["public"].

FACTS
{facts_block}

OUTPUT a JSON object mapping each fact id to a list of 1-3 valid tags.
Example:
{{
  "f-001": ["personal", "family"],
  "f-002": ["professional"],
  "f-003": ["personal"]
}}
"""


def parse_scope_response(llm_text: str, fact_ids: set[str]) -> dict[str, list[str]]:
    """Parse the LLM's scope classification JSON. Drops unknown ids
    (hallucinated) and invalid tags; facts with no valid tags dropped.
    Returns {id: [valid_tags]} for facts whose classification can be trusted."""
    cleaned = (llm_text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    out: dict[str, list[str]] = {}
    for fid, tags in parsed.items():
        if fid not in fact_ids:
            continue
        if not isinstance(tags, list):
            continue
        valid_tags = [t for t in tags if isinstance(t, str) and t in VALID_SCOPE_TAGS]
        if valid_tags:
            out[fid] = valid_tags
    return out


_ID_LINE_RE = re.compile(r"^\s*-\s*\*\*id(?::\*\*|\*\*:)\s*(.+?)\s*$")


def rewrite_facts_file_with_scopes(path: Path, scope_map: dict[str, list[str]]) -> int:
    """Rewrite a facts/YYYY-MM.md file in place, inserting audience_scope
    lines into the blocks whose id appears in scope_map and doesn't already
    have a scope line. Returns the number of facts updated.

    Idempotent — blocks that already have audience_scope are left alone.
    Uses a tmp+replace atomic write.
    """
    if not path.exists():
        return 0
    original = path.read_text(encoding="utf-8")
    updates = 0
    out_blocks: list[str] = []
    for block in original.split("\n---\n"):
        stripped = block.strip()
        if not stripped:
            out_blocks.append(block)
            continue

        fact_id = None
        has_scope = False
        for line in block.splitlines():
            m = _ID_LINE_RE.match(line)
            if m:
                fact_id = m.group(1).strip()
            if line.strip().startswith("- **audience_scope:**") or \
               line.strip().startswith("- **audience_scope**:"):
                has_scope = True

        if fact_id and fact_id in scope_map and not has_scope:
            tags = scope_map[fact_id]
            scope_line = f"- **audience_scope:** {json.dumps(tags)}"
            block_lines = block.rstrip("\n").splitlines()
            # Insert scope line right after the last `- **key:**` line
            last_field_idx = -1
            for idx, line in enumerate(block_lines):
                if line.strip().startswith("- **"):
                    last_field_idx = idx
            if last_field_idx >= 0:
                block_lines.insert(last_field_idx + 1, scope_line)
            else:
                block_lines.append(scope_line)
            block = "\n".join(block_lines)
            if not block.endswith("\n"):
                block += "\n"
            updates += 1
        out_blocks.append(block)

    new_text = "\n---\n".join(out_blocks)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    return updates
