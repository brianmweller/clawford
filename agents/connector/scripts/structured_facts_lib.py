"""structured_facts_lib — per-type extraction of typed self-facts from
the Stage 3 role/search-round archives.

Consumers (Huckle recruiter drafting, Murphy recruiter prep) query
JSON files under self/facts/:
  employers.json, target_companies.json, strength_themes.json,
  major_accomplishments.json, tenets_authored.json

Each file is a list of records with a common envelope:
  {id, type, source_archives, confidence, extracted_at}
plus type-specific payload fields.

Pure helpers — the runner owns LLM I/O and filesystem writes.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone


# ----------------------------------------------------------------------
# Schema: per-type required + optional payload fields
# ----------------------------------------------------------------------


_SCHEMAS: dict[str, dict] = {
    "employer": {
        "required": ["company", "title", "start", "end"],
        "optional": ["reporting_line", "team_scope", "departure_context", "confidence"],
        "id_fields": ["company"],
        "description": (
            "A specific employer tenure with title, dates, reporting line, "
            "team scope. Emit EXACTLY ONE record per employer — never "
            "split the same tenure into multiple records with different "
            "titles or phrasings.\n\n"
            "RULES:\n"
            "- Use titles as they would appear on the operator's CV/LinkedIn, not "
            "  paraphrased descriptive titles the LLM invents. Prefer a "
            "  specific title from bio/CV evidence over a generalized "
            "  description. If the archive mentions multiple phrasings "
            "  for the same tenure, pick the most CV-appropriate one "
            "  (e.g., 'Director, Flagship Data / FLEX DS' NOT 'Lead Data "
            "  Science for Consumer Experience').\n"
            "- For team_scope, restate the SPECIFIC number where archives "
            "  show it (e.g., 'about 30' → team_scope: '~30'). Do NOT "
            "  paraphrase '30' as 'in the thirties'.\n"
            "- When the archive mentions team growth (e.g., doubled in "
            "  H1 2020), include it.\n"
            "- For 'end': use the LAST DAY INCLUSIVE of the tenure "
            "  (e.g., LinkedIn ending in Dec 2023 → end: '2023-12-31', "
            "  NOT '2024-01-01'). If only month-level granularity is "
            "  available, use end-of-month (e.g., '2023-12-31')."
        ),
        "example": {
            "company": "Amazon",
            "title": "Head of Science, Worldwide Marketplace Science (WMS)",
            "start": "2020-09-01",
            "end": "2022-04-01",
            "reporting_line": "Jay Marine (VP Video)",
            "team_scope": "~30 across decision science, production science, and engineering",
            "departure_context": "Transition out of WMS with onboarding materials for new manager",
            "confidence": 0.95,
        },
    },
    "target_company": {
        "required": ["company", "search_round"],
        "optional": ["role_type", "tier_company", "tier_opportunity", "outcome", "notes", "confidence"],
        "id_fields": ["company", "search_round"],
        "description": (
            "A company the operator explored as a target in a specific search round. "
            "Captures what role shape he was pitching and what happened.\n\n"
            "RULES:\n"
            "- INCLUDE EVERY company that appears as a distinct target in "
            "  any search-round archive — even if only briefly mentioned. "
            "  The Interview Prep folder structure implies these companies, "
            "  all of which should get target_company records if any of "
            "  their content appears in the archives:\n"
            "    Example Corp, Amazon Core AI, Amazon Finance, Amazon Prime Video, "
            "    Amazon SEAS (2025), Anthropic, Apple, Asana, Babylist, Calm, "
            "    Chainlink Labs, Coca-Cola, Coupang, Databricks, DeepMind, "
            "    DoorDash, DoorDash (Wolt), Duolingo, Epic Games, Exec Searches, "
            "    Facebook, Facebook (2021), Gap, Google, Google (2026), HBS, "
            "    Indeed, Kindred, King Games, LinkedIn, Lyft, ML Systems, "
            "    Microsoft AI for Good, Mr Beast, NBCUniversal, Netflix, "
            "    Netflix (Pricing), Netflix GenAI, Outschool, Pinterest, "
            "    Quizlet, Riot Games, Roblox, Shipt, Shopify, Snap, Twitch, "
            "    Uber, Uber (2026), Unity, Upwork, ViacomCBS, Wayfair, "
            "    Wayfair (2025), Western Digital, YouTube\n"
            "- Bin each to the right search_round by chronological cues + "
            "  folder year suffix. E.g., 'Uber (2026)' → search-post-airbnb; "
            "  'Facebook (2021)' → search-post-amazon.\n"
            "- For 'outcome': use one of 'landed', 'declined', 'ghosted', "
            "  'targeted', 'in-progress'. Do NOT invent outcomes; if the "
            "  archive doesn't show an outcome, use 'targeted'.\n"
            "- TWO TIER FIELDS — distinguish COMPANY brand from OPPORTUNITY:\n"
            "  * tier_company (A/B/C): the COMPANY BRAND tier, agnostic to "
            "    role. A-tier companies = frontier AI labs (Anthropic, "
            "    DeepMind, OpenAI, xAI, Google DeepMind), plus Google, "
            "    Netflix, Uber, Meta, Apple. B-tier = Wayfair, Upwork, "
            "    DoorDash, Shopify, Pinterest, Snap, Duolingo, Coupang, "
            "    etc. (strong but not A). C-tier = weaker brands.\n"
            "  * tier_opportunity (A/B/C): the role-seniority tier for "
            "    THIS specific engagement. A = C-suite / Head of fn / "
            "    CTO-level. B = Sr Director / VP-of-function. C = lower.\n"
            "  * A role can be A-opportunity at a B-company (e.g., "
            "    Wayfair CDO = tier_company=B, tier_opportunity=A).\n"
            "- DO NOT default every record to 'B'. Apply criteria separately "
            "  to each field.\n"
            "- For search-post-airbnb: frontier AI labs (Anthropic, DeepMind, "
            "  OpenAI, xAI, Google DeepMind) are the operator's CURRENT A-tier-"
            "  company targets; surface them even if only an Interview Prep "
            "  folder exists without rich archive content."
        ),
        "example": {
            "company": "Wayfair",
            "search_round": "search-post-airbnb",
            "role_type": "Chief Data Officer / Head of ML",
            "tier_company": "B",
            "tier_opportunity": "A",
            "outcome": "in-progress",
            "notes": "Authored ML platform vision memo for CTO conversation",
            "confidence": 0.85,
        },
    },
    "strength_theme": {
        "required": ["theme"],
        "optional": ["evidence_types", "across_roles", "supporting_claim", "confidence"],
        "id_fields": ["theme"],
        "description": (
            "A consistent strength theme that appears across multiple role "
            "or search archives. Include the evidence types and roles where "
            "it surfaces."
        ),
        "example": {
            "theme": "Marketplace systems thinking",
            "evidence_types": ["authored_work"],
            "across_roles": ["airbnb", "search-post-airbnb", "linkedin"],
            "supporting_claim": (
                "Repeatedly authored frameworks connecting local technical "
                "decisions to broader marketplace mechanics."
            ),
            "confidence": 0.92,
        },
    },
    "major_accomplishment": {
        "required": ["role", "summary"],
        "optional": ["year", "metric", "confidence"],
        "id_fields": ["role", "summary"],
        "description": (
            "A HEADLINE accomplishment — the kind of item a hiring exec "
            "would highlight on the operator's CV/LinkedIn summary.\n\n"
            "STRICT RULES:\n"
            "- Prefer items that appear in CV/bio records (class=bio) "
            "  over items that only appear in monthly-activity or 1:1 "
            "  records. CVs are curated; monthly reports are minutiae.\n"
            "- DO NOT extract:\n"
            "  * Individual hires (unless the HIRING OUTCOME itself is "
            "    the accomplishment, e.g., 'built a team of 30 from 8')\n"
            "  * Reorg paperwork, QBRs, charters, or monthly summaries "
            "    unless the archive names a specific outcome or metric\n"
            "  * Internal product names or project codenames without a "
            "    visible metric or outcome (e.g., don't include 'delivered "
            "    Elixir' as an accomplishment)\n"
            "  * Work the operator's TEAM did that the operator didn't personally lead\n"
            "  * Derivative / paraphrased items when a crisper version "
            "    exists — prefer the CV phrasing\n"
            "- PREFER:\n"
            "  * Launches with named artifacts + concrete metrics\n"
            "  * Org-scope outcomes the operator explicitly owned\n"
            "  * Strategy transformations with measurable results\n"
            "- DEDUPE: one record per distinct outcome; do not split "
            "  the same launch into multiple records.\n"
            "- AIM for ~3-6 per role. If you can't get to 3 strong "
            "  accomplishments for a role, it's OK to have fewer."
        ),
        "example": {
            "role": "airbnb",
            "year": "2025",
            "summary": "Led Base Price Redesign, materially reducing poor pricing strategies across the marketplace.",
            "metric": "51% reduction in poor pricing strategies vs 5% target",
            "confidence": 0.98,
        },
    },
    "tenet_authored": {
        "required": ["name", "role", "doc_ref"],
        "optional": ["year_approx", "description", "confidence"],
        "id_fields": ["name", "role"],
        "description": (
            "A NAMED framework, operating model, tenets document, or "
            "decision framework the operator EXPLICITLY authored.\n\n"
            "STRICT RULES:\n"
            "- doc_ref is REQUIRED and MUST end in a real file extension: "
            "  .docx / .pdf / .pptx / .doc. Examples of valid doc_ref: "
            "  'FLEX DS Tenets.docx', 'Vision-Mission-Tenets v3.docx', "
            "  'Onduty Principles.docx', 'Flagship Data Operating Model.docx'. "
            "  If you CANNOT name a real canonical file from the archive "
            "  evidence (not a paraphrased placeholder, not the tenet's "
            "  name echoed back), DO NOT EXTRACT that record. Records "
            "  missing a file-extension doc_ref will be automatically "
            "  dropped by the parser.\n"
            "- DO NOT extract:\n"
            "  * Project codenames mentioned in passing (e.g., 'Project "
            "    Prometheus', 'Telescope') — these are projects, not tenets\n"
            "  * Team names or product names (e.g., 'EconTech', 'Marca', "
            "    'Sponsored Products') — these are orgs/products, not tenets\n"
            "  * Models or technical approaches (e.g., 'reinforcement "
            "    learning for pricing') — these are methods, not tenets\n"
            "  * Frameworks the operator DISCUSSED in meetings but did not "
            "    author as a canonical document\n"
            "  * Items from search-round archives unless they were "
            "    authored FOR a prior role (e.g., 'Power Stack' for Uber "
            "    recruiting is NOT an Example Corp-era tenet)\n"
            "- DEDUPE: one record per distinct named framework; do not "
            "  create multiple entries for the same tenet across roles."
        ),
        "example": {
            "name": "FLEX DS Tenets",
            "role": "linkedin",
            "year_approx": "2023",
            "doc_ref": "FLEX DS Tenets.docx",
            "description": (
                "Operating principles for the Flagship Experience data "
                "science team: outcomes, partnership, time discipline, "
                "manager accountability."
            ),
            "confidence": 0.98,
        },
    },
}


FACT_TYPES = tuple(_SCHEMAS.keys())


# ----------------------------------------------------------------------
# Prompt construction
# ----------------------------------------------------------------------


def build_extraction_prompt(fact_type: str, archives: dict[str, str]) -> str:
    """Build a per-type extraction prompt. archives is a dict mapping
    archive file name (e.g. 'airbnb.md') to its markdown content.

    The prompt asks the LLM to emit JSON of shape:
      {"records": [<per-type payload>, ...]}

    Raises ValueError if fact_type is unknown.
    """
    if fact_type not in _SCHEMAS:
        raise ValueError(f"unknown fact_type: {fact_type!r}")

    schema = _SCHEMAS[fact_type]
    required = ", ".join(schema["required"])
    optional = ", ".join(schema["optional"])
    description = schema["description"]
    example = json.dumps(schema["example"], indent=2)

    # Render archives as labeled sections
    archive_blocks: list[str] = []
    for name, content in archives.items():
        archive_blocks.append(f"======= {name} =======\n{content}\n")
    archives_section = "\n".join(archive_blocks)

    return f"""You are extracting typed structured facts of a SINGLE type from
