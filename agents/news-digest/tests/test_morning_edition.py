"""Tests for news-digest/scripts/morning-edition.py.

Phase-3b-fix architecture: Python owns selection, LLM only annotates.

  1. `load_ranked_articles` — reads cache/ranked-<today>.json
  2. `select_items` — pre-selects ~18 articles with LinkedIn reservation,
     assigns num 1..N
  3. `build_prompt` — asks the LLM to annotate each selected item
     (keyed by id) with a category + extended_headline
  4. `parse_response` — extracts {id: {category, extended_headline}}
     from the json_mode object the Responses API returns
  5. `merge_annotations` — merges LLM annotations onto the selected
     items, with deterministic fallbacks when the LLM drops/misses
  6. `write_morning_items` — atomic JSON write for morning-fleet-deliver
  7. `write_item_map` — atomic JSON write for engagement-poller lookup

This architecture fixes the Phase 3b day-1 bugs:
  - LLM dropping LinkedIn entirely (now Python reserves LinkedIn slots)
  - LLM ignoring "top 15-20" soft constraint (Python enforces exact count)
  - engagement-poller seeing an empty item-map (write_item_map always fires)

Tests monkeypatch agents.shared.llm.infer — no real network calls.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.shared.llm import InferResult

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_morning_edition():
    import types
    for name in ("feedparser",):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    path = SCRIPTS_DIR / "morning-edition.py"
    spec = importlib.util.spec_from_file_location("morning_edition", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_morning_edition()


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "news-digest-workspace"
    cache = ws / "cache"
    cache.mkdir(parents=True)
    me = _load_morning_edition()
    monkeypatch.setattr(me, "WORKSPACE", ws)
    monkeypatch.setattr(me, "CACHE_DIR", cache)
    return me, ws, cache


def _write_ranked(cache: Path, articles: list[dict]) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = cache / f"ranked-{today}.json"
    path.write_text(json.dumps({"articles": articles}), encoding="utf-8")
    return path


def _article(
    id_: str,
    title: str,
    source: str,
    rank: float,
    *,
    topics: list[str] | None = None,
    source_label: str | None = None,
    link: str | None = None,
) -> dict:
    return {
        "id": id_,
        "title": title,
        "summary": f"summary of {title}",
        "link": link or f"https://{source}.example.com/{id_}",
        "source": source,
        "source_label": source_label or source.upper(),
        "topics": topics or ["general"],
        "rank": rank,
    }


def _mixed_feed(
    *, non_li: int = 25, linkedin: int = 6
) -> list[dict]:
    """Build a realistic ranked feed: high-ranked non-LinkedIn items
    followed by lower-ranked LinkedIn items (LinkedIn usually ranks
    below news in real data, which is why selection drops it without
    explicit reservation)."""
    articles = []
    rank = 0.99
    for i in range(non_li):
        articles.append(_article(
            f"n{i:02d}",
            f"News item {i}",
            ["nyt", "wsj", "wapo", "google_news", "slate"][i % 5],
            rank,
            topics=[
                ["ai"], ["economics"], ["international_politics"],
                ["us_domestic_policy"], ["business"],
            ][i % 5],
        ))
        rank -= 0.01
    for i in range(linkedin):
        articles.append(_article(
            f"l{i:02d}",
            f"LinkedIn update from Person {i}",
            "linkedin",
            rank,
            topics=["linkedin"],
            source_label="LinkedIn (Like likes)",
        ))
        rank -= 0.01
    return articles


# ─── load_ranked_articles ────────────────────────────────────────────


def test_load_ranked_articles_reads_today_file(fake_workspace):
    me, ws, cache = fake_workspace
    _write_ranked(cache, _mixed_feed(non_li=5, linkedin=2))

    articles, date_str = me.load_ranked_articles()

    assert len(articles) == 7
    assert date_str == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_load_ranked_articles_raises_when_file_missing(fake_workspace):
    me, ws, cache = fake_workspace
    with pytest.raises(FileNotFoundError):
        me.load_ranked_articles()


def test_load_ranked_articles_raises_when_empty(fake_workspace):
    me, ws, cache = fake_workspace
    _write_ranked(cache, [])
    with pytest.raises(RuntimeError, match="no articles"):
        me.load_ranked_articles()


# ─── select_items ────────────────────────────────────────────────────


def test_select_items_returns_exactly_target_total(mod):
    articles = _mixed_feed(non_li=25, linkedin=6)
    selected = mod.select_items(articles)
    assert len(selected) == mod.TARGET_TOTAL


def test_select_items_reserves_linkedin_slots(mod):
    """Even when LinkedIn items rank below news, some LinkedIn must
    survive the cut."""
    articles = _mixed_feed(non_li=25, linkedin=6)
    selected = mod.select_items(articles)

    linkedin_selected = [s for s in selected if s.get("source") == "linkedin"]
    assert len(linkedin_selected) == mod.LINKEDIN_RESERVED
    assert len(linkedin_selected) >= 2  # at least a meaningful signal


def test_select_items_caps_linkedin_at_available_count(mod):
    """If LinkedIn has fewer items than the reserved slot count,
    take what's available and fill the rest with non-LinkedIn."""
    articles = _mixed_feed(non_li=25, linkedin=2)  # only 2 LinkedIn
    selected = mod.select_items(articles)
    assert len(selected) == mod.TARGET_TOTAL
    linkedin_selected = [s for s in selected if s.get("source") == "linkedin"]
    assert len(linkedin_selected) == 2  # both taken, no fabrication


