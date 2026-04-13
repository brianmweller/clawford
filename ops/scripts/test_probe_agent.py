"""Tests for ops/scripts/probe-agent.py.

R6 wrapper that fleet-health.py invokes via docker exec. Imports a
heartbeat module dynamically and prints json.dumps(probe()) without
triggering run() / _write_status_md() side effects.

Run: cd ops/scripts && python3 -m pytest test_probe_agent.py -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent


def _load_probe_agent():
    path = SCRIPTS_DIR / "probe-agent.py"
    spec = importlib.util.spec_from_file_location("probe_agent", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["probe_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_heartbeat_script(tmp_path):
    """Write a minimal heartbeat.py with a probe() function."""
    script = tmp_path / "heartbeat.py"
    script.write_text(textwrap.dedent('''
        def probe():
            return {"status": "ok", "test_field": "hello"}

        def run():
            # Should NOT be called by probe-agent.py — if it were,
            # this side effect file would appear.
            from pathlib import Path
            Path(__file__).parent.joinpath("RUN_WAS_CALLED").write_text("oops")
            return probe()

        def main():
            run()
            return 0
    '''))
    return script


def test_probe_agent_imports_and_calls_probe(fake_heartbeat_script, capsys):
    mod = _load_probe_agent()
    with patch.object(sys, "argv", ["probe-agent.py", str(fake_heartbeat_script)]):
        rc = mod.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["test_field"] == "hello"


def test_probe_agent_does_not_call_run(fake_heartbeat_script, capsys):
    """The whole point of R6: probe-agent.py must NOT trigger
    run() or any side effects. The fake heartbeat's run() writes a
    sentinel file — verify it doesn't exist after invocation."""
    sentinel = fake_heartbeat_script.parent / "RUN_WAS_CALLED"
    mod = _load_probe_agent()
    with patch.object(sys, "argv", ["probe-agent.py", str(fake_heartbeat_script)]):
        mod.main()
    capsys.readouterr()
    assert not sentinel.exists(), "probe-agent.py must not call run() or main()"


def test_probe_agent_emits_error_json_on_missing_file(tmp_path, capsys):
    mod = _load_probe_agent()
    with patch.object(sys, "argv", ["probe-agent.py", str(tmp_path / "no-such-file.py")]):
        rc = mod.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "alert" in payload


def test_probe_agent_emits_error_when_no_probe_function(tmp_path, capsys):
    """A heartbeat.py without probe() exported violates the R2
    contract. probe-agent.py should report it cleanly."""
    bad = tmp_path / "no-probe.py"
    bad.write_text("def main(): return 0\n")
    mod = _load_probe_agent()
    with patch.object(sys, "argv", ["probe-agent.py", str(bad)]):
        rc = mod.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "no probe()" in payload.get("alert", "") or "probe" in payload.get("error", "")


def test_probe_agent_emits_usage_error_with_no_args(capsys):
    mod = _load_probe_agent()
    with patch.object(sys, "argv", ["probe-agent.py"]):
        rc = mod.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "usage" in payload.get("alert", "")
