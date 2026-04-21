"""Pure helpers for career-archive-import.py — Stage 1: walk the career
archive roots, extract text per file type, batch for LLM classification,
and merge classified records into self/archive-index.json idempotently
(on (path, mtime)).

Canonical source roots (role → disk path) live in SOURCE_ROOTS. The
runner in scripts/career-archive-import.py orchestrates: walk → extract
→ batch → LLM classify → merge → write.

No network calls, no filesystem writes at this layer — the runner owns
I/O. All functions are testable with tmp_path fixtures and in-memory
docs built via python-docx / openpyxl / python-pptx / pypdf.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Canonical source roots. The runner defaults to these; each is an absolute
# path on the operator's Windows filesystem. Tests pass in tmp_path instead.
# ---------------------------------------------------------------------------

SOURCE_ROOTS: dict[str, Path] = {
    "bio": Path("E:/Dropbox/Personal/Bio"),
    "job-search": Path("E:/Dropbox/Personal/Job Search"),
    "amazon": Path("E:/Dropbox/Archive/Amazon"),
    "airbnb": Path("E:/Dropbox/Archive/Example Corp"),
    "linkedin": Path("E:/Dropbox/Archive/LinkedIn"),
    "twitch": Path("E:/Dropbox/Archive/Twitch"),
}


# Allow-list of file extensions we'll consider career content. Everything
# else is skipped at walk time. This is stricter than a blocklist because
# the operator's archives contain lots of code/config/asset noise (shapefiles,
# fonts, xml configs, slack exports, etc.) that's irrelevant to the
# profile. Fonts/media/archives/binaries/code are all excluded by
# omission.
ALLOWED_EXTENSIONS: set[str] = {
    ".txt", ".md", ".rtf",
    ".docx", ".doc",
    ".xlsx", ".xls",
    ".pptx", ".ppt",
    ".pdf",
    ".csv",
}


# Kept for test-surface compatibility and documentation — explicitly
# listing the common media/binary types we'd never want, even if they
# snuck past the allow-list somehow.
SKIP_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".mov", ".mp4", ".m4v", ".avi", ".wmv", ".mkv",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".exe", ".dll", ".bin", ".iso",
}


# Minimum file size in bytes to classify. Tiny files (empty notes, name
# shortcuts, stub .txt) are almost never career signal. 200 bytes is a
# generous floor — a one-paragraph bio clears it.
MIN_SIZE_BYTES = 200


# Directory names to skip at walk time. the operator's "Job Search/Interview
# Prep" folders sometimes contain full code checkouts (e.g., Uber 2026
# Product Jam with node_modules) — walking into those explodes record
# counts with zero career signal. Anything whose path contains one of
# these as a path component is excluded.
SKIP_DIR_NAMES: set[str] = {
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    ".next", ".nuxt", "dist", "build", "out", ".cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", ".DS_Store",
    "coverage", ".tox",
    "bower_components",
    ".gradle", ".terraform",
}


# The closed taxonomy the Stage 1 classifier may emit. Anything outside
# this set is clamped to "skip" by parse_classification_response so
# downstream stages don't trip on hallucinated classes.
ALLOWED_CLASSES: set[str] = {
    "bio",
    "accomplishment",
    "okr_kpi",
    "feedback_received",
    "feedback_given",
    "strategic_doc",
    "roadmap",
    "tenet_or_framework",
    "contact_list",
    "email_or_correspondence",
    "job_search_material",
    "ephemeral",
    "skip",
}


# How much of each file's extracted text to send to the classifier. 2000
# chars ≈ 500 tokens per file, × 10 files per batch ≈ 5K tokens prompt.
EXCERPT_CHARS = 2000


# The closed set of date_source values the classifier may return.
# Anything else is clamped to "unknown" so downstream weighting stays
# honest.
ALLOWED_DATE_SOURCES: set[str] = {
    "filename",       # date parsed from filename (e.g. "H2 2025 Roadmap.docx")
    "content",        # date extracted from document body
    "mtime",          # fell back to filesystem mtime
    "parent_node",    # Workflowy date-heading ancestor
    "unknown",        # no reliable signal
}


# ---------------------------------------------------------------------------
# File walking
# ---------------------------------------------------------------------------


def iter_file_records(root: Path, *, role: str) -> Iterator[dict]:
    """Yield a record dict for every file under `root` that is a
    candidate for classification.

    Record shape:
      {
        "path": Path,
        "role": str,           # passed through from caller
        "ext": str,            # lowercased, with leading dot (e.g. ".docx")
        "size": int,           # bytes
        "mtime": str,          # ISO 8601 UTC
      }

    Skips:
      - Missing root entirely (yields nothing, no exception)
      - Extensions in SKIP_EXTENSIONS
      - Microsoft Office lockfiles (names starting with "~$")
    """
    if not root.exists() or not root.is_dir():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("~$"):
            continue
        # Skip paths containing any excluded directory name as a component
        # (e.g., .../node_modules/... or .../.git/...)
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            rel_parts = path.parts
        if any(p in SKIP_DIR_NAMES for p in rel_parts):
            continue
        ext = path.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size < MIN_SIZE_BYTES:
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        yield {
            "path": path,
            "role": role,
            "ext": ext,
            "size": stat.st_size,
            "mtime": mtime,
        }


def infer_role_from_path(path: Path) -> str:
    """Return the canonical role label for a given path, based on which
    SOURCE_ROOTS directory it sits under. 'unknown' if no match.

    Windows path separators and casing are normalized before comparing.
    """
    # Normalize to forward slashes + lowercase for substring matching
    norm = str(path).replace("\\", "/").lower()
    # Check in order — personal/job-search is more specific than personal/bio
    for role in ("amazon", "airbnb", "linkedin", "twitch"):
        if f"/archive/{role}" in norm:
            return role
    if "/personal/job search" in norm:
        return "job-search"
    if "/personal/bio" in norm:
        return "bio"
    return "unknown"


# ---------------------------------------------------------------------------
# Text extraction (one function per supported format + dispatcher)
# ---------------------------------------------------------------------------


def extract_text(path: Path) -> tuple[str, str]:
    """Extract plain text from a file. Returns (text, extractor_used)
    where extractor_used is one of:
      "txt", "docx", "pptx", "pdf", "xlsx",
      "skip:<reason>"   — text is empty, downstream must treat as non-content

    Never raises — format errors and missing files become skip reasons.
    """
    if not path.exists() or not path.is_file():
        return "", "skip:missing"

    ext = path.suffix.lower()
    try:
        if ext in {".txt", ".md"}:
            return _extract_txt(path), "txt"
        if ext == ".docx":
            return _extract_docx(path), "docx"
        if ext == ".pptx":
            return _extract_pptx(path), "pptx"
        if ext == ".pdf":
            return _extract_pdf(path), "pdf"
        if ext == ".xlsx":
            return _extract_xlsx(path), "xlsx"
    except Exception as e:  # noqa: BLE001 — extractors raise a zoo of formats
        return "", f"skip:error:{type(e).__name__}"

    return "", f"skip:unsupported_ext:{ext}"


def _extract_txt(path: Path) -> str:
    # Try UTF-8 first, fall back to cp1252 (common on the operator's Windows bios)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1252", errors="replace")


def _extract_docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text]
    # Tables often hold OKR/KPI data — include them
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text.strip())
    return "\n".join(parts)


def _extract_pptx(path: Path) -> str:
    from pptx import Presentation
    pres = Presentation(str(path))
    parts: list[str] = []
    for slide in pres.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    txt = "".join(run.text for run in para.runs)
                    if txt:
                        parts.append(txt)
        # Speaker notes
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame else ""
            if notes:
                parts.append(f"[notes] {notes}")
    return "\n".join(parts)


def _extract_pdf(path: Path) -> str:
    # pypdf is fast and good enough for text-bearing PDFs; it silently
    # returns empty strings for scanned/image-only PDFs, which we let
    # the classifier see (it'll tag as skip/ephemeral).
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — some pages hit pypdf bugs
            txt = ""
        if txt.strip():
            parts.append(txt)
    return "\n".join(parts)


def _extract_xlsx(path: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        if ws.title:
            parts.append(f"[sheet:{ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append("\t".join(cells))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM classification: prompt building + response parsing
# ---------------------------------------------------------------------------


# Class → one-line description shown in the prompt taxonomy. Order here
# controls the order the classifier sees; we lead with the high-signal
# classes so ambiguity breaks toward them.
_CLASS_DESCRIPTIONS: list[tuple[str, str]] = [
    ("bio", "a bio/blurb written by or about the operator (self-intro, company profile, LinkedIn-style blurb)"),
    ("accomplishment", "a summary of the operator's work impact, launches, or outcomes at a role"),
    ("okr_kpi", "OKRs, KPIs, or metrics the operator owned, authored, or was measured against"),
    ("feedback_received", "performance reviews or evaluations where the operator himself is the subject (composite year-end feedback, manager-written review, self-review draft, calibration notes about the operator). NARROW: the operator almost never receives written feedback outside formal perf-review conversations with his manager — default to feedback_given when unsure"),
    ("feedback_given", "any written evaluation the operator authored about someone else — includes 1:1 notes where the operator is the manager discussing a direct report's performance, hiring debriefs, reference calls the operator gave, candidate interview notes, peer-review contributions the operator wrote, and any document where the SUBJECT being evaluated is not the operator"),
    ("strategic_doc", "product, org, or team strategy documents the operator authored or co-authored"),
    ("roadmap", "a roadmap, planning, or prioritization document"),
    ("tenet_or_framework", "tenets, principles, operating models, or decision frameworks the operator authored"),
    ("contact_list", "a list of names/emails/colleagues (usually xlsx/csv from a role)"),
    ("email_or_correspondence", "emails, 1:1 notes, meeting minutes, or message archives — NOT where the subject is the operator's own performance"),
    ("job_search_material", "cover letters, interview prep, decision frameworks, search planning"),
    ("ephemeral", "one-off notes, drafts, fragments, or low-signal scraps"),
    ("skip", "not career-signal (media, unrelated content, binaries, corruption)"),
]


def build_classification_prompt(batch: list[dict]) -> str:
    """Build a classifier prompt for a batch of file records.

    Each record must have: index, role, path, size, ext, text_excerpt.
    Optional date hints: mtime (ISO string), parent_date (ISO date from
    Workflowy date-heading ancestor).

    The LLM is asked to return {"classifications": [{index, class,
    signal_score, summary, chronological_date, date_source,
    date_confidence}, ...]} — one entry per input file, in any order.
    The parser tolerates reordering and missing indices.

    Chronological_date is LOAD-BEARING for downstream profile synthesis
    (the operator's interests and scope have changed across roles). The
    classifier must either infer it from filename/content, accept the
    parent_date hint (for Workflowy), or fall back to mtime with lower
    confidence.
    """
    taxonomy_lines = "\n".join(f"  {cls}: {desc}" for cls, desc in _CLASS_DESCRIPTIONS)

    file_blocks: list[str] = []
    for rec in batch:
        path = rec["path"]
        # Show relative name only — the classifier shouldn't key off of full paths
        name = path.name if isinstance(path, Path) else str(path).rsplit("/", 1)[-1]
        excerpt = rec.get("text_excerpt", "") or ""
        if len(excerpt) > EXCERPT_CHARS:
            excerpt = excerpt[:EXCERPT_CHARS] + "\n[...truncated]"

        # Date hints shown to classifier — parent_date (Workflowy) is the
        # highest-confidence signal; mtime is a weaker fallback
        hint_parts = []
        if rec.get("parent_date"):
            hint_parts.append(f"parent_date={rec['parent_date']}")
        if rec.get("mtime"):
            hint_parts.append(f"mtime={rec['mtime']}")
        hint_line = ""
        if hint_parts:
            hint_line = f"DATE HINTS: {' | '.join(hint_parts)}\n"

        file_blocks.append(
            f"[{rec['index']}] role={rec['role']} | name={name} | ext={rec['ext']} | size={rec['size']}\n"
            f"{hint_line}"
            f"EXCERPT:\n{excerpt}\n"
        )

    files_section = "\n---\n".join(file_blocks) if file_blocks else "(empty batch)"

    return f"""You are classifying files from Sam Smith's career archive so that a
downstream pipeline can synthesize his professional profile. Each file
gets one class from the taxonomy below, plus a chronological tag so
downstream synthesis can weight recent work over ancient work (the operator's
interests and scope have evolved significantly across his roles).

TAXONOMY:
{taxonomy_lines}

FEEDBACK DIRECTION (the hardest discrimination in this taxonomy):

Apply these signals in order:

1. If the filename, meeting title, or first line of the excerpt contains
   a person's name that is NOT the operator, that OTHER person is the SUBJECT
   being evaluated. the operator is the AUTHOR of the notes, not the subject.
   This is feedback_given — even when the content reads like a formal
   performance review.

   Name-pattern examples (all feedback_given because the filename names
   the SUBJECT, who is not the operator):
     - "Wang__Huan__2020_Mid-Year_Check-In.pdf"         (LastName__FirstName__Doc)
     - "Tchemodanov__Natalia__2020_Mid-Year_Check-In.pdf"
     - "Carson_Forter__2018_Talent_Review.pdf"          (FirstName_LastName__Doc)
     - "Dan 1_1 (4_20_20).docx"                         (FirstName [doc-type])
     - "Ariel.pdf"                                      (bare name)
     - "Ben Mid-Year Feedback.docx"                     (Name + doc-type)

   Any filename of the shape "{{PersonName}}{{separator}}{{Review|Check-In|
   1:1|Talent|Mid-Year|Year-End|Feedback|Coaching}}" where PersonName is
   not the operator indicates feedback_given. Underscores, double-underscores,
   spaces, and hyphens all count as name separators.

