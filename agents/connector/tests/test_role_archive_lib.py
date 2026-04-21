"""Tests for role_archive_lib — pure helpers for the per-role archive
synthesizer. Groups archive-index records by role, weights them by
class + signal_score, and builds synthesis prompts.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from role_archive_lib import (  # type: ignore
    CLASS_WEIGHTS,
    RAW_CONTENT_CLASSES,
    build_meeting_patterns_prompt,
    build_synthesis_prompt,
    build_workflowy_text_map,
    effective_score,
    fetch_raw_content,
    group_records_by_role,
    parse_meeting_patterns_response,
    pick_raw_records,
    rank_records,
)


def _rec(path, cls, score, date="", role="amazon", summary="x"):
    return path, {
        "path": path,
        "role": role,
        "class": cls,
        "signal_score": score,
        "chronological_date": date,
        "date_source": "content" if date else "unknown",
        "date_confidence": 0.8 if date else 0.0,
        "summary": summary,
        "size": 100,
        "ext": ".docx",
        "mtime": "2021-01-01T00:00:00+00:00",
    }


# --- effective_score ---

def test_effective_score_applies_class_weight():
    # feedback_received has weight 1.0, ephemeral has lower
    _, r_fb = _rec("a", "feedback_received", 0.8)
    _, r_eph = _rec("b", "ephemeral", 0.8)
    assert effective_score(r_fb) > effective_score(r_eph)


def test_effective_score_skip_class_is_zero():
    _, r = _rec("s", "skip", 1.0)
    assert effective_score(r) == 0.0


def test_effective_score_is_signal_times_weight():
    _, r = _rec("a", "bio", 0.9)
    expected = 0.9 * CLASS_WEIGHTS["bio"]
    assert abs(effective_score(r) - expected) < 1e-9


# --- group_records_by_role ---

_RANGES = [
    {"role": "pre-twitch", "start": "", "end": "2019-05-01", "label": "x"},
    {"role": "twitch", "start": "2019-05-01", "end": "2020-09-01", "label": "x"},
    {"role": "amazon", "start": "2020-09-01", "end": "2022-04-01", "label": "x"},
    {"role": "netflix", "start": "2022-04-01", "end": "2022-11-01", "label": "x"},
    {"role": "linkedin", "start": "2022-11-01", "end": "2024-01-01", "label": "x"},
    {"role": "between-linkedin-airbnb", "start": "2024-01-01", "end": "2024-05-01", "label": "x"},
    {"role": "airbnb", "start": "2024-05-01", "end": "2026-02-01", "label": "x"},
    {"role": "post-airbnb", "start": "2026-02-01", "end": "current", "label": "x"},
]


def test_group_records_by_role_routes_filesystem_role_direct():
    """Filesystem records with role=amazon/airbnb/linkedin/twitch/netflix
    route directly to that role, regardless of chronological_date."""
    path_a, entry_a = _rec("a.docx", "strategic_doc", 0.8, role="amazon", date="2021-01-01")
    path_b, entry_b = _rec("b.docx", "strategic_doc", 0.8, role="airbnb", date="2024-01-01")
    index = {"files": {path_a: entry_a, path_b: entry_b}}
    groups = group_records_by_role(index, _RANGES)
    assert len(groups["amazon"]) == 1
    assert len(groups["airbnb"]) == 1


def test_group_records_by_role_routes_meetings_by_chronological_date():
    """Workflowy records (role=meetings) route by chronological_date
    against the timeline — EXCEPT those classified job_search_material,
    which always go to the search/job-search bucket via the class
    override (the operator's rule: job-search content never contaminates role
    archives)."""
    p1, e1 = _rec("wf://1", "email_or_correspondence", 0.5, role="meetings", date="2021-03-15")
    p2, e2 = _rec("wf://2", "feedback_given", 0.8, role="meetings", date="2024-07-20")
    p3, e3 = _rec("wf://3", "strategic_doc", 0.7, role="meetings", date="2022-06-01")
    p4, e4 = _rec("wf://4", "job_search_material", 0.8, role="meetings", date="2024-03-10")
    index = {"files": {p1: e1, p2: e2, p3: e3, p4: e4}}
    groups = group_records_by_role(index, _RANGES)   # no search_timeline
    # Non-job-search WF records route by date to role buckets
    assert len(groups["amazon"]) == 1               # 2021-03 → amazon
    assert len(groups["airbnb"]) == 1               # 2024-07 → airbnb
    assert len(groups["netflix"]) == 1              # 2022-06 → netflix
    # job_search_material → job-search bucket (NOT between-linkedin-airbnb)
    assert len(groups["job-search"]) == 1
    assert not groups.get("between-linkedin-airbnb")


def test_group_records_by_role_bios_route_by_date_when_available():
    """Bios (role=bio) route by chronological_date if known; otherwise
    they fall into a cross-role 'bio' bucket the synthesizer can pick up."""
    p1, e1 = _rec("LinkedIn Bio.txt", "bio", 0.95, role="bio", date="2022-12-01")
    p2, e2 = _rec("Twitch Bio.txt", "bio", 0.9, role="bio", date="")  # no date
    index = {"files": {p1: e1, p2: e2}}
    groups = group_records_by_role(index, _RANGES)
    # LinkedIn Bio with 2022-12 date → linkedin role
    assert any(r["path"] == "LinkedIn Bio.txt" for r in groups.get("linkedin", []))
    # Undated bio falls to 'bio' bucket for cross-role consumption
    assert any(r["path"] == "Twitch Bio.txt" for r in groups.get("bio", []))


def test_group_records_by_role_job_search_stays_separate():
    p, e = _rec("cover_letter.docx", "job_search_material", 0.9, role="job-search", date="2024-01-01")
    index = {"files": {p: e}}
    groups = group_records_by_role(index, _RANGES)
    assert len(groups["job-search"]) == 1
    # Job-search records do NOT leak into the airbnb bucket even if date-overlapping
    assert not groups.get("airbnb")


_SEARCH_RANGES = [
    {"role": "search-post-twitch", "start": "2019-10-01", "end": "2020-09-01", "label": "Amazon round"},
    {"role": "search-post-amazon", "start": "2021-09-01", "end": "2022-11-01", "label": "DoorDash/Shopify/Netflix → LinkedIn"},
    {"role": "search-post-linkedin", "start": "2023-06-01", "end": "2024-05-01", "label": "Duolingo/Uber → Example Corp"},
    {"role": "search-post-airbnb", "start": "2025-06-01", "end": "current", "label": "Wayfair/Uber 2026"},
]


def test_group_records_by_role_routes_job_search_by_search_timeline():
    """With a search_timeline_ranges param, job-search records go to
    the search-round bucket matching their chronological_date."""
    p1, e1 = _rec("amazon_cover.docx", "job_search_material", 0.8, role="job-search", date="2020-03-15")
    p2, e2 = _rec("ds_prep.docx", "job_search_material", 0.8, role="job-search", date="2022-01-20")
    p3, e3 = _rec("duolingo.docx", "job_search_material", 0.8, role="job-search", date="2023-11-05")
    p4, e4 = _rec("wayfair.docx", "job_search_material", 0.8, role="job-search", date="2025-09-10")
    index = {"files": {p1: e1, p2: e2, p3: e3, p4: e4}}
    groups = group_records_by_role(index, _RANGES, search_timeline_ranges=_SEARCH_RANGES)
    assert len(groups["search-post-twitch"]) == 1
    assert len(groups["search-post-amazon"]) == 1
    assert len(groups["search-post-linkedin"]) == 1
    assert len(groups["search-post-airbnb"]) == 1
    # Flat job-search bucket not used when search_timeline is provided
    assert not groups.get("job-search")


def test_group_records_by_role_filters_pre_2015_job_search_dates():
    """Classifier artifacts (years like 1880, 1920) should not be
    routed to any search round. They fall to search-undated."""
    p, e = _rec("weird.docx", "job_search_material", 0.5, role="job-search", date="1920-01-01")
    index = {"files": {p: e}}
    groups = group_records_by_role(index, _RANGES, search_timeline_ranges=_SEARCH_RANGES)
    # Nothing gets routed to a real search-round
    for r in _SEARCH_RANGES:
        assert not groups.get(r["role"])
    # Falls to search-undated
    assert len(groups.get("search-undated", [])) == 1


def test_group_records_by_role_job_search_without_date_to_undated():
    """Records with no chronological_date (e.g., Decision Framework)
    can't be routed — they fall to search-undated."""
    p, e = _rec("framework.docx", "job_search_material", 0.9, role="job-search", date="")
    index = {"files": {p: e}}
    groups = group_records_by_role(index, _RANGES, search_timeline_ranges=_SEARCH_RANGES)
    assert len(groups.get("search-undated", [])) == 1


