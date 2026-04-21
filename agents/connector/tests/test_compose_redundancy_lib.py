"""Tests for agents/connector/scripts/compose_redundancy_lib.py.

The redundancy pass is a second LLM call that prunes sentences the
recipient already knows — either from the inbound message they just
sent (echoing their own words back at them is the classic case) or
from previously-established known_by facts. Intentional mirroring for
emotional / rapport reasons survives the prune when the strategy
explicitly calls for it.

Contract pinned here:
- Always-on when reply_needed=true and draft has more than 2 sentences;
  skipped otherwise.
- Degrades open: malformed LLM JSON, LLM error, empty pruned result,
  or pruned draft that shrinks below 30% of the original all roll back
  to the original draft with no harm done.
- Preserves intentional mirrors called out in the strategy.
- Returns structured metadata (removed_sentences + preserved_as_mirror)
  so callers can surface the prune summary to Telegram.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

_spec = importlib.util.spec_from_file_location(
    "compose_redundancy_lib", _SCRIPTS_DIR / "compose_redundancy_lib.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _ok(payload: dict):
    return SimpleNamespace(
        ok=True, text=json.dumps(payload), error=None,
        input_tokens=0, output_tokens=0, model="fake",
    )


def _err():
    return SimpleNamespace(
        ok=False, text=None, error="down",
        input_tokens=0, output_tokens=0, model=None,
    )


def _inbound(body: str = "", subject: str = "Re: catching up") -> dict:
    return {
        "from_name": "Jamie Fitzgerald",
        "from_email": "jamie@example.com",
        "subject": subject,
        "body": body,
        "received_at": "2026-04-21T14:30:00Z",
    }


def _facts(*items) -> list[dict]:
    # items are (id, content, recipient_knows) tuples
    return [
        {"id": i, "content": c, "recipient_knows": k}
        for i, c, k in items
    ]


# ─── build_redundancy_check_prompt ───────────────────────────────────


def test_prompt_includes_inbound_subject_and_body():
    prompt = mod.build_redundancy_check_prompt(
        draft="Thanks for the note.",
        inbound=_inbound(body="Hope all is well with you and the family."),
        known_facts=[],
        strategy="Warm brief acknowledgement.",
        recipient_model="Jamie wants to feel heard.",
    )
    assert "Hope all is well" in prompt
    assert "Re: catching up" in prompt


def test_prompt_surfaces_known_facts_only_when_recipient_knows():
    # The pass only considers facts flagged recipient_knows=True; the
    # mention-only CC case is explicitly out of scope per the Phase 5c
    # discussion.
    facts = _facts(
        ("f-known", "Jamie is exploring new roles", True),
        ("f-unknown", "the operator visited SF last week", False),
    )
    prompt = mod.build_redundancy_check_prompt(
        draft="...", inbound=_inbound(),
        known_facts=facts, strategy="", recipient_model="",
    )
    assert "Jamie is exploring new roles" in prompt
    assert "the operator visited SF last week" not in prompt


def test_prompt_includes_strategy_and_recipient_model():
    prompt = mod.build_redundancy_check_prompt(
        draft="...", inbound=_inbound(),
        known_facts=[],
        strategy="Reinforce that her exploration is seen and appreciated.",
        recipient_model="She wants emotional acknowledgment, not answers.",
    )
    assert "Reinforce that her exploration" in prompt
    assert "emotional acknowledgment" in prompt


def test_prompt_instructs_output_schema_with_three_fields():
    prompt = mod.build_redundancy_check_prompt(
        draft="a", inbound=_inbound(), known_facts=[],
        strategy="", recipient_model="",
    )
    for key in ("pruned_draft", "removed", "preserved_as_mirror"):
        assert key in prompt


def test_prompt_explicitly_names_intentional_mirror_exception():
    prompt = mod.build_redundancy_check_prompt(
        draft="a", inbound=_inbound(), known_facts=[],
        strategy="", recipient_model="",
    )
    low = prompt.lower()
    assert "intentional" in low or "mirror" in low


# ─── parse_redundancy_result ─────────────────────────────────────────


def test_parse_valid_response():
    raw = json.dumps({
        "pruned_draft": "Thanks. Thursday 2 PT works.",
        "removed": [
            {"sentence": "Regarding the Head of Product role at Anthropic,",
             "source": "inbound",
             "reason": "role name is verbatim from sender's message"},
        ],
        "preserved_as_mirror": [
            {"sentence": "Thanks.",
             "why": "acknowledgment, matches recipient_model emotional outcome"},
        ],
    })
    result = mod.parse_redundancy_result(raw)
    assert "error" not in result
    assert result["pruned_draft"].startswith("Thanks.")
    assert len(result["removed"]) == 1
    assert result["removed"][0]["source"] == "inbound"


def test_parse_strips_markdown_fences():
    raw = (
        "```json\n"
        + json.dumps({"pruned_draft": "x", "removed": [], "preserved_as_mirror": []})
        + "\n```"
    )
    result = mod.parse_redundancy_result(raw)
    assert result["pruned_draft"] == "x"


def test_parse_malformed_returns_error():
    result = mod.parse_redundancy_result("not json at all")
    assert "error" in result


def test_parse_missing_pruned_draft_returns_error():
    raw = json.dumps({"removed": [], "preserved_as_mirror": []})
    result = mod.parse_redundancy_result(raw)
    assert "error" in result


# ─── apply_redundancy_check ──────────────────────────────────────────


def _parsed(draft: str, reply_needed: bool = True,
            strategy: str = "", recipient_model: str = "") -> dict:
    return {
        "reply_needed": reply_needed,
        "draft_text": draft,
        "strategy": strategy or "Respond warmly.",
        "recipient_model": recipient_model or "She wants to feel heard.",
        "objective": "x",
        "current_state_and_gap": "x",
        "leverage": "x",
    }


def test_apply_skips_when_reply_not_needed():
    parsed = _parsed("", reply_needed=False)
    fake_calls = []

    def fake_infer(prompt, **kw):
        fake_calls.append(prompt)
        return _ok({"pruned_draft": "", "removed": [], "preserved_as_mirror": []})

    out = mod.apply_redundancy_check(
        parsed, inbound=_inbound(), known_facts=[],
        infer_fn=fake_infer,
    )
    assert fake_calls == []  # LLM NOT called
    assert out["draft_text"] == ""


def test_apply_skips_when_draft_too_short():
    # Single-sentence drafts don't have meaningful prune surface area
    parsed = _parsed("Yes!")

    def fake_infer(prompt, **kw):
        raise AssertionError("should not call LLM on 1-sentence draft")

    out = mod.apply_redundancy_check(
        parsed, inbound=_inbound(), known_facts=[],
        infer_fn=fake_infer,
    )
    assert out["draft_text"] == "Yes!"
    assert out.get("redundancy_status") == "skipped_short"


def test_apply_runs_second_pass_on_multi_sentence_draft():
    draft = (
        "Regarding the Head of Product role at Anthropic, thanks for "
        "reaching out. Thursday 2 PT works for me. I can send my LinkedIn "
        "and a deck ahead."
    )
    pruned = "Thursday 2 PT works for me. I can send my LinkedIn and a deck ahead."

    def fake_infer(prompt, **kw):
        return _ok({
            "pruned_draft": pruned,
            "removed": [{
                "sentence": "Regarding the Head of Product role at Anthropic, thanks for reaching out.",
                "source": "inbound",
                "reason": "role name + thanks both echo the inbound verbatim",
            }],
            "preserved_as_mirror": [],
        })

    out = mod.apply_redundancy_check(
        _parsed(draft), inbound=_inbound(
            body="We'd love to discuss our Head of Product role at Anthropic.",
        ),
        known_facts=[],
        infer_fn=fake_infer,
    )
    assert out["draft_text"] == pruned
    assert out["redundancy_status"] == "pruned"
    assert out["redundancy"]["removed_count"] == 1


def test_apply_degrades_open_on_llm_error():
    draft = "A. B. C."

    def fake_infer(prompt, **kw):
        return _err()

    out = mod.apply_redundancy_check(
        _parsed(draft), inbound=_inbound(), known_facts=[],
        infer_fn=fake_infer,
    )
    assert out["draft_text"] == draft  # original preserved
    assert out["redundancy_status"] == "llm_error"


def test_apply_degrades_open_on_malformed_json():
    draft = "A. B. C."

    def fake_infer(prompt, **kw):
        return SimpleNamespace(ok=True, text="not json",
                               error=None, input_tokens=0,
                               output_tokens=0, model="fake")

    out = mod.apply_redundancy_check(
        _parsed(draft), inbound=_inbound(), known_facts=[],
        infer_fn=fake_infer,
    )
    assert out["draft_text"] == draft
    assert out["redundancy_status"] == "parse_error"


def test_apply_rejects_over_aggressive_prune():
    # If the pruner shrinks the draft to under 30% of original length,
    # treat it as a bad response and keep the original. Guards against
    # the LLM collapsing everything to a two-word acknowledgement.
    draft = (
        "Thanks for reaching out. I'd love to discuss the role. "
        "Thursday 2 PT works well for me. I'll send context ahead of "
        "the call. Talk soon."
    )

    def fake_infer(prompt, **kw):
        return _ok({
            "pruned_draft": "Thanks.",  # way too short
            "removed": [{"sentence": "everything", "source": "inbound", "reason": "x"}],
            "preserved_as_mirror": [],
        })

    out = mod.apply_redundancy_check(
        _parsed(draft), inbound=_inbound(), known_facts=[],
        infer_fn=fake_infer,
    )
    assert out["draft_text"] == draft  # rollback
    assert out["redundancy_status"] == "rejected_over_pruned"


def test_apply_accepts_noop_prune_when_nothing_redundant():
    draft = (
        "Thursday 2 PT works for me. I can send my LinkedIn ahead. "
        "Looking forward to the conversation."
    )

    def fake_infer(prompt, **kw):
        return _ok({
            "pruned_draft": draft,
            "removed": [],
            "preserved_as_mirror": [],
        })

    out = mod.apply_redundancy_check(
        _parsed(draft), inbound=_inbound(), known_facts=[],
        infer_fn=fake_infer,
    )
    assert out["draft_text"] == draft
    assert out["redundancy_status"] == "noop"


def test_apply_records_preserved_mirrors():
    draft = (
        "Glad to hear the exploration is going well. Thursday 2 PT "
        "works for me. I'll send context ahead."
    )

    def fake_infer(prompt, **kw):
        return _ok({
            "pruned_draft": draft,  # no removals but one explicit mirror
            "removed": [],
            "preserved_as_mirror": [{
                "sentence": "Glad to hear the news.",
                "why": "emotional acknowledgment — strategy calls for warmth",
            }],
        })

    out = mod.apply_redundancy_check(
        _parsed(draft, strategy="Reinforce the warmth."),
        inbound=_inbound(body="Just wanted to share that the exploration is going well."),
        known_facts=[],
        infer_fn=fake_infer,
    )
    assert out["redundancy"]["preserved_count"] == 1
    assert out["redundancy_status"] == "noop"  # no removals → no-op