2. If the body is the operator writing about another person's performance,
   gaps, promotion readiness, coaching advice, hiring debrief, or
   reference call — feedback_given.

3. feedback_received is NARROW: composite year-end feedback ABOUT
   the operator, self-review drafts the operator wrote about himself, calibration
   notes with the operator as the subject, or 1:1 notes where the operator's manager
   is explicitly giving the operator feedback on the operator's own work. Almost
   always a formal performance-review conversation with the operator's own
   manager.

4. When in doubt, default to feedback_given. the operator gives far more
   written feedback than he receives.

For each file, emit exactly one entry in the output JSON with:
  - "index": the integer index shown in brackets below (preserve it)
  - "class": one of the taxonomy labels above (use "skip" if truly unusable)
  - "signal_score": float 0.0–1.0 for how useful this file is for
    synthesizing the operator's professional profile. A canonical bio, a tenets
    doc, or a performance review is ~0.9. A forwarded newsletter is ~0.1.
  - "summary": ~30-word description of what the file is and why it's
    useful (or why it's skip). Be specific — "strategic doc about
    pricing guidance" beats "strategic doc".
  - "chronological_date": ISO date (YYYY-MM-DD) or partial (YYYY or
    YYYY-MM) for when the work/event actually happened (NOT when bytes
    last changed). Prefer filename > content > parent_date > mtime.
    Empty string if genuinely unknown.
  - "date_source": one of "filename", "content", "parent_node", "mtime",
    or "unknown" — where the date came from. Use "parent_node" when you
    accepted the parent_date hint (Workflowy).
  - "date_confidence": float 0.0–1.0. Filename with explicit date is
    ~0.95. Content-extracted date is ~0.85. parent_node is ~0.95. mtime
    fallback is ~0.4 (mtime reflects last edit, not authorship).

Return a single JSON object with exactly this shape:
{{"classifications": [{{"index": 0, "class": "...", "signal_score": 0.0, "summary": "...", "chronological_date": "...", "date_source": "...", "date_confidence": 0.0}}, ...]}}

Do not invent classes outside the taxonomy. When in doubt between two
valid classes, pick the more specific one. Confidential content is
fine to classify — this classification output will be reviewed by the operator
before any summary gets produced.

FILES:

{files_section}
"""


def parse_classification_response(raw: str) -> list[dict]:
    """Parse the classifier's JSON response into a list of dicts.

    Tolerant to:
      - Missing "summary" field (defaults to "")
      - signal_score outside [0, 1] (clamped)
      - class not in ALLOWED_CLASSES (coerced to "skip")

    Raises ValueError if:
      - raw is not valid JSON
      - parsed JSON lacks the "classifications" key
      - a classification entry lacks required keys (index, class)
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e

    if not isinstance(obj, dict) or "classifications" not in obj:
        raise ValueError("response missing 'classifications' key")

    raw_list = obj["classifications"]
    if not isinstance(raw_list, list):
        raise ValueError("'classifications' is not a list")

    out: list[dict] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        if "index" not in entry or "class" not in entry:
            continue
        cls = entry["class"]
        if cls not in ALLOWED_CLASSES:
            cls = "skip"
        score = entry.get("signal_score", 0.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        date_source = entry.get("date_source", "unknown")
        if date_source not in ALLOWED_DATE_SOURCES:
            date_source = "unknown"

        date_conf = entry.get("date_confidence", 0.0)
        try:
            date_conf = float(date_conf)
        except (TypeError, ValueError):
            date_conf = 0.0
        date_conf = max(0.0, min(1.0, date_conf))

        out.append({
            "index": int(entry["index"]),
            "class": cls,
            "signal_score": score,
            "summary": str(entry.get("summary", "") or ""),
            "chronological_date": str(entry.get("chronological_date", "") or ""),
            "date_source": date_source,
            "date_confidence": date_conf,
        })
    return out


# ---------------------------------------------------------------------------
# Index merge: idempotent on (path, mtime)
# ---------------------------------------------------------------------------


def merge_records_into_index(existing: dict, new_records: list[dict], *, overwrite: bool = False) -> dict:
    """Merge newly-classified records into the archive index.

    Idempotency rule: if the existing entry for a given path has the
    same mtime as the new record, the existing classification is
    preserved (we do NOT re-classify unchanged files). If the mtime
    differs, the new record wins — the file has been edited since we
    last looked.

    Pass overwrite=True to skip the idempotency check — used when the
    runner is forcing a re-classification of specific buckets (e.g.,
    --force-classes). Same-mtime records will be overwritten with the
    new classification.

    Returns a new dict; does not mutate the input.
    """
    out = dict(existing) if existing else {}
    files = dict(out.get("files") or {})

    for rec in new_records:
        key = str(rec["path"])
        prev = files.get(key)
        if not overwrite and prev is not None and prev.get("mtime") == rec.get("mtime"):
            # Same file, same mtime → idempotent, keep existing classification
            continue
        files[key] = dict(rec)
        # Normalize Path object to string for JSON round-trip
        if isinstance(files[key].get("path"), Path):
            files[key]["path"] = str(files[key]["path"])

    out["files"] = files
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out
