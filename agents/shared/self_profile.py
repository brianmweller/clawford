"""agents/shared/self_profile.py — load the operator's professional brain
(profile.md + structured facts) for fleet-wide consumption.

Called by Huckle's compose (recruiter-draft fit-eval) and Murphy's
meeting-prep (recruiter-meeting context). Exposes a SelfProfile
dataclass with the load-bearing fields plus convenience accessors
(current_targets, level_bar_text).

Design notes:
  - No writes. Brain files are hand-curated or produced by the
    connector's profile-synthesize / structured-facts-extract flows.
  - Graceful missing-file handling: if self/ doesn't exist yet,
    returns a SelfProfile with None/empty fields rather than raising.
    Callers branch on profile.profile_md being None.
  - Single-shot read; no caching. The brain is small (<100KB total),
    and consumers are per-compose / per-meeting-prep so repeat
    reads are bounded.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from agents.shared.brain import dropbox_brain_root
except ImportError:
    dropbox_brain_root = None   # type: ignore


# Fact types we expect under self/facts/
_FACT_TYPES = (
    "employer",
    "target_company",
    "strength_theme",
    "major_accomplishment",
    "tenet_authored",
)


@dataclass
class SelfProfile:
    """Loaded snapshot of the operator's professional brain.

    All fields have safe defaults so consumers can opt-in per section
    and not care about missing data.
    """

    profile_md: str | None = None
    level_bar_text: str = ""
    employer_history: list[dict] = field(default_factory=list)
    target_companies: list[dict] = field(default_factory=list)
    strength_themes: list[dict] = field(default_factory=list)
    major_accomplishments: list[dict] = field(default_factory=list)
    tenets_authored: list[dict] = field(default_factory=list)
    active_search_pipeline: list[dict] = field(default_factory=list)
    recently_concluded_searches: list[dict] = field(default_factory=list)

    @property
    def current_targets(self) -> list[dict]:
        """Target companies with open outcomes — excludes landed/declined."""
        open_outcomes = {"targeted", "in-progress", "in_progress", ""}
        return [
            r for r in self.target_companies
            if r.get("outcome", "").lower() in open_outcomes
        ]

    @property
    def has_profile(self) -> bool:
        return bool(self.profile_md)

    @property
    def late_stage_searches(self) -> list[dict]:
        """Searches in late-funnel stages — useful urgency signal for
        Huckle's cold-recruiter drafting ('I'm in late stages with
        others' context that otherwise lives only in the operator's head)."""
        late_stages = {"onsite-panel", "final-round", "offer",
                       "technical-interview", "hiring-manager"}
        return [s for s in self.active_search_pipeline
                if s.get("stage") in late_stages]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_self_profile(self_dir: Path | None = None) -> SelfProfile:
    """Load the operator's professional brain from the Dropbox self/ directory.

    self_dir defaults to ~/Dropbox/openclaw-backup/self/ (via
    dropbox_brain_root). Pass a tmp_path in tests.
    """
    if self_dir is None:
        if dropbox_brain_root is None:
            return SelfProfile()
        self_dir = dropbox_brain_root() / "self"

    profile = SelfProfile()

    profile_path = self_dir / "profile.md"
    if profile_path.exists():
        try:
            profile.profile_md = profile_path.read_text(encoding="utf-8")
            profile.level_bar_text = extract_level_bar_section(profile.profile_md)
        except OSError:
            pass

    facts_dir = self_dir / "facts"
    if facts_dir.exists():
        for fact_type in _FACT_TYPES:
            records = _load_fact_file(facts_dir / f"{fact_type}.json")
            _assign_facts(profile, fact_type, records)

        # Search-status layer — produced by search-status-build.py
        stages_path = facts_dir / "active_search_stages.json"
        if stages_path.exists():
            try:
                raw = json.loads(stages_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    profile.active_search_pipeline = raw.get("active_searches") or []
                    profile.recently_concluded_searches = raw.get("recently_concluded") or []
            except (json.JSONDecodeError, OSError):
                pass

    return profile


def _load_fact_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict) and "records" in data:
        recs = data["records"]
    elif isinstance(data, list):
        recs = data
    else:
        return []
    return [r for r in recs if isinstance(r, dict)]


def _assign_facts(profile: SelfProfile, fact_type: str, records: list[dict]) -> None:
    if fact_type == "employer":
        profile.employer_history = records
    elif fact_type == "target_company":
        profile.target_companies = records
    elif fact_type == "strength_theme":
        profile.strength_themes = records
    elif fact_type == "major_accomplishment":
        profile.major_accomplishments = records
    elif fact_type == "tenet_authored":
        profile.tenets_authored = records


# ---------------------------------------------------------------------------
# Section extractors (for prompt composition)
# ---------------------------------------------------------------------------


_LEVEL_BAR_HEADING_RE = re.compile(
    r"^##\s+Level\s*&?\s*scope\s+bar\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_NEXT_H2_RE = re.compile(r"^##\s+", flags=re.MULTILINE)


def extract_level_bar_section(profile_md: str | None) -> str:
    """Pull the 'Level & scope bar' section body out of profile.md, or
    return empty string if not present. Used by Huckle's fit-check to
    cite the operator's explicit role-shape filter."""
    if not profile_md:
        return ""
    m = _LEVEL_BAR_HEADING_RE.search(profile_md)
    if not m:
        return ""
    start = m.end()
    # Find the next h2 heading after this section's start
    next_m = _NEXT_H2_RE.search(profile_md, pos=start)
    end = next_m.start() if next_m else len(profile_md)
    return profile_md[start:end].strip()
