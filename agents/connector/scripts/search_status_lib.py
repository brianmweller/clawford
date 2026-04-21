"""search_status_lib — pure helpers for the active-search pipeline miner.

Consumes Gmail recruiter threads + Workflowy interview-prep records,
produces a structured view of the operator's current search pipeline:
  { active_searches: [{company, stage, last_signal_date, notes}],
    recently_concluded: [{company, outcome, date, notes}] }

The runner (search-status-build.py) wires this to the Gmail API and
Workflowy /nodes-export, runs the LLM consolidation step, and writes
self/search-status.md + self/facts/active_search_stages.json.

Downstream consumers:
  - profile.md synthesis (search-status.md as a priority raw doc)
  - Huckle cold-inbound compose prompt (SELF CONTEXT urgency signal:
    'in late stages with X, Y, Z' → creates urgency in draft stance)
"""
from __future__ import annotations

import json
import re


# Funnel stages — left-to-right from first touch to decision.
STAGES: set[str] = {
    "initial-outreach",      # recruiter reached out; the operator hasn't engaged yet
    "engaged",               # the operator replied; scheduling
    "recruiter-screen",      # first recruiter call done
    "hiring-manager",        # intro with hiring manager
    "technical-interview",   # IC / tech screen
    "onsite-panel",          # full day / panel interviews
    "final-round",           # exec round / comp conversation
    "offer",                 # offer extended
    "accepted",              # the operator accepted
    "declined",              # the operator declined
    "passed",                # company passed on the operator
    "ghosted",               # no response either side
    "unknown",               # can't tell from evidence
}


# Title-level patterns that mark a Workflowy meeting record as
# interview-prep / interview-related. Case-insensitive substring match
# on the first line of text_excerpt.
_INTERVIEW_TITLE_PATTERNS = [
    r"phone screen", r"recruiter screen",
    r"hiring manager", r"\bhm intro\b", r"hm chat",
    r"onsite\b", r"\bloop\b", r"interview loop",
    r"technical interview", r"\btech screen\b",
    r"final round", r"exec round",
    r"interview prep", r"prep\b.*\b(phone|onsite|loop|interview|final)",
    r"(phone|onsite|loop|interview|final)\s+prep",
    r"debrief", r"candidate prep",
    r"recruiter (call|chat|intro|conversation)",
]
_INTERVIEW_TITLE_RE = re.compile("|".join(_INTERVIEW_TITLE_PATTERNS), flags=re.IGNORECASE)


def is_interview_workflowy_record(rec: dict) -> bool:
    """Heuristic: does this Workflowy record look like interview-prep
    content? Looks at the first line of text_excerpt (the meeting
    title) for interview-stage keywords."""
    excerpt = (rec.get("text_excerpt") or "").strip()
    if not excerpt:
        return False
    first_line = excerpt.split("\n", 1)[0]
    return bool(_INTERVIEW_TITLE_RE.search(first_line))


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------


def extract_gmail_evidence(threads: list[dict]) -> list[dict]:
    """Reduce a list of Gmail thread dicts to per-thread evidence
    records. Each output has the minimum needed for LLM stage-inference:
    subject, first/last sender, dates, message count, snippet.

    Input thread shape (from the MCP Gmail tool or Gmail API raw):
      {id, messages: [{date, sender, subject, snippet}]}
    """
    out: list[dict] = []
    for thread in threads or []:
        messages = thread.get("messages") or []
        if not messages:
            continue
        # Sort by date ascending (messages often already ordered, but
        # some APIs aren't strict)
        ordered = sorted(
            messages,
            key=lambda m: str(m.get("date") or ""),
        )
        first = ordered[0]
        last = ordered[-1]
        # Subject = the first message's subject (thread topic)
        subject = (first.get("subject") or "").strip()
        snippet = (last.get("snippet") or first.get("snippet") or "").strip()
        out.append({
            "thread_id": thread.get("id", ""),
            "subject": subject,
            "first_sender": first.get("sender", ""),
            "last_sender": last.get("sender", ""),
            "first_date": first.get("date", ""),
            "last_date": last.get("date", ""),
            "message_count": len(ordered),
            "snippet": snippet,
        })
    return out


def extract_workflowy_evidence(wf_records: list[dict]) -> list[dict]:
    """Filter a list of Workflowy records to those that look like
    interview-related content, returning a compact evidence form."""
    out: list[dict] = []
    for rec in wf_records or []:
        if not is_interview_workflowy_record(rec):
            continue
        out.append({
            "path": rec.get("path", ""),
            "chronological_date": rec.get("chronological_date", ""),
            "summary": rec.get("summary", ""),
        })
    return out


# ---------------------------------------------------------------------------
# LLM prompt + response
# ---------------------------------------------------------------------------


