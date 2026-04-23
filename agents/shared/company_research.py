"""agents/shared/company_research.py — company-level enrichment for
Huckle (cold-recruiter drafting) and Murphy (meeting prep).

Why this exists: Huckle's LLM infers company/role/stage from the
recruiter email body every time, and Murphy's prep sees zero signal
about the company at all — so pitches and question lists land generic.
This module turns `company_name (+ optional role_title)` into a
structured, operator-tailored brief: what the company ships, stage,
recent news, and an `operator_fit` block (angles, concerns, specific
questions, hooks to drop). The brief is cached per-slug under
~/.clawford/company-research-cache/ with a 30-day TTL so:
  1. A Murphy prep at 3:30 AM PT for an Anthropic hiring-manager seeds
     the cache; a Huckle reply at 10 AM to an Anthropic recruiter the
     same day is a cache hit (0 Brave + 0 LLM).
  2. Corporate-suffix variants ("Anthropic, PBC" vs "Anthropic") hash
     to the same slug so we don't re-research the same company.

Public entry point: research_company(company_name, *, role_title,
operator_context, force) → CompanyBrief.

Degrades gracefully: Brave failure → empty snippets, LLM still runs
with lowered confidence. LLM failure → CompanyBrief.error set, no
cache write (next call retries). Never raises.

Budget: ≤3 HTTPS + 1 LLM per miss, 0 per hit. Fits comfortably under
the fleet's 10-min cron cap.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from agents.shared.llm import InferResult, infer as _llm_infer_backend
except ImportError:
    # Running from within the workspace (agents/shared/ on sys.path).
    from llm import InferResult, infer as _llm_infer_backend  # type: ignore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_CACHE_DIR = Path(os.path.expanduser("~/.clawford/company-research-cache"))
DEFAULT_TTL_DAYS = 30
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_TIMEOUT_S = 10
LLM_TIMEOUT_S = 60

# Corporate-entity suffixes. Order matters: strip the first match so
# "Reddit, Inc." (with comma) is tried before " inc" (no comma) to
# avoid leaving a trailing comma.
_CORP_SUFFIXES: tuple[str, ...] = (
    ", pbc",
    ", inc.",
    ", inc",
    " inc.",
    " inc",
    ", corp.",
    ", corp",
    " corp.",
    " corp",
    ", ltd.",
    ", ltd",
    " ltd.",
    " ltd",
    ", llc",
    " llc",
    ", llp",
    " llp",
)


_SYNTHESIS_INSTRUCTIONS = (
    "You are a research analyst producing a terse, operator-tailored "
    "brief on a company. Reply with a single valid JSON object matching "
    "the schema the user provides. No prose outside the JSON."
)


# ---------------------------------------------------------------------------
# CompanyBrief
# ---------------------------------------------------------------------------


@dataclass
class CompanyBrief:
    """Structured brief returned by research_company.

    On success: `error` is None, `.ok` is True, and the content fields
    are populated. On failure: `error` carries the reason, `.ok` is
    False, and callers should render without the brief (or fall back
    to their pre-enrichment path).
    """

    company_name: str
    company_slug: str
    researched_at_utc: str = ""
    what_they_do: str = ""
    stage_signal: str = "unknown"
    recent_news: list[dict] = field(default_factory=list)
    tech_or_product_hints: list[str] = field(default_factory=list)
    role_context: dict | None = None
    operator_fit: dict = field(default_factory=dict)
    confidence: str = "low"
    sources: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_prompt_dict(self) -> dict:
        """Compact dict safe to splice into downstream compose / prep
        prompts. Errored briefs return an empty dict so callers can
        treat the return as falsy."""
        if not self.ok:
            return {}
        return {
            "company_name": self.company_name,
            "company_slug": self.company_slug,
            "what_they_do": self.what_they_do,
            "stage_signal": self.stage_signal,
            "recent_news": list(self.recent_news),
            "tech_or_product_hints": list(self.tech_or_product_hints),
            "role_context": dict(self.role_context) if self.role_context else None,
            "operator_fit": dict(self.operator_fit),
            "confidence": self.confidence,
            "sources": list(self.sources),
            "researched_at_utc": self.researched_at_utc,
        }

    def to_cache_dict(self) -> dict:
        """Full serialization including the researched_at timestamp
        used for TTL comparisons."""
        return {
            "company_name": self.company_name,
            "company_slug": self.company_slug,
            "researched_at_utc": self.researched_at_utc,
            "what_they_do": self.what_they_do,
            "stage_signal": self.stage_signal,
            "recent_news": list(self.recent_news),
            "tech_or_product_hints": list(self.tech_or_product_hints),
            "role_context": dict(self.role_context) if self.role_context else None,
            "operator_fit": dict(self.operator_fit),
            "confidence": self.confidence,
            "sources": list(self.sources),
        }

    @classmethod
    def from_cache_dict(cls, data: dict) -> "CompanyBrief":
        return cls(
            company_name=data.get("company_name", ""),
            company_slug=data.get("company_slug", ""),
            researched_at_utc=data.get("researched_at_utc", ""),
            what_they_do=data.get("what_they_do", ""),
            stage_signal=data.get("stage_signal", "unknown"),
            recent_news=list(data.get("recent_news") or []),
            tech_or_product_hints=list(data.get("tech_or_product_hints") or []),
            role_context=data.get("role_context"),
            operator_fit=dict(data.get("operator_fit") or {}),
            confidence=data.get("confidence", "low"),
            sources=list(data.get("sources") or []),
        )


# ---------------------------------------------------------------------------
# Slug normalization
# ---------------------------------------------------------------------------


def _slugify_company(name: str) -> str:
    """Normalize a company name to a cache-safe slug.

    Lowercases, strips a corporate-entity tail (", PBC", " Inc.",
    " Corp.", " Ltd.", " LLC", " LLP"), and replaces runs of
    non-alphanumerics with single hyphens.

    "Anthropic, PBC" → "anthropic"
    "Hugging Face" → "hugging-face"
    "Goldman Sachs Group, Inc." → "goldman-sachs-group"
    """
    raw = (name or "").strip().lower()
    if not raw:
        return ""
    for suffix in _CORP_SUFFIXES:
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].rstrip(", ")
            break
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug


# ---------------------------------------------------------------------------
# Cache read / write
# ---------------------------------------------------------------------------


def _cache_path(slug: str, cache_dir: Path) -> Path:
    return cache_dir / f"{slug}.json"


def _read_cache(
    path: Path, *, now: datetime, ttl_days: int
) -> Optional[CompanyBrief]:
    """Read a cache file if it exists and is within TTL. Returns None on
    missing, malformed, or expired — the caller then refreshes."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ts_raw = data.get("researched_at_utc", "")
    try:
        ts = datetime.fromisoformat(ts_raw)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now - ts > timedelta(days=ttl_days):
        return None
    return CompanyBrief.from_cache_dict(data)