def test_group_records_by_role_without_search_timeline_flat_bucket():
    """When search_timeline_ranges is not provided (legacy path), all
    job-search records stay in a single flat bucket."""
    p, e = _rec("x.docx", "job_search_material", 0.8, role="job-search", date="2024-01-01")
    index = {"files": {p: e}}
    groups = group_records_by_role(index, _RANGES)
    assert len(groups["job-search"]) == 1
    assert not groups.get("search-post-linkedin")


def test_group_records_by_role_class_job_search_material_overrides_workflowy_role():
    """Workflowy (role=meetings) records classified as job_search_material
    route to search rounds, not to the role active on their date.

    Catches the scenario where Uber/Wayfair interview-prep meeting notes
    got pulled into role archives (e.g., linkedin.md, airbnb.md)."""
    # Uber onsite prep during LinkedIn tenure (role timeline says 2023-07 = linkedin)
    p1, e1 = _rec("wf://uber-prep-1", "job_search_material", 0.9,
                  role="meetings", date="2023-07-15")
    # Product jam prep during Example Corp tenure
    p2, e2 = _rec("wf://uber-product-jam", "job_search_material", 0.9,
                  role="meetings", date="2026-03-10")
    index = {"files": {p1: e1, p2: e2}}
    groups = group_records_by_role(index, _RANGES, search_timeline_ranges=_SEARCH_RANGES)
    # Should land in search-round buckets, not role buckets
    assert not groups.get("linkedin")
    assert not groups.get("airbnb")
    assert len(groups["search-post-linkedin"]) == 1
    assert len(groups["search-post-airbnb"]) == 1