def test_select_items_no_linkedin_available(mod):
    """With no LinkedIn at all, selection is pure rank order."""
    articles = _mixed_feed(non_li=30, linkedin=0)
    selected = mod.select_items(articles)
    assert len(selected) == mod.TARGET_TOTAL
    assert all(s.get("source") != "linkedin" for s in selected)


def test_select_items_fewer_articles_than_target(mod):
    """If the ranked feed has fewer than TARGET_TOTAL articles, return
    all of them rather than padding or erroring."""
    articles = _mixed_feed(non_li=5, linkedin=2)  # 7 total
    selected = mod.select_items(articles)
    assert len(selected) == 7


def test_select_items_assigns_sequential_num(mod):
    articles = _mixed_feed(non_li=25, linkedin=6)
    selected = mod.select_items(articles)
    nums = [s["num"] for s in selected]
    assert nums == list(range(1, len(selected) + 1))


def test_select_items_preserves_article_fields(mod):
    articles = _mixed_feed(non_li=20, linkedin=4)
    selected = mod.select_items(articles)
    for item in selected:
        for field in ("id", "title", "link", "source", "source_label", "topics"):
            assert field in item, f"{field} missing after select_items"


def test_select_items_top_ranked_non_linkedin_always_present(mod):
    """The single highest-ranked non-LinkedIn article must be in the
    selected set (regression gate for a future LinkedIn-heavy bias bug)."""
    articles = _mixed_feed(non_li=25, linkedin=6)
    selected = mod.select_items(articles)
    top_non_li = max(
        (a for a in articles if a["source"] != "linkedin"),
        key=lambda a: a["rank"],
    )
    ids = {s["id"] for s in selected}
    assert top_non_li["id"] in ids


# ─── LinkedIn notifications & messages (Bug #7) ───────────────────────


def _linkedin_notif(id_: str, title: str, rank: float) -> dict:
    return {
        "id": id_, "title": title, "summary": title,
        "link": "https://www.linkedin.com/notifications/",
        "source": "linkedin",
        "source_label": "LinkedIn Notification",
        "topics": ["linkedin"],
        "rank": rank,
        "_is_notification": True,
    }


def _linkedin_msg(id_: str, title: str, rank: float) -> dict:
    return {
        "id": id_, "title": title, "summary": title,
        "link": "https://www.linkedin.com/messaging/",
        "source": "linkedin",
        "source_label": "LinkedIn Message",
        "topics": ["linkedin"],
        "rank": rank,
        "_is_message": True,
    }


def test_select_items_includes_notifications_at_end(mod):
    """LinkedIn notifications were being dropped because they compete
    against higher-scored feed posts for LINKEDIN_RESERVED=4 slots.
    They must always appear in the selection — outside the main
    target_total pool — so the operator sees them every morning."""
    articles = _mixed_feed(non_li=25, linkedin=4)
    articles.extend([
        _linkedin_notif("nn0", "notif 0", 0.30),
        _linkedin_notif("nn1", "notif 1", 0.25),
        _linkedin_notif("nn2", "notif 2", 0.20),
    ])
    selected = mod.select_items(articles)
    notif_ids = {s["id"] for s in selected if s.get("_is_notification")}
    assert notif_ids == {"nn0", "nn1", "nn2"}, (
        f"every notification must be selected, got {notif_ids}"
    )


