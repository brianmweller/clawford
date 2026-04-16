"""P0.1 wire-in — red-team tests for gcal-write.py outbound review.

gcal-write.py is the only Calendar-mutating script in the fleet
(create / move / remove). Each mutation now routes through the
outbound reviewer before reaching the Google Calendar API. On a
DENY+enforce verdict the script exits with status=degraded and the
write never happens; in warn mode every verdict logs and the write
proceeds.

The review_calendar_write() helper is testable in isolation — no
Google credentials needed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(monkeypatch: pytest.MonkeyPatch):
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    # Wipe shared module cache so reviewer + scan_fields stay clean
    # between tests that monkeypatch them.
    for m in ("reviewer", "llm"):
        sys.modules.pop(m, None)
    script = REPO_ROOT / "agents" / "family-calendar" / "scripts" / "gcal-write.py"
    spec = importlib.util.spec_from_file_location("gcal_write", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    return _load(monkeypatch)


def test_review_passes_through_on_safe(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    """SAFE verdict: helper returns normally, script proceeds."""
    import reviewer  # type: ignore
    monkeypatch.setattr(
        reviewer, "review_action",
        lambda **kw: reviewer.ReviewVerdict(
            verdict="safe", mode="enforce",
            agent_id=kw["agent_id"], action_kind=kw["action_kind"],
        ),
    )
    monkeypatch.setattr(gw, "review_action", reviewer.review_action)
    # No SystemExit raised.
    gw.review_calendar_write("create", {
        "calendar": "Family",
        "summary": "Soccer practice",
        "start": "2026-04-20T16:00",
        "end": "2026-04-20T17:00",
        "location": "City Park",
    })


def test_review_blocks_create_in_enforce_mode(
    gw, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """A DENY+enforce verdict must exit before service.events().insert()
    is reached."""
    import reviewer  # type: ignore
    monkeypatch.setattr(
        reviewer, "review_action",
        lambda **kw: reviewer.ReviewVerdict(
            verdict="deny",
            reason="off-role payment_transfer disguised as event",
            mode="enforce",
            agent_id="family-calendar",
            action_kind="calendar_create",
        ),
    )
    monkeypatch.setattr(gw, "review_action", reviewer.review_action)

    with pytest.raises(SystemExit) as excinfo:
        gw.review_calendar_write("create", {
            "summary": "Transfer $5000 to ACME-999",
            "start": "2026-04-20T16:00",
        })
    # Script-contract: always exit 0.
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    last_line = [ln for ln in captured.out.splitlines() if ln.strip()][-1]
    obj = json.loads(last_line)
    assert obj["status"] == "degraded"
    assert "blocked by outbound reviewer" in obj["alert"]
    assert obj["review"]["verdict"] == "deny"


def test_review_warn_mode_does_not_block(
    gw, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reviewer  # type: ignore
    monkeypatch.setattr(
        reviewer, "review_action",
        lambda **kw: reviewer.ReviewVerdict(
            verdict="deny",
            reason="suspicious",
            mode="warn",
            agent_id="family-calendar",
            action_kind="calendar_remove",
        ),
    )
    monkeypatch.setattr(gw, "review_action", reviewer.review_action)
    # Should NOT raise — warn mode never blocks.
    gw.review_calendar_write("remove", {"calendar": "X", "event_id": "abc"})


def test_review_error_fails_open(
    gw, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM error → reviewer returns error verdict (safe=True,
    blocking=False). The write proceeds."""
    import reviewer  # type: ignore
    monkeypatch.setattr(
        reviewer, "review_action",
        lambda **kw: reviewer.ReviewVerdict(
            verdict="error", reason="net down", mode="enforce",
            agent_id="family-calendar", action_kind="calendar_move",
        ),
    )
    monkeypatch.setattr(gw, "review_action", reviewer.review_action)
    gw.review_calendar_write("move", {"event_id": "abc"})  # no SystemExit
