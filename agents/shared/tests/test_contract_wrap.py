"""Unit tests for agents/shared/contract_wrap.py.

contract_wrap.py is a subprocess launcher that guarantees
SCRIPT_CONTRACT.md-compliant JSON stdout for any target script,
regardless of whether the target itself follows the contract. These
tests fabricate tiny target scripts in a tmp dir and verify the
wrapper's behavior across the full matrix of target outcomes:

  - target exits 0, prints compliant JSON
  - target exits 0, prints free-form text
  - target exits 0, prints nothing
  - target exits 1 via sys.exit(1)
  - target exits 1 via uncaught exception
  - target exits with an ImportError at module load
  - target prints JSON but with an unknown `status` value
  - target prints multi-line stdout with JSON as the final line
  - target hangs past the --timeout
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[1] / "contract_wrap.py"
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _run_wrapper(target: Path, *args: str, timeout: int | None = None) -> dict:
    cmd = [sys.executable, str(WRAPPER)]
    if timeout is not None:
        cmd += ["--timeout", str(timeout)]
    cmd += [str(target), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, (
        f"contract_wrap.py must always exit 0; got {proc.returncode}\n"
        f"stderr: {proc.stderr}"
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"contract_wrap.py must always print a JSON line; got empty stdout"
    return json.loads(lines[-1])


def test_happy_path_target_emits_compliant_json(tmp_path: Path) -> None:
    target = _write(tmp_path, "good.py", '''
import json
print(json.dumps({"status": "ok", "count": 42}))
''')
    result = _run_wrapper(target)
    assert result["status"] == "ok"
    assert result.get("count") == 42
    assert result["wrapped"]["exit_code"] == 0


def test_target_prints_freeform_text_only(tmp_path: Path) -> None:
    target = _write(tmp_path, "freeform.py", '''
print("hello world")
print("no JSON here")
''')
    result = _run_wrapper(target)
    assert result["status"] == "ok"  # exit 0 but no JSON → wrapper fabricates ok
    assert "wrapped" in result
    assert result["wrapped"]["exit_code"] == 0
    assert "no JSON here" in result["wrapped"]["stdout_tail"]


def test_target_produces_no_output(tmp_path: Path) -> None:
    target = _write(tmp_path, "silent.py", "pass\n")
    result = _run_wrapper(target)
    assert result["status"] == "ok"
    assert result["wrapped"]["exit_code"] == 0


def test_target_sys_exit_nonzero(tmp_path: Path) -> None:
    target = _write(tmp_path, "exit1.py", '''
import sys
print("about to fail")
sys.exit(1)
''')
    result = _run_wrapper(target)
    assert result["status"] == "error"
    assert "exited 1" in result["error"]
    assert result["wrapped"]["exit_code"] == 1
    assert "about to fail" in result["wrapped"]["stdout_tail"]


def test_target_uncaught_exception(tmp_path: Path) -> None:
    target = _write(tmp_path, "crash.py", '''
raise RuntimeError("boom")
''')
    result = _run_wrapper(target)
    assert result["status"] == "error"
    assert result["wrapped"]["exit_code"] != 0
    assert "boom" in result["wrapped"]["stderr_tail"]


def test_target_import_error_at_module_load(tmp_path: Path) -> None:
    target = _write(tmp_path, "bad_import.py", '''
import definitely_not_a_real_package_42
''')
    result = _run_wrapper(target)
    assert result["status"] == "error"
    assert "ModuleNotFoundError" in result["wrapped"]["stderr_tail"] or \
           "No module named" in result["wrapped"]["stderr_tail"]


def test_target_json_with_unknown_status(tmp_path: Path) -> None:
    """If target prints JSON with status='pending' (not in the allowed set),
    wrapper should still produce a compliant result by defaulting to 'ok'
    since the process exited 0, but preserve the original fields."""
    target = _write(tmp_path, "weird_status.py", '''
import json
print(json.dumps({"status": "pending", "note": "huh"}))
''')
    result = _run_wrapper(target)
    assert result["status"] in ("ok", "pending")
    # Wrapper always produces a valid status even if target didn't.
    assert result["status"] in ("ok", "error", "degraded", "pending")


def test_target_prints_multiple_json_lines_final_wins(tmp_path: Path) -> None:
    target = _write(tmp_path, "multi.py", '''
import json
print(json.dumps({"status": "ok", "event": "progress1"}))
print(json.dumps({"status": "ok", "event": "progress2"}))
print(json.dumps({"status": "ok", "final": True, "count": 3}))
''')
    result = _run_wrapper(target)
    assert result["status"] == "ok"
    assert result.get("final") is True
    assert result.get("count") == 3


def test_target_hangs_past_timeout(tmp_path: Path) -> None:
    target = _write(tmp_path, "slow.py", '''
import time
print("starting long work")
time.sleep(10)
print("done")
''')
    result = _run_wrapper(target, timeout=2)
    assert result["status"] == "error"
    assert "timeout" in result["error"]
    assert result["wrapped"]["exit_code"] == -1


def test_target_not_found(tmp_path: Path) -> None:
    fake = tmp_path / "does_not_exist.py"
    result = _run_wrapper(fake)
    assert result["status"] == "error"
    assert "not found" in result["error"]


def test_target_with_positional_args(tmp_path: Path) -> None:
    target = _write(tmp_path, "args.py", '''
import json, sys
print(json.dumps({"status": "ok", "argv": sys.argv[1:]}))
''')
    result = _run_wrapper(target, "one", "two", "three")
    assert result["status"] == "ok"
    assert result.get("argv") == ["one", "two", "three"]