def test_group_records_by_role_class_override_works_for_filesystem_too():
    """Filesystem records classified job_search_material also route by
    search timeline (this was already the case via src_role=job-search,
    but the class override makes it robust against future src_role
    mis-tagging)."""
    p, e = _rec("prep.docx", "job_search_material", 0.9,
                role="airbnb", date="2025-09-01")
    # Even though src_role is 'airbnb' (filesystem path hypothetically),
    # class=job_search_material forces search-round routing
    index = {"files": {p: e}}
    groups = group_records_by_role(index, _RANGES, search_timeline_ranges=_SEARCH_RANGES)
    assert not groups.get("airbnb")
    assert len(groups.get("search-post-airbnb", [])) == 1


def test_group_records_by_role_unknown_date_bucket():
    """Records with role=meetings but no chronological_date end up in
    'unknown' — synthesizer typically drops these."""
    p, e = _rec("wf://x", "email_or_correspondence", 0.3, role="meetings", date="")
    index = {"files": {p: e}}
    groups = group_records_by_role(index, _RANGES)
    assert len(groups["unknown"]) == 1


def test_group_records_by_role_empty_index_returns_empty():
    groups = group_records_by_role({"files": {}}, _RANGES)
    assert groups == {}


# --- rank_records ---

def test_rank_records_orders_by_effective_score_desc():
    r1 = _rec("lo", "ephemeral", 0.9)[1]   # low weight × high score
    r2 = _rec("hi", "feedback_received", 0.9)[1]   # high weight × high score
    r3 = _rec("mid", "strategic_doc", 0.5)[1]
    ranked = rank_records([r1, r2, r3])
    assert [r["path"] for r in ranked] == ["hi", "mid", "lo"]


def test_rank_records_truncates_to_limit():
    records = [_rec(f"r{i}", "accomplishment", 0.9 - i * 0.01)[1] for i in range(20)]
    ranked = rank_records(records, limit=5)
    assert len(ranked) == 5
    # Best 5 = r0..r4
    assert [r["path"] for r in ranked] == [f"r{i}" for i in range(5)]


def test_rank_records_empty_returns_empty():
    assert rank_records([]) == []


# --- build_synthesis_prompt ---