def test_select_items_drops_profile_view_notifications(mod):
    """2026-04-18: profile-view and other low-signal notifications were
    landing in the digest because notifications bypass the LLM
    categorization pass. Keyword-blacklist them at select_items so the
    morning digest only carries notifications worth the operator's attention."""
    articles = _mixed_feed(non_li=25, linkedin=0)
    articles.extend([
        _linkedin_notif("pv0", "Omar Shahine viewed your profile. See all views.", 0.5),
        _linkedin_notif("pv1", "Someone viewed your profile", 0.5),
        _linkedin_notif("rx0", "Tom reacted to your post", 0.5),
        _linkedin_notif("en0", "Lisa endorsed you for Product Management", 0.5),
        _linkedin_notif("an0", "Celebrate Kate's work anniversary", 0.5),
        _linkedin_notif("fo0", "Jane is now following you", 0.5),
        _linkedin_notif("bd0", "Today is Alex's birthday", 0.5),
    ])
    selected = mod.select_items(articles)
    notif_ids = {s["id"] for s in selected if s.get("_is_notification")}
    assert notif_ids == set(), f"low-signal notifications must be dropped, got {notif_ids}"


def test_select_items_keeps_substantive_notifications(mod):
    """A mention, a new job, a comment, or a DM-preview notification is
    signal — those must survive the filter."""
    articles = _mixed_feed(non_li=25, linkedin=0)
    articles.extend([
        _linkedin_notif("m0", "Sarah mentioned you in a comment", 0.6),
        _linkedin_notif("c0", "New comment on your post about AI agents", 0.5),
        _linkedin_notif("j0", "Dan Zylberglejd started a new position as CTO at Anthropic", 0.4),
    ])
    selected = mod.select_items(articles)
    notif_ids = {s["id"] for s in selected if s.get("_is_notification")}
    assert notif_ids == {"m0", "c0", "j0"}, f"substantive notifications should pass, got {notif_ids}"


def test_select_items_includes_messages_at_end(mod):
    articles = _mixed_feed(non_li=25, linkedin=4)
    articles.extend([
        _linkedin_msg("mm0", "msg 0", 0.30),
        _linkedin_msg("mm1", "msg 1", 0.25),
    ])
    selected = mod.select_items(articles)
    msg_ids = {s["id"] for s in selected if s.get("_is_message")}
    assert msg_ids == {"mm0", "mm1"}


def test_select_items_notifications_do_not_compete_with_feed_posts(mod):
    """Notifications and messages being added must not shrink the
    number of feed LinkedIn items selected — the feed pool still gets
    LINKEDIN_RESERVED=4 of its own slots."""
    articles = _mixed_feed(non_li=25, linkedin=6)  # 6 feed posts
    articles.extend([
        _linkedin_notif("nn0", "notif 0", 0.30),
        _linkedin_msg("mm0", "msg 0", 0.25),
    ])
    selected = mod.select_items(articles)
    feed_selected = [
        s for s in selected
        if s.get("source") == "linkedin"
        and not s.get("_is_notification")
        and not s.get("_is_message")
    ]
    assert len(feed_selected) == mod.LINKEDIN_RESERVED


def test_fallback_category_routes_notification_and_message_to_own_sections(mod):
    notif = _linkedin_notif("nn0", "notif 0", 0.30)
    msg = _linkedin_msg("mm0", "msg 0", 0.25)
    assert mod._fallback_category(notif) == "🔔 LinkedIn Notifications"
    assert mod._fallback_category(msg) == "💬 LinkedIn Messages"


def test_merge_annotations_overrides_llm_category_for_notifs_and_msgs(mod):
    """Even if the LLM labels a notification/message as 🔗 LinkedIn,
    merge_annotations forces the distinct category so the digest
    always renders notifications and messages as their own sections."""
    selected = [
        {**_linkedin_notif("nn0", "notif 0", 0.30), "num": 1},
        {**_linkedin_msg("mm0", "msg 0", 0.25), "num": 2},
    ]
    annotations = [
        {"id": "nn0", "category": "🔗 LinkedIn", "extended_headline": "n0"},
        {"id": "mm0", "category": "🔗 LinkedIn", "extended_headline": "m0"},
    ]
    merged = mod.merge_annotations(selected, annotations)
    cats = {m["id"]: m["category"] for m in merged}
    assert cats["nn0"] == "🔔 LinkedIn Notifications"
    assert cats["mm0"] == "💬 LinkedIn Messages"


def test_merge_annotations_notification_headline_is_deterministic(mod):
    """Notifications must NOT take the LLM's extended_headline — they
    render from scraped text + time_ago directly. Regression gate for
    the "A LinkedIn notification showing a recruiter viewed the
    profile signals potential hiring interest" generic framing the operator
    flagged 2026-04-20."""
    notif = {**_linkedin_notif("nn0", "Sarah mentioned you in a comment", 0.3), "num": 1}
    notif["time_ago"] = "2h"
    annotations = [
        {"id": "nn0", "category": "🔔 LinkedIn Notifications",
         "extended_headline": "A LinkedIn notification showing Sarah mentioned you signals growing engagement."},
    ]
    merged = mod.merge_annotations([notif], annotations)
    headline = merged[0]["extended_headline"]
    # Deterministic shape: text (time_ago); no LLM wrapper prose
    assert "A LinkedIn notification" not in headline
    assert "Sarah mentioned you in a comment" in headline
    assert "(2h)" in headline