def _write_cache_atomic(path: Path, brief: CompanyBrief) -> None:
    """Atomic cache write: tempfile in the same directory, then rename.
    The same-dir temp file guarantees the rename is a single inode move
    on Windows and POSIX alike — no partial file ever appears at the
    final path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(brief.to_cache_dict(), indent=2), encoding="utf-8"
    )
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Brave Search (mirrors agents/news-digest/scripts/on-demand.py)
# ---------------------------------------------------------------------------


def _search_brave(query: str, api_key: str) -> list[dict]:
    """Call the Brave Web Search API. Returns a list of
    {title, url, description} dicts, or [] on any failure. Matches the
    shape of the news-digest caller."""
    if not api_key:
        return []
    q = urllib.parse.quote_plus(query)
    url = f"{BRAVE_ENDPOINT}?q={q}&count=5"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=BRAVE_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        print(f"[company_research] brave error: {e}", file=sys.stderr)
        return []

    out: list[dict] = []
    for result in data.get("web", {}).get("results", []):
        out.append(
            {
                "title": result.get("title", "") or "",
                "url": result.get("url", "") or "",
                "description": result.get("description", "") or "",
            }
        )
    return out


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------


def _llm_infer(*, prompt: str, timeout: int = LLM_TIMEOUT_S) -> InferResult:
    """Indirection so tests can monkeypatch at module scope without
    having to stub urllib on the underlying llm.py."""
    return _llm_infer_backend(
        prompt=prompt,
        instructions=_SYNTHESIS_INSTRUCTIONS,
        json_mode=True,
        timeout=timeout,
    )


def _render_snippet_bundle(results: list[dict]) -> str:
    if not results:
        return "(no results)"
    lines: list[str] = []
    for r in results:
        title = r.get("title", "").strip()
        desc = r.get("description", "").strip()
        url = r.get("url", "").strip()
        lines.append(f"- {title} — {desc} ({url})")
    return "\n".join(lines)


def _render_operator_context(ctx: dict | None) -> str:
    if not ctx:
        return "(no operator context provided)"
    parts: list[str] = []
    strengths = ctx.get("strength_themes") or []
    if strengths:
        parts.append("Strength themes the operator leans on:")
        for s in strengths:
            label = s.get("value") if isinstance(s, dict) else str(s)
            if label:
                parts.append(f"  - {label}")
    targets = ctx.get("current_targets") or []
    if targets:
        parts.append("Current target companies (the operator's shortlist):")
        for t in targets:
            label = t.get("value") if isinstance(t, dict) else str(t)
            if label:
                parts.append(f"  - {label}")
    lvl = (ctx.get("level_bar_text") or "").strip()
    if lvl:
        parts.append("Level/scope bar the operator is looking for:")
        parts.append(lvl)
    return "\n".join(parts) if parts else "(empty operator context)"


def _build_synthesis_prompt(
    *,
    company_name: str,
    role_title: str | None,
    what_they_do_hits: list[dict],
    news_hits: list[dict],
    jd_hits: list[dict],
    operator_context: dict | None,
) -> str:
    role_line = (
        f"Role under consideration: {role_title}"
        if role_title
        else "Role under consideration: (not specified)"
    )
    jd_block = (
        f"\n=== Role / JD signal ===\n{_render_snippet_bundle(jd_hits)}\n"
        if role_title
        else ""
    )
    return f"""Produce a brief about {company_name} for a Director-level operator \