def test_build_synthesis_prompt_includes_role_and_label():
    range_info = {"role": "airbnb", "start": "2023-01-01", "end": "2026-02-01", "label": "Marketplace Data Science"}
    records = [_rec("r1", "bio", 0.9, date="2024-01-01", summary="Example Corp bio about marketplace leadership")[1]]
    prompt = build_synthesis_prompt(range_info, records=records)
    assert "airbnb" in prompt.lower()
    assert "Marketplace Data Science" in prompt
    assert "2023-01-01" in prompt
    assert "2026-02-01" in prompt


def test_build_synthesis_prompt_includes_record_summaries():
    range_info = {"role": "twitch", "start": "2019-05-01", "end": "2020-09-01", "label": "Director, Central Science"}
    records = [
        _rec("a", "okr_kpi", 0.95, date="2020-07", summary="Q2 Central Science OKRs with LTV and price-sensitivity goals")[1],
        _rec("b", "accomplishment", 0.9, date="2020-06", summary="Shipped Viewer Lifecycle Stages framework")[1],
    ]
    prompt = build_synthesis_prompt(range_info, records=records)
    assert "Q2 Central Science" in prompt
    assert "Viewer Lifecycle" in prompt
    assert "okr_kpi" in prompt
    assert "accomplishment" in prompt


def test_build_synthesis_prompt_requests_markdown_sections():
    range_info = {"role": "twitch", "start": "2019-05-01", "end": "2020-09-01", "label": "x"}
    records = [_rec("a", "bio", 0.9)[1]]
    prompt = build_synthesis_prompt(range_info, records=records)
    # Key section headings the v2 synthesizer must request
    for heading in ["Scope", "OKRs", "Tenets", "Accomplishments",
                    "Meeting patterns", "Strengths", "development areas"]:
        assert heading in prompt


def test_build_synthesis_prompt_includes_confidentiality_guard():
    range_info = {"role": "airbnb", "start": "2023-01-01", "end": "2026-02-01", "label": "x"}
    records = [_rec("a", "strategic_doc", 0.9)[1]]
    prompt = build_synthesis_prompt(range_info, records=records)
    # Some form of confidentiality instruction must appear
    assert "confidential" in prompt.lower() or "proprietary" in prompt.lower()


def test_build_synthesis_prompt_v2_includes_raw_content():
    """v2 prompt must surface full raw text for the top-signal records,
    clearly distinguished from the summary-only breadth records."""
    range_info = {"role": "airbnb", "start": "2024-05-01", "end": "2026-02-01", "label": "x"}
    raw = [(
        _rec("Composite Year-End 2025 Feedback.docx", "feedback_received", 0.97,
             date="2025", summary="formal year-end")[1],
        "the operator delivered extraordinary technical leadership. Strengths include structured writing, causal inference rigor, and cross-functional influence. Growth area: pacing.",
    )]
    summary = [_rec("b", "email_or_correspondence", 0.3, summary="minor meeting note")[1]]
    prompt = build_synthesis_prompt(range_info, raw_content_records=raw, summary_records=summary)
    assert "the operator delivered extraordinary technical leadership" in prompt
    assert "minor meeting note" in prompt
    assert "Composite Year-End 2025 Feedback" in prompt


def test_build_synthesis_prompt_v2_includes_meeting_patterns():
    range_info = {"role": "airbnb", "start": "2024-05-01", "end": "2026-02-01", "label": "x"}
    patterns = {
        "recurring_participants": [{"name": "Dan Schmierer", "count": 40, "cadence_guess": "weekly"}],
        "summary_paragraph": "the operator met weekly with Dan Schmierer as manager.",
    }
    prompt = build_synthesis_prompt(range_info, meeting_patterns=patterns)
    assert "Dan Schmierer" in prompt
    assert "Meeting patterns" in prompt


def test_build_synthesis_prompt_v2_instructs_strict_development_areas_rule():
    """The N>=2 rule is the load-bearing discipline. Must appear in the
    prompt so the LLM doesn't elevate single records to themes."""
    range_info = {"role": "airbnb", "start": "2024-05-01", "end": "2026-02-01", "label": "x"}
    prompt = build_synthesis_prompt(range_info, records=[])
    assert "TWO OR MORE" in prompt or "two or more" in prompt.lower()