def test_render_notification_headline_strips_see_more_tail(mod):
    """LinkedIn notification text often ends with 'See all views' /
    'See more' CTAs. Strip them so the rendered line is substantive."""
    notif = {
        "id": "pv", "summary": "Omar reacted to your post See all views",
        "title": "Omar reacted", "time_ago": "3h",
        "_is_notification": True, "source": "linkedin",
    }
    out = mod._render_notification_headline(notif)
    assert out == "Omar reacted to your post (3h)"


def test_render_notification_headline_no_time_ago(mod):
    notif = {"summary": "Dan started a new position at Anthropic", "time_ago": ""}
    out = mod._render_notification_headline(notif)
    assert out == "Dan started a new position at Anthropic"
    assert "()" not in out


# ─── build_prompt ────────────────────────────────────────────────────


def test_build_prompt_includes_all_selected_items(mod):
    articles = _mixed_feed(non_li=15, linkedin=3)
    selected = mod.select_items(articles)
    prompt = mod.build_prompt(selected)
    for item in selected:
        assert item["id"] in prompt, f"id {item['id']} missing from prompt"


def test_build_prompt_mentions_category_labels(mod):
    articles = _mixed_feed(non_li=15, linkedin=3)
    selected = mod.select_items(articles)
    prompt = mod.build_prompt(selected)
    for label in (
        "🤖 AI & Tech", "💰 Economics", "🌍 World",
        "🏛️ US Policy", "🔗 LinkedIn", "📋 Also Noted",
    ):
        assert label in prompt


def test_build_prompt_asks_for_items_object_keyed_by_id(mod):
    articles = _mixed_feed(non_li=15, linkedin=3)
    selected = mod.select_items(articles)
    prompt = mod.build_prompt(selected)
    assert '"items"' in prompt
    assert "JSON object" in prompt or "json object" in prompt.lower()
    assert '"id"' in prompt
    assert '"category"' in prompt
    assert '"extended_headline"' in prompt


def test_build_prompt_excludes_notifications_and_messages(mod):
    """Notifications and messages don't need LLM rewording — they
    render deterministically from the scraped text. Keeping them OUT
    of the prompt saves tokens and prevents the "A LinkedIn
    notification showing…" generic framing the operator flagged 2026-04-20."""
    articles = _mixed_feed(non_li=10, linkedin=2)
    articles.extend([
        _linkedin_notif("nn0", "Sarah mentioned you in a comment", 0.3),
        _linkedin_msg("mm0", "Jane Doe: quick question about…", 0.25),
    ])
    selected = mod.select_items(articles)
    prompt = mod.build_prompt(selected)
    assert "nn0" not in prompt
    assert "mm0" not in prompt


def test_build_prompt_includes_author_for_linkedin_items(mod):
    """LinkedIn feed items must carry author + author_headline into
    the prompt so the LLM can lead with credibility ("Dario Amodei,
    Anthropic CEO: …") instead of the generic "A LinkedIn post
    argues…" framing the operator flagged 2026-04-20."""
    articles = _mixed_feed(non_li=5, linkedin=0)
    articles.append({
        "id": "li_author",
        "title": "Dario Amodei: AI agents are…",
        "summary": "body of post",
        "link": "https://linkedin.com/x",
        "source": "linkedin",
        "source_label": "LinkedIn",
        "topics": ["linkedin"],
        "rank": 0.9,
        "author": "Dario Amodei",
        "author_headline": "CEO at Anthropic",
    })
    selected = mod.select_items(articles)
    prompt = mod.build_prompt(selected)
    assert "Dario Amodei" in prompt
    assert "Anthropic" in prompt
    assert '"author"' in prompt
    assert '"author_headline"' in prompt


def test_build_prompt_forbids_generic_linkedin_framing(mod):
    """The prompt must explicitly tell the LLM NOT to write
    'A LinkedIn post argues…' / 'A LinkedIn update says…' — that
    generic framing is exactly what the operator complained about."""
    articles = _mixed_feed(non_li=5, linkedin=2)
    selected = mod.select_items(articles)
    prompt = mod.build_prompt(selected)
    lower = prompt.lower()
    assert "a linkedin post" in lower or "generic platform" in lower
    assert "name the person" in lower or "lead with the author" in lower