(Sam Smith, former Example Corp Director) who may be engaging with this company.

{role_line}

SEARCH RESULTS:
=== What they do ===
{_render_snippet_bundle(what_they_do_hits)}

=== Recent news ===
{_render_snippet_bundle(news_hits)}
{jd_block}
OPERATOR CONTEXT:
{_render_operator_context(operator_context)}

Return a JSON object with EXACTLY these keys:
  - what_they_do         : one concrete sentence
  - stage_signal         : one of "seed","series-A","series-B","series-C","series-D","pre-IPO","public","unknown"
  - recent_news          : list of 3-5 items, each {{"bullet","dated"}}; dated is "YYYY-MM" or similar
  - tech_or_product_hints: list of short strings (specific products, stacks, markets)
  - role_context         : {{"title","team_hint","level_band_hint","scope_hint"}} or null
  - operator_fit         : {{"strength_angles", "concerns", "questions_to_ask", "hooks_to_drop"}}
      - strength_angles   : which of the operator's strength themes map to this company's stated problems
      - concerns          : stage/level/domain mismatches — be honest, the operator prefers filter over flattery
      - questions_to_ask  : 3-5 SPECIFIC questions grounded in what_they_do + recent_news.
                            NEVER "tell me about team structure." PREFER questions tying a specific
                            product line to a specific recent signal.
      - hooks_to_drop     : 1-2 phrases the operator could weave into a reply that prove he read about them
  - confidence           : "high" | "medium" | "low" — use "low" when snippets are empty or vague
  - sources              : list of {{"url","title"}} you drew from

