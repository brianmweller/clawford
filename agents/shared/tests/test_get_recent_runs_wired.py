"""Smoke-test: each agent's tools.py must expose `get_recent_runs`
via both its TOOLS list (for the LLM) and its EXECUTORS dict (for
the dispatcher). Without both, the tool is invisible or uncallable.

This is a fleet-wide contract; one missing wire breaks the agent's
ability to answer meta-questions about its own state, which was the
original bug that motivated the carve-out plan.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SHARED_DIR = REPO_ROOT / "agents" / "shared"
sys.path.insert(0, str(SHARED_DIR))


AGENT_IDS = [
    "connector",
    "family-calendar",
    "fix-it",
    "meetings-coach",
    "news-digest",
    "shopping",
]


def _load_tools(agent_id: str):
    path = REPO_ROOT / "agents" / agent_id / "tools.py"
    spec = importlib.util.spec_from_file_location(
        f"{agent_id}_tools_smoketest", path,
    )
    mod = importlib.util.module_from_spec(spec)
    # Some agent tools.py files self-bootstrap their directory onto
    # sys.path at module-load time (e.g. agents/fix-it/tools.py,
    # agents/family-calendar/tools.py) so the dispatcher's exec_module
    # flow resolves sibling imports. Production needs that side effect
    # to stick; this smoke loader does not, and leaking it poisons
    # bare `import tools` lookups in every downstream test suite.
    # Snapshot + restore isolates the mutation.
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
        for m in list(sys.modules):
            if m not in saved_modules:
                del sys.modules[m]
    return mod


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_get_recent_runs_in_tools_list(agent_id: str) -> None:
    mod = _load_tools(agent_id)
    names = [t.get("name") for t in getattr(mod, "TOOLS", [])]
    assert "get_recent_runs" in names, (
        f"{agent_id}/tools.py TOOLS list is missing get_recent_runs"
    )


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_get_recent_runs_in_executors(agent_id: str) -> None:
    mod = _load_tools(agent_id)
    executors = getattr(mod, "EXECUTORS", {})
    assert "get_recent_runs" in executors, (
        f"{agent_id}/tools.py EXECUTORS dict is missing get_recent_runs"
    )
    assert callable(executors["get_recent_runs"])


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_get_recent_runs_is_callable_and_returns_dict(
    agent_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point CLAWFORD_LOGS_DIR at an empty dir so the call succeeds
    with no logs, returning the empty-but-well-formed shape."""
    monkeypatch.setenv("CLAWFORD_LOGS_DIR", str(tmp_path))
    mod = _load_tools(agent_id)
    result = mod.EXECUTORS["get_recent_runs"]()
    assert isinstance(result, dict)
    assert result["agent_id"] == agent_id
    assert result["runs"] == []
    assert "counts" in result