def build_status_prompt(
    gmail_evidence: list[dict],
    workflowy_evidence: list[dict],
) -> str:
    """Build the LLM prompt for pipeline-stage inference. The LLM reads
    the combined evidence and returns structured JSON with active
    searches + recently concluded."""
    gmail_block_lines: list[str] = []
    for e in gmail_evidence:
        gmail_block_lines.append(
            f"  - [{e.get('last_date', '?')[:10]} | {e.get('message_count', 0)} msgs] "
            f"subj={e.get('subject', '')[:80]!r}"
        )
        gmail_block_lines.append(
            f"    from_first={e.get('first_sender', '')}  "
            f"from_last={e.get('last_sender', '')}"
        )
        snippet = e.get("snippet", "")
        if snippet:
            gmail_block_lines.append(f"    snippet: {snippet[:240]}")
    gmail_block = "\n".join(gmail_block_lines) if gmail_block_lines else "  (none)"

    wf_block_lines: list[str] = []
    for e in workflowy_evidence:
        wf_block_lines.append(
            f"  - [{e.get('chronological_date', '?'):10s}] "
            f"{(e.get('summary') or '')[:200]}"
        )
    wf_block = "\n".join(wf_block_lines) if wf_block_lines else "  (none)"

    stages_list = ", ".join(sorted(STAGES))

    return f"""You are Sam Smith's assistant. Below is evidence from his Gmail
(threads with likely recruiters in the last 90 days) and from his
Workflowy meeting notes (interview-prep records). Synthesize the
current state of his executive search pipeline.

TASK: infer, per company, (a) what stage the operator is at in that company's
process, (b) the date of the most recent signal, (c) brief notes that
would help the operator — or Huckle drafting a recruiter reply — calibrate
urgency and tone.

STAGES taxonomy (use EXACTLY one of these):
{stages_list}

GMAIL EVIDENCE (subject, participant flow, message count per thread):
{gmail_block}

WORKFLOWY EVIDENCE (interview-prep notes by date, with summaries):
{wf_block}

OUTPUT: a single JSON object with EXACTLY this shape:

{{
  "active_searches": [
    {{
      "company":          "<company name>",
      "stage":            "<one of STAGES>",
      "last_signal_date": "<ISO date YYYY-MM-DD>",
      "notes":            "<one sentence: what's known about scope, hiring manager, or next step. Empty if nothing specific.>"
    }}
  ],
  "recently_concluded": [
    {{
      "company": "<company>",
      "outcome": "accepted | declined | passed | ghosted",
      "date":    "<ISO date>",
      "notes":   "<one sentence>"
    }}
  ]
}}

RULES:
- Deduplicate by company — one entry per company across both sources.
- Prefer the MOST RECENT signal date when consolidating.
- If a company has both Gmail and Workflowy evidence, combine them in
  notes.
- 'active_searches' = still in-flight (any stage before offer, accepted,
  declined, passed, or ghosted). Everything else goes in
  'recently_concluded'.
- If stage is genuinely unclear from evidence, use "unknown" — don't
  guess.
- Don't invent companies. If a Gmail thread is from a retained-search
  firm (e.g., rivierapartners.com) without naming the company in the
  subject or snippet, it's OK to use "unknown" as the company.
- Output valid JSON only, no code fences.
"""


def parse_status_response(raw: str) -> dict:
    """Parse the LLM's JSON response. Graceful on malformed input —
    returns empty pipeline structure rather than raising, so search-
    status failures don't crash downstream synthesis."""
    empty = {"active_searches": [], "recently_concluded": []}
    if not raw:
        return empty
    stripped = raw.strip()
    fence_match = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", stripped, flags=re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1)
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return empty
    if not isinstance(obj, dict):
        return empty

    active = []
    for rec in obj.get("active_searches") or []:
        if not isinstance(rec, dict):
            continue
        company = str(rec.get("company", "")).strip()
        if not company:
            continue
        stage = str(rec.get("stage", "unknown"))
        if stage not in STAGES:
            stage = "unknown"
        active.append({
            "company": company,
            "stage": stage,
            "last_signal_date": str(rec.get("last_signal_date", "") or ""),
            "notes": str(rec.get("notes", "") or ""),
        })

    concluded = []
    for rec in obj.get("recently_concluded") or []:
        if not isinstance(rec, dict):
            continue
        company = str(rec.get("company", "")).strip()
        if not company:
            continue
        concluded.append({
            "company": company,
            "outcome": str(rec.get("outcome", "unknown")),
            "date": str(rec.get("date", "") or ""),
            "notes": str(rec.get("notes", "") or ""),
        })

    return {"active_searches": active, "recently_concluded": concluded}


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def format_status_md(data: dict, *, generated_at: str) -> str:
    """Render the structured pipeline as markdown for human reading
    (self/search-status.md) and for profile.md synthesis to consume as
    a priority raw doc."""
    active = data.get("active_searches") or []
    concluded = data.get("recently_concluded") or []

    lines: list[str] = []
    lines.append("# the operator's Active Search Status")
    lines.append("")
    lines.append(f"- **generated_at:** {generated_at}")
    lines.append(f"- **active_pipeline_size:** {len(active)}")
    lines.append(f"- **recently_concluded_size:** {len(concluded)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## Active pipeline")
    lines.append("")
    if active:
        lines.append("| Company | Stage | Last signal | Notes |")
        lines.append("|---------|-------|-------------|-------|")
        for e in sorted(active, key=lambda r: r.get("last_signal_date", ""), reverse=True):
            notes = (e.get("notes") or "").replace("|", r"\|")
            lines.append(f"| {e['company']} | {e['stage']} | "
                         f"{e.get('last_signal_date', '')} | {notes} |")
    else:
        lines.append("*(no active searches detected in the current window)*")
    lines.append("")

    lines.append("## Recently concluded")
    lines.append("")
    if concluded:
        lines.append("| Company | Outcome | Date | Notes |")
        lines.append("|---------|---------|------|-------|")
        for e in sorted(concluded, key=lambda r: r.get("date", ""), reverse=True):
            notes = (e.get("notes") or "").replace("|", r"\|")
            lines.append(f"| {e['company']} | {e['outcome']} | "
                         f"{e.get('date', '')} | {notes} |")
    else:
        lines.append("*(no concluded searches in the current window)*")
    lines.append("")

    return "\n".join(lines) + "\n"