# --- fetch_raw_content + pick_raw_records ---

def test_fetch_raw_content_filesystem_re_extracts(tmp_path: Path):
    p = tmp_path / "note.txt"
    p.write_text("Real content from the source document " * 20, encoding="utf-8")
    rec = {"path": str(p), "class": "bio"}
    content = fetch_raw_content(rec)
    assert "Real content from the source document" in content


def test_fetch_raw_content_workflowy_looks_up_in_map():
    rec = {"path": "wf://abc-123", "class": "feedback_received"}
    wf_map = {"wf://abc-123": "Meeting transcript with Jay Marine giving the operator feedback on leadership."}
    content = fetch_raw_content(rec, workflowy_texts=wf_map)
    assert "Jay Marine" in content


def test_fetch_raw_content_workflowy_without_map_returns_empty():
    rec = {"path": "wf://abc-123", "class": "feedback_received"}
    assert fetch_raw_content(rec) == ""


def test_fetch_raw_content_truncates_to_max_chars(tmp_path: Path):
    p = tmp_path / "long.txt"
    p.write_text("x" * 10000, encoding="utf-8")
    rec = {"path": str(p), "class": "bio"}
    content = fetch_raw_content(rec, max_chars=500)
    assert len(content) == 500


def test_fetch_raw_content_missing_file_returns_empty(tmp_path: Path):
    rec = {"path": str(tmp_path / "nope.txt"), "class": "bio"}
    assert fetch_raw_content(rec) == ""


def test_pick_raw_records_routes_by_class_and_limit():
    ranked = [
        _rec("a", "feedback_received", 0.95)[1],
        _rec("b", "bio", 0.9)[1],
        _rec("c", "email_or_correspondence", 0.8)[1],
        _rec("d", "accomplishment", 0.75)[1],
        _rec("e", "ephemeral", 0.7)[1],
    ]
    raw, rest = pick_raw_records(ranked, raw_limit=3)
    raw_classes = {r["class"] for r in raw}
    assert "feedback_received" in raw_classes
    assert "bio" in raw_classes
    assert "accomplishment" in raw_classes
    assert "email_or_correspondence" not in raw_classes
    # Non-raw classes go to rest
    rest_classes = {r["class"] for r in rest}
    assert "email_or_correspondence" in rest_classes
    assert "ephemeral" in rest_classes


def test_pick_raw_records_caps_at_raw_limit():
    # All high-signal feedback_received records — only raw_limit get raw
    ranked = [_rec(f"r{i}", "feedback_received", 0.9 - i * 0.01)[1] for i in range(30)]
    raw, rest = pick_raw_records(ranked, raw_limit=5)
    assert len(raw) == 5
    assert len(rest) == 25


def test_pick_raw_records_priority_override_wins_over_class():
    """A record with priority_override=True goes to raw tier even if
    its class isn't normally raw-eligible. Used for hand-flagged critical
    docs (e.g., comprehensive Amazon meeting-notes file that classifier
    mis-binned as ephemeral/email)."""
    ranked = [
        _rec("bio1", "bio", 0.98)[1],
        _rec("bio2", "bio", 0.97)[1],
        _rec("notes", "email_or_correspondence", 0.92, role="amazon")[1] | {"priority_override": True},
        _rec("fb", "feedback_received", 0.95)[1],
    ]
    raw, rest = pick_raw_records(ranked, raw_limit=3)
    raw_paths = {r["path"] for r in raw}
    assert "notes" in raw_paths   # priority-override wins even as email_or_correspondence
    assert len(raw) == 3


def test_pick_raw_records_priority_override_respects_raw_limit():
    # More priority-overrides than raw_limit → take first raw_limit
    ranked = [
        _rec(f"o{i}", "email_or_correspondence", 0.9)[1] | {"priority_override": True}
        for i in range(10)
    ]
    raw, rest = pick_raw_records(ranked, raw_limit=5)
    assert len(raw) == 5
    assert len(rest) == 5   # excess overrides fall to summary-only


# --- build_workflowy_text_map ---