Output ONLY the JSON object. No prose."""


def _parse_synthesis(text: str) -> dict:
    """Parse LLM response as JSON. Raises json.JSONDecodeError on
    malformed output; the caller converts that into a CompanyBrief
    with .error set."""
    return json.loads(text)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def research_company(
    company_name: str,
    *,
    role_title: str | None = None,
    operator_context: dict | None = None,
    force: bool = False,
    cache_dir: Path | None = None,
    now: datetime | None = None,
    brave_api_key: str | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> CompanyBrief:
    """Research a company, returning a structured brief.

    Args:
        company_name: The company to research. Gets slugified — "Anthropic, PBC"
            and "Anthropic" resolve to the same cache entry.
        role_title: Optional. If present, a third Brave query is fired
            targeting the JD, and role_context in the brief is populated.
        operator_context: Optional dict with keys {strength_themes,
            current_targets, level_bar_text} — typically a projection of
            self_profile.SelfProfile. Flows into the synthesis prompt so
            operator_fit is the operator-tailored, not generic.
        force: Bypass cache and re-fetch.
        cache_dir: Override the default cache dir. Tests pass tmp_path.
        now: Override current time (for TTL testing). Tests pass a
            fixed UTC datetime.
        brave_api_key: Override the BRAVE_API_KEY env var. Empty string
            or None skips the Brave call — synthesis still runs on
            empty snippets and returns confidence="low".
        ttl_days: Cache TTL. Default 30.

    Returns:
        CompanyBrief. Check .ok — never raises.
    """
    slug = _slugify_company(company_name)
    if not slug:
        return CompanyBrief(
            company_name=company_name,
            company_slug="",
            error="empty company_name",
        )

    actual_cache_dir = cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR
    actual_now = now if now is not None else datetime.now(timezone.utc)
    if actual_now.tzinfo is None:
        actual_now = actual_now.replace(tzinfo=timezone.utc)

    cache_path = _cache_path(slug, actual_cache_dir)

    if not force:
        cached = _read_cache(cache_path, now=actual_now, ttl_days=ttl_days)
        if cached is not None:
            return cached

    api_key = brave_api_key if brave_api_key is not None else os.environ.get("BRAVE_API_KEY", "")

    # Brave queries: always 2 (product + news), +1 if a role title is given.
    what_query = f'"{company_name}" what they do'
    news_query = f'"{company_name}" news'
    what_hits = _search_brave(what_query, api_key) if api_key else []
    news_hits = _search_brave(news_query, api_key) if api_key else []
    jd_hits: list[dict] = []
    if role_title:
        jd_query = f'"{role_title}" at "{company_name}"'
        jd_hits = _search_brave(jd_query, api_key) if api_key else []

    prompt = _build_synthesis_prompt(
        company_name=company_name,
        role_title=role_title,
        what_they_do_hits=what_hits,
        news_hits=news_hits,
        jd_hits=jd_hits,
        operator_context=operator_context,
    )

    llm_result = _llm_infer(prompt=prompt, timeout=LLM_TIMEOUT_S)
    if not llm_result.ok:
        return CompanyBrief(
            company_name=company_name,
            company_slug=slug,
            error=f"llm failure: {llm_result.error}",
        )

    try:
        parsed = _parse_synthesis(llm_result.text)
    except (json.JSONDecodeError, ValueError) as e:
        return CompanyBrief(
            company_name=company_name,
            company_slug=slug,
            error=f"llm returned invalid JSON: {e}",
        )
    if not isinstance(parsed, dict):
        return CompanyBrief(
            company_name=company_name,
            company_slug=slug,
            error="llm returned non-object JSON",
        )

    brief = CompanyBrief(
        company_name=company_name,
        company_slug=slug,
        researched_at_utc=actual_now.isoformat(),
        what_they_do=str(parsed.get("what_they_do", "")).strip(),
        stage_signal=str(parsed.get("stage_signal", "unknown")).strip() or "unknown",
        recent_news=_coerce_list_of_dicts(parsed.get("recent_news")),
        tech_or_product_hints=_coerce_list_of_str(parsed.get("tech_or_product_hints")),
        role_context=(parsed.get("role_context") if isinstance(parsed.get("role_context"), dict) else None),
        operator_fit=(parsed.get("operator_fit") if isinstance(parsed.get("operator_fit"), dict) else {}),
        confidence=str(parsed.get("confidence", "low")).strip() or "low",
        sources=_coerce_list_of_dicts(parsed.get("sources")),
    )

    try:
        _write_cache_atomic(cache_path, brief)
    except OSError as e:
        # Cache-write failure doesn't invalidate the brief we just
        # synthesized — log and continue. Next call refreshes.
        print(f"[company_research] cache write failed: {e}", file=sys.stderr)

    return brief


def _coerce_list_of_dicts(x: Any) -> list[dict]:
    if not isinstance(x, list):
        return []
    return [d for d in x if isinstance(d, dict)]


def _coerce_list_of_str(x: Any) -> list[str]:
    if not isinstance(x, list):
        return []
    return [str(s) for s in x if isinstance(s, (str, int, float))]