Sam Smith's career archives. The goal is to produce programmatically-
queryable facts that downstream agents (Huckle recruiter drafting,
Murphy meeting prep) can consume without re-reading prose.

FACT TYPE: {fact_type}
{description}

REQUIRED fields per record: {required}
OPTIONAL fields per record: {optional}

Example record:
{example}

RULES:
- Emit ONE record per distinct instance. Do not merge multiple employers,
  tenets, or themes into one record.
- Only include records that are clearly grounded in the archive text.
  If a field is unknown, omit it rather than guessing.
- The 'confidence' field (0.0-1.0) reflects how strongly the archive
  supports the claim. Exec-backed accomplishments with specific metrics
  = ~0.95. Inferences with weak direct support = ~0.5.
- Dates in ISO format (YYYY-MM-DD) where possible.
- Do NOT quote verbatim proprietary strategy or use identifiable
  judgments about other people.

OUTPUT: a single JSON object with exactly this shape:
{{"records": [<record 1>, <record 2>, ...]}}

Do NOT wrap in code fences. Do NOT include prose.

ARCHIVES TO READ:

{archives_section}
"""


# ----------------------------------------------------------------------
# Response parsing
# ----------------------------------------------------------------------


def parse_extraction_response(fact_type: str, raw: str) -> list[dict]:
    """Parse the LLM's JSON response into typed records.

    - Drops records missing required fields.
    - Adds envelope fields (id, type, extracted_at) to each record.
    - Tolerates ```json fences if present.
    - Raises ValueError on invalid JSON or missing 'records' key.
    """
    if fact_type not in _SCHEMAS:
        raise ValueError(f"unknown fact_type: {fact_type!r}")

    if not raw:
        raise ValueError("empty response")

    stripped = raw.strip()
    fence_match = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", stripped, flags=re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1)

    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e

    if not isinstance(obj, dict) or "records" not in obj:
        raise ValueError("response missing 'records' key")

    raw_records = obj["records"]
    if not isinstance(raw_records, list):
        raise ValueError("'records' is not a list")

    schema = _SCHEMAS[fact_type]
    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []
    for rec in raw_records:
        if not isinstance(rec, dict):
            continue
        if not validate_record(fact_type, rec):
            continue
        entry = dict(rec)
        entry["type"] = fact_type
        # Employer: convert exclusive-end dates (e.g., 2024-01-01) to
        # inclusive-end (2023-12-31) so end reads as the operator's last day
        if fact_type == "employer":
            _normalize_inclusive_end(entry)
        entry["id"] = _record_id(fact_type, rec, schema["id_fields"])
        entry.setdefault("confidence", 0.8)
        # Clamp confidence
        try:
            c = float(entry["confidence"])
            entry["confidence"] = max(0.0, min(1.0, c))
        except (TypeError, ValueError):
            entry["confidence"] = 0.0
        entry["extracted_at"] = now_iso
        out.append(entry)
    return out


# Extensions a canonical source document would have. Used to guard the
# tenet_authored doc_ref field from being echoed back as the tenet name.
_DOC_REF_EXTENSIONS = (".docx", ".doc", ".pdf", ".pptx", ".md", ".xlsx", ".txt")


def validate_record(fact_type: str, record: dict) -> bool:
    """Return True if all required fields are present and non-empty.

    Additional per-type gates:
      - tenet_authored: doc_ref must look like a filename (ends in a
        known doc extension). Without this, the LLM fills doc_ref with
        the tenet name itself, which defeats the canonical-doc
        requirement.
    """
    schema = _SCHEMAS.get(fact_type)
    if not schema:
        return False
    for field in schema["required"]:
        v = record.get(field)
        if v is None or (isinstance(v, str) and not v.strip()):
            return False

    if fact_type == "tenet_authored":
        doc_ref = str(record.get("doc_ref", "")).strip().lower()
        if not any(doc_ref.endswith(ext) for ext in _DOC_REF_EXTENSIONS):
            return False

    return True


def _normalize_inclusive_end(record: dict) -> None:
    """Convert exclusive-end dates to inclusive last-day format, in place.

    The LLM tends to echo the timeline file's exclusive-end boundary
    format (e.g., LinkedIn ended '2024-01-01' = exclusive) into employer
    records. But the operator expects inclusive-end (LinkedIn ended '2023-12-31').
    This post-process catches the pattern: any 'end' date that falls on
    the 1st of a month gets converted to the last day of the PREVIOUS
    month.
    """
    end = record.get("end")
    if not isinstance(end, str):
        return
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", end)
    if not m:
        return
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if d != 1:
        # Already something other than the 1st — leave as-is
        return
    try:
        d_obj = datetime(y, mo, 1) - timedelta(days=1)
    except ValueError:
        return
    record["end"] = d_obj.strftime("%Y-%m-%d")


def _record_id(fact_type: str, record: dict, id_fields: list[str]) -> str:
    """Deterministic id from (type, id_fields). Supports idempotent merge
    — same content → same id."""
    key = "|".join(str(record.get(f, "")).strip().lower() for f in id_fields)
    if not key:
        # Fallback to hash of whole record
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    # Include a readable slug from the first id_field where possible
    first = str(record.get(id_fields[0], "")).strip().lower() if id_fields else ""
    slug = re.sub(r"[^a-z0-9]+", "-", first).strip("-")[:40] if first else "rec"
    return f"self-{fact_type}-{slug}-{digest}"


# ----------------------------------------------------------------------
# Merge
# ----------------------------------------------------------------------


def merge_facts(existing: list[dict], new_records: list[dict]) -> list[dict]:
    """Merge new records into existing. Idempotent on `id` — new wins
    on id collision (latest extraction overrides)."""
    by_id: dict[str, dict] = {}
    for rec in existing or []:
        rid = rec.get("id")
        if rid:
            by_id[rid] = dict(rec)
    for rec in new_records or []:
        rid = rec.get("id")
        if rid:
            by_id[rid] = dict(rec)
    return list(by_id.values())