def test_build_prompt_discourages_also_noted_overuse(mod):
    """Regression gate for the Phase-3b-followup category skew: the
    first real run put 4 items in 📋 Also Noted with only 1 Econ and
    1 US Policy. The prompt now explicitly tells the LLM to avoid
    defaulting to Also Noted and prefer a topic-specific bucket."""
    articles = _mixed_feed(non_li=15, linkedin=3)
    selected = mod.select_items(articles)
    prompt = mod.build_prompt(selected)
    lower = prompt.lower()
    # The prompt mentions "only" near "Also Noted" — an explicit
    # "Also Noted only as a fallback" directive — and asks for spread.
    assert "📋 also noted" in lower
    assert "only" in lower
    assert "fallback" in lower or "no other" in lower


# ─── parse_response ─────────────────────────────────────────────────


def _llm_annotations(selected: list[dict]) -> list[dict]:
    return [
        {
            "id": item["id"],
            "category": "🤖 AI & Tech",
            "extended_headline": f"Why {item['title']} matters",
        }
        for item in selected
    ]


def test_parse_response_extracts_items_from_wrapper(mod):
    text = json.dumps({"items": [
        {"id": "a1", "category": "🤖 AI & Tech", "extended_headline": "x"},
        {"id": "a2", "category": "💰 Economics", "extended_headline": "y"},
    ]})
    annotations = mod.parse_response(text)
    assert len(annotations) == 2
    assert annotations[0]["id"] == "a1"


def test_parse_response_unwraps_markdown_fence(mod):
    fenced = "```json\n" + json.dumps({"items": [
        {"id": "a1", "category": "🤖 AI & Tech", "extended_headline": "x"},
    ]}) + "\n```"
    annotations = mod.parse_response(fenced)
    assert len(annotations) == 1


def test_parse_response_raises_on_missing_items_key(mod):
    text = json.dumps({"results": []})
    with pytest.raises(ValueError, match="items"):
        mod.parse_response(text)


# ─── merge_annotations ──────────────────────────────────────────────


def test_merge_annotations_happy_path(mod):
    articles = _mixed_feed(non_li=15, linkedin=3)
    selected = mod.select_items(articles)
    annotations = _llm_annotations(selected)

    merged = mod.merge_annotations(selected, annotations)

    assert len(merged) == len(selected)
    for m, s in zip(merged, selected):
        assert m["num"] == s["num"]
        assert m["id"] == s["id"]
        assert m["title"] == s["title"]
        assert m["category"] == "🤖 AI & Tech"
        assert "matters" in m["extended_headline"]


