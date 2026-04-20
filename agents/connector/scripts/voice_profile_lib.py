"""Pure helpers for voice-profile-build.py.

Voice profile building is a one-time style-extraction pass: the operator's sent
emails to people in each social circle are sampled, fed to an LLM, and
the LLM returns a structured JSON profile describing his characteristic
voice for that circle (greeting, signoff, register, patterns, etc.).

The profile is loaded by voice.py at compose time so drafts can match
the operator's actual style even when the per-thread history anchor is thin
(e.g. new contacts, cold outreach).
"""
from __future__ import annotations

import json
import re
from pathlib import Path


_FIELD_RE = re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")


REQUIRED_PROFILE_FIELDS = (
    "typical_greeting",
    "typical_signoff",
    "register",
    "common_patterns",
    "anti_patterns",
    "sample_opening_phrases",
    "distinctive_traits",
)


def bucket_people_by_circle(people_dir: Path) -> dict[str, list[str]]:
    """Read every people/*.md (skipping _template.md etc.) and return a
    dict of circle → sorted unique list of emails in that circle.

    Skips people whose email is missing or placeholder ("—"), and skips
    the "unknown" circle (that's the auto-generated-stub placeholder;
    those contacts have sparse or miscalibrated data).
    """
    out: dict[str, set[str]] = {}
    if not people_dir.exists():
        return {}
    for path in people_dir.glob("*.md"):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        email = None
        circles_raw = None
        for line in text.splitlines():
            m = _FIELD_RE.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if key == "email" and val and val not in {"—", "-", ""}:
                email = val.lower()
            elif key == "circles":
                circles_raw = val
        if not email or "@" not in email:
            continue
        if not circles_raw:
            continue
        for c in [c.strip() for c in circles_raw.split(",")]:
            if not c or c == "unknown":
                continue
            out.setdefault(c, set()).add(email)
    return {k: sorted(v) for k, v in out.items()}


_PROFILE_EXTRACTION_SYSTEM = """\
You are a writing-style analyst. Given N emails a person sent, extract their
distinctive writing voice as structured JSON. Focus on what's MOST
characteristic — patterns a stranger could use to identify his writing.

Be specific. "Uses contractions" is weak; "uses contractions in every
clause, never writes 'I am'" is useful. Cite actual patterns from the
samples, not generic writing-advice platitudes.
"""


def build_profile_extraction_prompt(circle: str, samples: list[dict]) -> str:
    """Construct the LLM prompt to extract a voice profile from samples.
    samples: list of {to, date, body}.
    """
    sample_blocks = []
    for i, s in enumerate(samples, 1):
        to = s.get("to", "?")
        date = s.get("date", "?")
        body = (s.get("body") or "").strip()
        sample_blocks.append(
            f"--- SAMPLE {i} ---\n"
            f"To: {to}\n"
            f"Date: {date}\n"
            f"Body:\n{body}\n"
        )
    samples_text = "\n".join(sample_blocks) if sample_blocks else "(no samples)"

    schema = """\
Respond with a JSON object with EXACTLY these fields:
{
  "typical_greeting":       "<the opening phrase the operator most often uses, e.g. 'Hey {name},' or 'Hi all --'. If varies by recipient type, pick the dominant.>",
  "typical_signoff":        "<how the operator signs off: 'Love, the operator' vs 'Best, the operator' vs 'the operator' alone, etc.>",
  "register":               "<intimate | casual | consultative | formal — single word>",
  "common_patterns":        ["<3-5 specific patterns — contraction rate, em-dash frequency, sentence length, warmth markers, punctuation quirks>"],
  "anti_patterns":          ["<2-3 things the operator AVOIDS — what's notable by its ABSENCE>"],
  "sample_opening_phrases": ["<3-5 specific opening phrases the operator frequently uses, copied verbatim from samples>"],
  "distinctive_traits":     "<1-2 sentences: what makes this voice recognizably the operator's for this circle?>"
}
"""
    return (
        f"{_PROFILE_EXTRACTION_SYSTEM}\n"
        f"CIRCLE: {circle}\n"
        f"SAMPLE COUNT: {len(samples)}\n\n"
        f"{samples_text}\n\n"
        f"{schema}"
    )


def _strip_json_fences(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return stripped


def parse_profile_response(llm_text: str) -> dict:
    """Parse the LLM's JSON profile response. Returns the validated dict,
    or {"error": "..."} if malformed or missing required fields."""
    cleaned = _strip_json_fences(llm_text)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return {"error": "llm returned non-json", "raw": llm_text}
    if not isinstance(parsed, dict):
        return {"error": "llm returned non-object", "raw": llm_text}
    missing = [f for f in REQUIRED_PROFILE_FIELDS if f not in parsed or parsed.get(f) in (None, "", [])]
    if missing:
        return {"error": f"missing required fields: {missing}", "raw": llm_text}
    return parsed
