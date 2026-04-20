"""Pure helpers for workflowy-facts-mine.py.

Builds a name→slug index from brain/people/ and detects which known
people (if any) are mentioned by full name in a Workflowy node's text.
No network — the HTTP calls live in the orchestrator.
"""
from __future__ import annotations

import re
from pathlib import Path


_FULL_NAME_RE = re.compile(r"^\s*-\s*\*\*full_name(?::\*\*|\*\*:)\s*(.+?)\s*$")
_SLUG_RE = re.compile(r"^\s*-\s*\*\*slug(?::\*\*|\*\*:)\s*(.+?)\s*$")


def build_name_to_slug_index(people_dir: Path) -> dict[str, str]:
    """Return a lowercase full_name → slug map for every person file.
    Files starting with '_' (templates) are skipped."""
    if not people_dir.exists():
        return {}
    out: dict[str, str] = {}
    for path in people_dir.glob("*.md"):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        name = None
        slug = None
        for line in text.splitlines():
            if name is None:
                m = _FULL_NAME_RE.match(line)
                if m:
                    name = m.group(1).strip()
            if slug is None:
                m = _SLUG_RE.match(line)
                if m:
                    slug = m.group(1).strip()
            if name and slug:
                break
        if not slug:
            slug = path.stem
        if name:
            out[name.lower()] = slug
    return out


def find_mentioned_slugs(text: str | None, name_to_slug: dict[str, str]) -> set[str]:
    """Return the set of slugs whose full_name appears in ``text`` as a
    whole-word case-insensitive match. Whole-word guard avoids the
    'Sarah' → 'Sarah Chen' false positive.
    """
    if not text:
        return set()
    lowered = text.lower()
    out: set[str] = set()
    for name, slug in name_to_slug.items():
        # Use word-boundary regex. Names may contain spaces and
        # apostrophes; re.escape handles these safely.
        pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, lowered):
            out.add(slug)
    return out