def test_merge_annotations_falls_back_when_llm_drops_item(mod):
    """If the LLM's response is missing an id, the merged item still
    exists with a fallback category + headline — Python owns the final
    list and doesn't let the LLM silently vanish items."""
    articles = _mixed_feed(non_li=10, linkedin=2)
    selected = mod.select_items(articles)
    # LLM annotates only half
    annotations = _llm_annotations(selected)[: len(selected) // 2]

    merged = mod.merge_annotations(selected, annotations)

    assert len(merged) == len(selected)
    # Items the LLM didn't annotate get a non-empty fallback category
    fallback_items = [m for m in merged if m["id"] not in {a["id"] for a in annotations}]
    assert all(m["category"] for m in fallback_items)
    assert all(m["extended_headline"] for m in fallback_items)


def test_merge_annotations_preserves_source_short_form(mod):
    """engagement-poller reads item.source (the short-form key like
    'nyt' or 'linkedin') — merged output must carry it through."""
    articles = _mixed_feed(non_li=15, linkedin=3)
    selected = mod.select_items(articles)
    annotations = _llm_annotations(selected)

    merged = mod.merge_annotations(selected, annotations)

    for m in merged:
        assert "source" in m
        assert m["source"] != ""


def test_merge_annotations_ignores_extra_llm_fields(mod):
    """The LLM might echo back extra fields we didn't ask for. Don't
    let those clobber our Python-owned fields."""
    articles = _mixed_feed(non_li=5, linkedin=1)
    selected = mod.select_items(articles)
    annotations = [
        {
            "id": selected[0]["id"],
            "category": "🤖 AI & Tech",
            "extended_headline": "x",
            "title": "LLM HALLUCINATED TITLE",  # should not replace real title
            "url": "https://evil.example.com",  # should not replace real url
        }
    ]

    merged = mod.merge_annotations(selected, annotations)

    first = merged[0]
    assert first["title"] == selected[0]["title"]
    assert first["url"] == selected[0]["link"]


# ─── group_by_section ───────────────────────────────────────────────


def _merged_item(num: int, id_: str, category: str, source: str = "nyt") -> dict:
    return {
        "num": num,
        "id": id_,
        "category": category,
        "extended_headline": f"why {id_}",
        "title": id_,
        "url": f"https://x/{id_}",
        "source_label": source.upper(),
        "source": source,
        "topics": [],
    }


def test_group_by_section_orders_sections_by_category_labels(mod):
    """Sections appear in CATEGORY_LABELS order, not rank-mixed order."""
    merged = [
        _merged_item(1, "a", "💰 Economics"),
        _merged_item(2, "b", "🤖 AI & Tech"),
        _merged_item(3, "c", "🏛️ US Policy"),
        _merged_item(4, "d", "🤖 AI & Tech"),
        _merged_item(5, "e", "🔗 LinkedIn", source="linkedin"),
    ]
    grouped = mod.group_by_section(merged)

    categories = [g["category"] for g in grouped]
    assert categories == [
        "🤖 AI & Tech",
        "🤖 AI & Tech",
        "💰 Economics",
        "🏛️ US Policy",
        "🔗 LinkedIn",
    ]


def test_group_by_section_preserves_rank_within_section(mod):
    """Stable sort — within each section, items retain the rank order
    they arrived in (merged is already rank-sorted)."""
    merged = [
        _merged_item(1, "ai_hi", "🤖 AI & Tech"),
        _merged_item(2, "econ_hi", "💰 Economics"),
        _merged_item(3, "ai_lo", "🤖 AI & Tech"),
        _merged_item(4, "econ_lo", "💰 Economics"),
    ]
    grouped = mod.group_by_section(merged)
    assert [g["id"] for g in grouped] == ["ai_hi", "ai_lo", "econ_hi", "econ_lo"]


def test_group_by_section_reassigns_sequential_num_in_new_order(mod):
    merged = [
        _merged_item(1, "a", "💰 Economics"),
        _merged_item(2, "b", "🤖 AI & Tech"),
        _merged_item(3, "c", "🤖 AI & Tech"),
    ]
    grouped = mod.group_by_section(merged)
    assert [g["num"] for g in grouped] == [1, 2, 3]
    assert grouped[0]["category"] == "🤖 AI & Tech"
    assert grouped[0]["id"] == "b"


def test_group_by_section_unknown_category_sorts_last(mod):
    merged = [
        _merged_item(1, "u", "🤯 Unknown"),
        _merged_item(2, "ai", "🤖 AI & Tech"),
        _merged_item(3, "also", "📋 Also Noted"),
    ]
    grouped = mod.group_by_section(merged)
    cats = [g["category"] for g in grouped]
    assert cats[0] == "🤖 AI & Tech"
    assert cats[1] == "📋 Also Noted"
    assert cats[2] == "🤯 Unknown"


def test_group_by_section_does_not_mutate_input(mod):
    merged = [
        _merged_item(1, "a", "💰 Economics"),
        _merged_item(2, "b", "🤖 AI & Tech"),
    ]
    _ = mod.group_by_section(merged)
    assert merged[0]["num"] == 1
    assert merged[0]["id"] == "a"
    assert merged[1]["num"] == 2
    assert merged[1]["id"] == "b"


def test_run_output_is_sectioned(fake_workspace):
    """End-to-end: morning-items.json is written grouped by section
    with contiguous category runs — no category appears, disappears,
    and reappears."""
    me, ws, cache = fake_workspace
    articles = _mixed_feed(non_li=20, linkedin=4)
    _write_ranked(cache, articles)
    selected = me.select_items(articles)

    categories_cycle = [
        "🤖 AI & Tech",
        "💰 Economics",
        "🏛️ US Policy",
        "🌍 World",
    ]

    def _annotate(items: list[dict]) -> list[dict]:
        return [
            {
                "id": it["id"],
                "category": (
                    "🔗 LinkedIn"
                    if it.get("source") == "linkedin"
                    else categories_cycle[i % 4]
                ),
                "extended_headline": f"why {it['id']}",
            }
            for i, it in enumerate(items)
        ]

    fake_result = InferResult(
        text=json.dumps({"items": _annotate(selected)}),
        model="gpt-5.4",
        input_tokens=800,
        output_tokens=300,
        total_tokens=1100,
    )
    with patch("agents.shared.llm.infer", return_value=fake_result):
        me.run()

    items = json.loads((cache / "morning-items.json").read_text(encoding="utf-8"))
    seen: list[str] = []
    prev = None
    for it in items:
        cat = it["category"]
        if cat != prev:
            assert cat not in seen, (
                f"category {cat!r} reappears after {prev!r} — items not grouped"
            )
            seen.append(cat)
            prev = cat


def test_run_item_map_keys_match_sectioned_num(fake_workspace):
    """item-map-<date>.json must key on the sectioned num, not the
    original select_items num, so engagement callbacks resolve."""
    me, ws, cache = fake_workspace
    articles = _mixed_feed(non_li=20, linkedin=4)
    _write_ranked(cache, articles)
    selected = me.select_items(articles)

    def _annotate(items: list[dict]) -> list[dict]:
        return [
            {
                "id": it["id"],
                "category": (
                    "🔗 LinkedIn"
                    if it.get("source") == "linkedin"
                    else ["🤖 AI & Tech", "💰 Economics"][i % 2]
                ),
                "extended_headline": f"why {it['id']}",
            }
            for i, it in enumerate(items)
        ]

    fake_result = InferResult(
        text=json.dumps({"items": _annotate(selected)}),
        model="gpt-5.4",
        input_tokens=800,
        output_tokens=300,
        total_tokens=1100,
    )
    with patch("agents.shared.llm.infer", return_value=fake_result):
        me.run()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items = json.loads((cache / "morning-items.json").read_text(encoding="utf-8"))
    item_map = json.loads((cache / f"item-map-{today}.json").read_text(encoding="utf-8"))

    for it in items:
        key = str(it["num"])
        assert key in item_map, f"num {key} missing from item-map"
        assert item_map[key]["id"] == it["id"]


# ─── write_morning_items ────────────────────────────────────────────


def test_write_morning_items_atomic_no_tmp_leftover(fake_workspace):
    me, ws, cache = fake_workspace
    items = [{
        "num": 1, "id": "a1", "category": "🤖 AI & Tech",
        "extended_headline": "x", "title": "t", "url": "https://u",
        "source_label": "s", "source": "nyt", "topics": ["ai"],
    }]
    me.write_morning_items(items, "2026-04-14")
    assert (cache / "morning-items.json").exists()
    assert not (cache / "morning-items.json.tmp").exists()


def test_write_morning_items_overwrites_previous(fake_workspace):
    me, ws, cache = fake_workspace
    (cache / "morning-items.json").write_text("[]")
    items = [{
        "num": 1, "id": "a1", "category": "🤖 AI & Tech",
        "extended_headline": "x", "title": "t", "url": "https://u",
        "source_label": "s", "source": "nyt", "topics": ["ai"],
    }]
    me.write_morning_items(items, "2026-04-14")
    data = json.loads((cache / "morning-items.json").read_text())
    assert len(data) == 1


# ─── write_item_map ─────────────────────────────────────────────────


def test_write_item_map_creates_today_file(fake_workspace):
    me, ws, cache = fake_workspace
    items = [
        {
            "num": 1, "id": "a1", "title": "t1", "url": "https://u1",
            "source": "nyt", "source_label": "NYT", "topics": ["ai"],
            "category": "🤖 AI & Tech", "extended_headline": "h1",
        },
        {
            "num": 2, "id": "a2", "title": "t2", "url": "https://u2",
            "source": "wsj", "source_label": "WSJ", "topics": ["economics"],
            "category": "💰 Economics", "extended_headline": "h2",
        },
    ]
    me.write_item_map(items, "2026-04-14")
    path = cache / "item-map-2026-04-14.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert set(data.keys()) == {"1", "2"}
    assert data["1"] == {
        "id": "a1",
        "title": "t1",
        "topics": ["ai"],
        "source": "nyt",
        "link": "https://u1",
    }


def test_write_item_map_shape_matches_engagement_poller_reads(fake_workspace):
    """engagement-poller does article.get('id'), .get('title'),
    .get('topics', []), .get('source', ''). The item-map values must
    have those keys — not source_label, not url."""
    me, ws, cache = fake_workspace
    items = [{
        "num": 1, "id": "a1", "title": "t", "url": "https://u",
        "source": "nyt", "source_label": "NYT", "topics": ["ai"],
        "category": "🤖 AI & Tech", "extended_headline": "h",
    }]
    me.write_item_map(items, "2026-04-14")
    data = json.loads((cache / "item-map-2026-04-14.json").read_text())
    entry = data["1"]
    assert "id" in entry
    assert "title" in entry
    assert "topics" in entry
    assert "source" in entry
    # engagement-poller doesn't use source_label — it's not in the map
    assert "source_label" not in entry


def test_write_item_map_atomic(fake_workspace):
    me, ws, cache = fake_workspace
    items = [{
        "num": 1, "id": "a1", "title": "t", "url": "https://u",
        "source": "nyt", "source_label": "NYT", "topics": [],
        "category": "x", "extended_headline": "h",
    }]
    me.write_item_map(items, "2026-04-14")
    assert not (cache / "item-map-2026-04-14.json.tmp").exists()


# ─── run() orchestrator ─────────────────────────────────────────────


def _fake_infer_annotations(selected: list[dict]) -> InferResult:
    return InferResult(
        text=json.dumps({"items": _llm_annotations(selected)}),
        model="gpt-5.4",
        input_tokens=800,
        output_tokens=300,
        total_tokens=1100,
    )


def test_run_writes_both_output_files(fake_workspace):
    me, ws, cache = fake_workspace
    articles = _mixed_feed(non_li=20, linkedin=5)
    _write_ranked(cache, articles)

    # Build annotations to match what select_items will produce
    selected = me.select_items(articles)
    with patch("agents.shared.llm.infer", return_value=_fake_infer_annotations(selected)):
        result = me.run()

    assert result["status"] == "ok"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert (cache / "morning-items.json").exists()
    assert (cache / f"item-map-{today}.json").exists()


def test_run_items_count_matches_selection_target(fake_workspace):
    me, ws, cache = fake_workspace
    articles = _mixed_feed(non_li=25, linkedin=6)
    _write_ranked(cache, articles)
    selected = me.select_items(articles)
    with patch("agents.shared.llm.infer", return_value=_fake_infer_annotations(selected)):
        result = me.run()
    assert result["items_count"] == me.TARGET_TOTAL


def test_run_includes_linkedin_items_in_output(fake_workspace):
    me, ws, cache = fake_workspace
    articles = _mixed_feed(non_li=25, linkedin=6)
    _write_ranked(cache, articles)
    selected = me.select_items(articles)
    with patch("agents.shared.llm.infer", return_value=_fake_infer_annotations(selected)):
        me.run()

    items = json.loads((cache / "morning-items.json").read_text())
    linkedin = [i for i in items if i.get("source") == "linkedin"]
    assert len(linkedin) > 0, "no LinkedIn items in morning-items.json"


def test_run_passes_json_mode_true_to_infer(fake_workspace):
    me, ws, cache = fake_workspace
    articles = _mixed_feed(non_li=15, linkedin=3)
    _write_ranked(cache, articles)
    selected = me.select_items(articles)

    captured = {}
    def fake_infer(prompt, **kwargs):
        captured.update(kwargs)
        return _fake_infer_annotations(selected)

    with patch("agents.shared.llm.infer", side_effect=fake_infer):
        me.run()
    assert captured.get("json_mode") is True


def test_run_raises_on_infer_failure(fake_workspace):
    me, ws, cache = fake_workspace
    _write_ranked(cache, _mixed_feed(non_li=15, linkedin=3))
    err = InferResult(text="", error="network down", returncode=500)
    with patch("agents.shared.llm.infer", return_value=err):
        with pytest.raises(RuntimeError, match="infer failed"):
            me.run()


def test_run_raises_on_missing_ranked_file(fake_workspace):
    me, ws, cache = fake_workspace
    with patch("agents.shared.llm.infer", return_value=_fake_infer_annotations([])):
        with pytest.raises(FileNotFoundError):
            me.run()


# ─── main() SCRIPT_CONTRACT wrapper ─────────────────────────────────


def test_main_returns_zero_on_happy_path(fake_workspace, capsys):
    me, ws, cache = fake_workspace
    articles = _mixed_feed(non_li=15, linkedin=3)
    _write_ranked(cache, articles)
    selected = me.select_items(articles)
    with patch("agents.shared.llm.infer", return_value=_fake_infer_annotations(selected)):
        rc = me.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    payload = json.loads(out)
    assert payload["status"] == "ok"


def test_main_returns_zero_on_infer_failure(fake_workspace, capsys):
    me, ws, cache = fake_workspace
    _write_ranked(cache, _mixed_feed(non_li=15, linkedin=3))
    err = InferResult(text="", error="auth expired", returncode=401)
    with patch("agents.shared.llm.infer", return_value=err):
        rc = me.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "🐛" in payload["alert"]


def test_main_returns_zero_on_missing_ranked_file(fake_workspace, capsys):
    me, ws, cache = fake_workspace
    rc = me.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"


def test_main_imports_shared_llm_module():
    src = (SCRIPTS_DIR / "morning-edition.py").read_text(encoding="utf-8")
    assert "agents.shared.llm" in src or "from agents.shared import llm" in src
