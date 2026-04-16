"""Tests for agents/shared/subprocess_helpers.py — the shared
run_json_script helper that replaces 12 byte-near-identical
_run_script definitions across the agent scripts.

Design invariants:
- Success: return the parsed JSON (dict or list).
- Any failure mode (timeout, spawn error, non-zero exit, empty stdout,
  malformed JSON) returns {'__error__': '<reason>'}.
- is_subprocess_error(result) predicate lets callers detect the
  sentinel without poking at internal keys.
- Callers that want the OLD behavior ("treat failure as empty result")
  can still do `if is_subprocess_error(r): r = []` explicitly — the
  helper doesn't force them to propagate. But the DEFAULT is to
  surface the failure so callers can't silently ignore.

2026-04-15 context: the family-calendar alert trio (gmail-invite,
activity-email, whatsapp-chat) all had local _run_script that
returned None on failure, which callers collapsed to empty list and
reported status=ok. The Gmail OAuth revocation went undetected for
2.5 days because the alert scripts never saw the auth error. This
helper is the root fix.
"""
from __future__ import annotations

import json
import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import subprocess_helpers  # type: ignore


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def test_success_returns_parsed_dict():
    fake = _fake_completed(stdout='{"status": "ok", "count": 3}')
    with patch("subprocess_helpers.subprocess.run", return_value=fake):
        result = subprocess_helpers.run_json_script("foo.py")
    assert result == {"status": "ok", "count": 3}
    assert not subprocess_helpers.is_subprocess_error(result)


def test_success_returns_parsed_list():
    fake = _fake_completed(stdout='[{"id": 1}, {"id": 2}]')
    with patch("subprocess_helpers.subprocess.run", return_value=fake):
        result = subprocess_helpers.run_json_script("foo.py")
    assert result == [{"id": 1}, {"id": 2}]
    assert not subprocess_helpers.is_subprocess_error(result)


def test_nonzero_exit_returns_error_sentinel():
    fake = _fake_completed(returncode=1, stderr="boom\ntraceback line")
    with patch("subprocess_helpers.subprocess.run", return_value=fake):
        result = subprocess_helpers.run_json_script("foo.py")
    assert subprocess_helpers.is_subprocess_error(result)
    assert "exit 1" in result["__error__"]
    assert "traceback" in result["__error__"] or "boom" in result["__error__"]


def test_empty_stdout_returns_error_sentinel():
    fake = _fake_completed(stdout="", returncode=0)
    with patch("subprocess_helpers.subprocess.run", return_value=fake):
        result = subprocess_helpers.run_json_script("foo.py")
    assert subprocess_helpers.is_subprocess_error(result)
    assert "empty" in result["__error__"].lower()


def test_malformed_json_returns_error_sentinel():
    fake = _fake_completed(stdout="not json", returncode=0)
    with patch("subprocess_helpers.subprocess.run", return_value=fake):
        result = subprocess_helpers.run_json_script("foo.py")
    assert subprocess_helpers.is_subprocess_error(result)
    assert "not json" in result["__error__"].lower()


def test_last_line_fallback_parses_trailing_json():
    """Scripts sometimes print debug lines to stdout before the final
    JSON line. Helper tries parsing the whole stdout first, then falls
    back to the last non-empty line."""
    fake = _fake_completed(stdout='debug line 1\ndebug line 2\n{"status": "ok"}')
    with patch("subprocess_helpers.subprocess.run", return_value=fake):
        result = subprocess_helpers.run_json_script("foo.py")
    assert result == {"status": "ok"}


def test_timeout_returns_error_sentinel():
    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="foo.py", timeout=30)
    with patch("subprocess_helpers.subprocess.run", side_effect=_raise):
        result = subprocess_helpers.run_json_script("foo.py", timeout=30)
    assert subprocess_helpers.is_subprocess_error(result)
    assert "timed out" in result["__error__"].lower()
    assert "30" in result["__error__"]


def test_file_not_found_returns_error_sentinel():
    def _raise(*a, **kw):
        raise FileNotFoundError(2, "No such file", "foo.py")
    with patch("subprocess_helpers.subprocess.run", side_effect=_raise):
        result = subprocess_helpers.run_json_script("foo.py")
    assert subprocess_helpers.is_subprocess_error(result)
    assert "spawn" in result["__error__"].lower() or "not found" in result["__error__"].lower()


def test_os_error_returns_error_sentinel():
    def _raise(*a, **kw):
        raise OSError(13, "Permission denied", "foo.py")
    with patch("subprocess_helpers.subprocess.run", side_effect=_raise):
        result = subprocess_helpers.run_json_script("foo.py")
    assert subprocess_helpers.is_subprocess_error(result)


def test_args_forwarded_to_subprocess_run():
    fake = _fake_completed(stdout='{"ok": true}')
    captured = {}
    def _capture(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return fake
    with patch("subprocess_helpers.subprocess.run", side_effect=_capture):
        subprocess_helpers.run_json_script("foo.py", "--query", "kirkland", timeout=45)
    assert "foo.py" in captured["cmd"][-3]
    assert "--query" in captured["cmd"]
    assert "kirkland" in captured["cmd"]
    assert captured["kwargs"]["timeout"] == 45


def test_is_subprocess_error_predicate_false_on_success():
    assert subprocess_helpers.is_subprocess_error([]) is False
    assert subprocess_helpers.is_subprocess_error({"status": "ok"}) is False
    assert subprocess_helpers.is_subprocess_error(None) is False
    assert subprocess_helpers.is_subprocess_error("text") is False


def test_is_subprocess_error_predicate_true_on_sentinel():
    assert subprocess_helpers.is_subprocess_error({"__error__": "x"}) is True


def test_error_message_on_success_dict_with_error_key():
    """A LEGITIMATE caller response like {'status': 'error', 'message': '...'}
    must NOT be confused with our __error__ sentinel. Only the
    __error__ key indicates a subprocess-level failure."""
    fake = _fake_completed(stdout='{"status": "error", "message": "upstream down"}')
    with patch("subprocess_helpers.subprocess.run", return_value=fake):
        result = subprocess_helpers.run_json_script("foo.py")
    assert not subprocess_helpers.is_subprocess_error(result)
    assert result["status"] == "error"
