"""people — helpers for updating `brain/people/<slug>.md` cards.

Primary helper: ``append_observation()`` adds one bullet under a
``## Recent observations`` section on a person's card. Miners call
this for high-confidence facts (>= 0.7) so the card stays in sync with
the facts stream as a human-readable summary.

Why append, not structured-field upsert: people cards carry a small
set of curated fields (slug, circles, social_distance, notes). Miners
shouldn't rewrite those — too easy to drift. A running observation log
surfaces "what's new about Sarah" at a glance without duplicating
durable storage (facts remain authoritative in ``brain/facts/``).

Invariants:
- Append-only; the section is trimmed to the 10 most-recent bullets.
- Atomic tmp+replace so a crash mid-write doesn't truncate the card.
- Skipped silently (``status=skipped``) when the person file doesn't
  exist; miners shouldn't auto-create cards from a single observation.
"""
from __future__ import annotations

import re
from pathlib import Path


SECTION_HEADER = "## Recent observations"
MAX_OBSERVATIONS_RETAINED = 10


def append_observation(
    *,
    people_dir: Path,
    slug: str,
    content: str,
    source: str,
    timestamp: str,
) -> dict:
    """Add one observation bullet to ``people_dir/<slug>.md`` under
    ``## Recent observations``. Creates the section if absent; trims
    to the 10 most-recent bullets. Atomic write.

    Returns ``{"status": "appended"}`` on success, or
    ``{"status": "skipped", "reason": "person file absent"}`` when the
    target card doesn't exist.
    """
    path = people_dir / f"{slug}.md"
    if not path.exists():
        return {"status": "skipped", "reason": "person file absent"}

    text = path.read_text(encoding="utf-8")
    bullet = f"- {timestamp} · {content} (source: {source})"

    new_text = _insert_or_append_bullet(text, bullet)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    return {"status": "appended", "path": str(path)}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _insert_or_append_bullet(text: str, bullet: str) -> str:
    """Return a new card body with `bullet` added to the Recent
    observations section. Section bullets are trimmed to
    MAX_OBSERVATIONS_RETAINED (most recent retained)."""
    lines = text.splitlines()

    # Locate the section and its bullet range.
    section_idx = _find_header_line(lines, SECTION_HEADER)

    if section_idx is None:
        # No section yet — append to end.
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(SECTION_HEADER)
        lines.append(bullet)
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    # Section exists. Collect existing bullets (until next blank H2 or EOF)
    # and rewrite the bullet range with trim applied.
    bullets_start = section_idx + 1
    bullets_end = bullets_start
    while bullets_end < len(lines):
        ln = lines[bullets_end]
        stripped = ln.strip()
        if stripped.startswith("## ") and stripped != SECTION_HEADER:
            break
        if stripped.startswith("- "):
            bullets_end += 1
            continue
        # Blank line: keep scanning — the section's bullets may have
        # blank separators before the next H2.
        if stripped == "":
            # Peek ahead: if next non-blank is another H2, stop here.
            peek = bullets_end + 1
            while peek < len(lines) and lines[peek].strip() == "":
                peek += 1
            if peek < len(lines) and lines[peek].strip().startswith("## "):
                break
            # Otherwise, treat the blank as EOF of bullets.
            break
        # Non-bullet, non-header text inside the section — leave it in
        # place by stopping here.
        break

    existing_bullets = [
        ln for ln in lines[bullets_start:bullets_end]
        if ln.strip().startswith("- ")
    ]
    existing_bullets.append(bullet)
    trimmed = existing_bullets[-MAX_OBSERVATIONS_RETAINED:]

    new_lines = lines[:bullets_start] + trimmed + lines[bullets_end:]
    return "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")


def _find_header_line(lines: list[str], header: str) -> int | None:
    for i, ln in enumerate(lines):
        if ln.strip() == header:
            return i
    return None