def test_build_workflowy_text_map_returns_id_to_text():
    export = [
        {"id": "dt", "name": "Tue, Feb 11, 2026", "parent_id": None},
        {"id": "mtg1", "name": "1:1 with Jane", "parent_id": "dt"},
        {"id": "bullet", "name": "Discussed Q2 goals", "parent_id": "mtg1"},
        {"id": "mtg2", "name": "Standup", "parent_id": "dt"},
    ]
    text_map = build_workflowy_text_map(export)
    assert "wf://mtg1" in text_map
    assert "Jane" in text_map["wf://mtg1"]
    assert "Q2 goals" in text_map["wf://mtg1"]
    assert "wf://mtg2" in text_map


def test_build_workflowy_text_map_truncates_to_max_chars():
    # Large descendant tree; ensure truncation is honored
    export = [
        {"id": "dt", "name": "Tue, Feb 11, 2026", "parent_id": None},
        {"id": "mtg", "name": "Big meeting " * 1000, "parent_id": "dt"},
    ]
    text_map = build_workflowy_text_map(export, max_chars=100)
    assert len(text_map["wf://mtg"]) <= 100


# --- build_meeting_patterns_prompt + parse_meeting_patterns_response ---

def test_build_meeting_patterns_prompt_shows_records_and_schema():
    records = [
        _rec("wf://1", "email_or_correspondence", 0.6, date="2024-07-01",
             summary="1:1 with Dan Schmierer about team staffing and roadmap")[1],
        _rec("wf://2", "feedback_given", 0.8, date="2024-08-05",
             summary="1:1 coaching notes for James Sorenson covering promo path")[1],
    ]
    prompt = build_meeting_patterns_prompt("airbnb", records)
    assert "Dan Schmierer" in prompt
    assert "James Sorenson" in prompt
    assert "recurring_participants" in prompt
    assert "cross_functional_groups" in prompt
    assert "summary_paragraph" in prompt


def test_build_meeting_patterns_prompt_caps_records():
    records = [
        _rec(f"wf://{i}", "email_or_correspondence", 0.5, date="2024-01-01",
             summary=f"Meeting {i} notes")[1]
        for i in range(500)
    ]
    prompt = build_meeting_patterns_prompt("airbnb", records, max_records=50)
    # Only ~50 records surface in the prompt data block
    assert prompt.count("Meeting 499 notes") == 0   # 499 > max_records cap
    assert prompt.count("Meeting 0 notes") == 1     # 0 is in first slot


def test_parse_meeting_patterns_response_valid_json():
    raw = '{"recurring_participants": [{"name": "Dan", "approx_count": 40, "cadence_guess": "weekly", "relationship_guess": "manager"}], "cross_functional_groups": [{"group": "Product", "cadence": "biweekly", "likely_role": "cross-functional-partner"}], "notable_meeting_types": ["leadership sync"], "summary_paragraph": "the operator met weekly with Dan Schmierer."}'
    result = parse_meeting_patterns_response(raw)
    assert len(result["recurring_participants"]) == 1
    assert result["recurring_participants"][0]["name"] == "Dan"
    assert result["summary_paragraph"].startswith("the operator met")


def test_parse_meeting_patterns_response_strips_json_fences():
    """The LLM sometimes wraps output in ```json fences despite being told not to."""
    raw = '```json\n{"recurring_participants": [], "cross_functional_groups": [], "notable_meeting_types": [], "summary_paragraph": "ok"}\n```'
    result = parse_meeting_patterns_response(raw)
    assert result["summary_paragraph"] == "ok"


def test_parse_meeting_patterns_response_invalid_returns_empty():
    result = parse_meeting_patterns_response("not json")
    assert result["recurring_participants"] == []
    assert result["cross_functional_groups"] == []
    assert result["summary_paragraph"] == ""


def test_parse_meeting_patterns_response_empty_string():
    result = parse_meeting_patterns_response("")
    assert result["recurring_participants"] == []


def test_parse_meeting_patterns_response_partial_keys_fills_defaults():
    raw = '{"recurring_participants": [{"name": "Jane"}]}'
    result = parse_meeting_patterns_response(raw)
    assert result["recurring_participants"][0]["name"] == "Jane"
    # Missing keys default to empty
    assert result["cross_functional_groups"] == []
    assert result["notable_meeting_types"] == []
    assert result["summary_paragraph"] == ""
