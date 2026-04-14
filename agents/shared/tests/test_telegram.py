"""Unit tests for agents/shared/telegram.py — shared Telegram Bot API helpers.

Replaces ~168 lines of byte-identical logic duplicated across all five
per-agent timed-deliver.py scripts, plus news-digest's deliver-digest.py
custom send_telegram helper. This library exposes:

  - send_message(token, chat_id, text, ...)
  - send_chunked(token, chat_id, text, ...)  for >4000-char messages
  - wait_for_top_of_hour() — the gather-window hold pattern
  - resolve_credentials(token_env) — env var lookup
  - deliver_from_file(msg_file, ...) — end-to-end orchestration

Tests stub urllib.request.urlopen so no real API calls happen.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload_telegram():
    """Reload agents/shared/telegram_api.py fresh per test.

    Named telegram_api (not telegram) to avoid collision with the
    installed python-telegram-bot package in site-packages.
    """
    for mod in list(sys.modules):
        if mod == "telegram_api" or mod.startswith("telegram_api."):
            del sys.modules[mod]
    import telegram_api  # type: ignore
    return telegram_api


class FakeHTTPResponse:
    def __init__(self, body: bytes, *, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, *a):
        return self._body


def _make_urlopen_stub(
    *,
    factory: Callable[[], FakeHTTPResponse] | None = None,
    sequence: list[Any] | None = None,
) -> tuple[Callable, dict]:
    """Same shape as the llm test helper: return a urlopen stub plus a
    captured dict recording each call."""
    captured: dict = {"count": 0, "requests": [], "timeouts": []}

    def stub(req, timeout=None):
        captured["count"] += 1
        captured["requests"].append(req)
        captured["timeouts"].append(timeout)
        if sequence is not None:
            entry = sequence[captured["count"] - 1]
            if isinstance(entry, Exception):
                raise entry
            return entry() if callable(entry) else entry
        assert factory is not None
        return factory()

    return stub, captured


def _ok_response(**extras) -> FakeHTTPResponse:
    body = {"ok": True, "result": {"message_id": 42, **extras}}
    return FakeHTTPResponse(json.dumps(body).encode())


def _not_ok_response(error: str = "BAD_REQUEST") -> FakeHTTPResponse:
    return FakeHTTPResponse(json.dumps({"ok": False, "description": error}).encode())


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


def test_send_message_posts_to_correct_url(monkeypatch):
    telegram = _reload_telegram()
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)

    telegram.send_message("TOKEN_ABC", "chat123", "hello")

    req = captured["requests"][0]
    assert req.full_url == "https://api.telegram.org/botTOKEN_ABC/sendMessage"
    assert req.get_method() == "POST"


def test_send_message_body_has_expected_fields(monkeypatch):
    telegram = _reload_telegram()
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)

    telegram.send_message("TOKEN", "chat123", "hello world", silent=True)

    body = json.loads(captured["requests"][0].data)
    assert body["chat_id"] == "chat123"
    assert body["text"] == "hello world"
    assert body["disable_web_page_preview"] is True
    assert body["disable_notification"] is True


def test_send_message_silent_false_by_default(monkeypatch):
    telegram = _reload_telegram()
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)

    telegram.send_message("T", "c", "hi")

    body = json.loads(captured["requests"][0].data)
    assert body["disable_notification"] is False


def test_send_message_supports_reply_markup(monkeypatch):
    """deliver-digest.py passes inline_keyboard reply_markup for like/dislike
    buttons — the shared library must support this shape."""
    telegram = _reload_telegram()
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)

    buttons = {
        "inline_keyboard": [
            [
                {"text": "👍", "callback_data": "like:1"},
                {"text": "👎", "callback_data": "dislike:1"},
            ]
        ]
    }
    telegram.send_message("T", "c", "item 1", reply_markup=buttons)

    body = json.loads(captured["requests"][0].data)
    assert body["reply_markup"] == buttons


def test_send_message_omits_reply_markup_when_none(monkeypatch):
    telegram = _reload_telegram()
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)

    telegram.send_message("T", "c", "plain")

    body = json.loads(captured["requests"][0].data)
    assert "reply_markup" not in body


def test_send_message_returns_true_on_ok(monkeypatch):
    telegram = _reload_telegram()
    stub, _ = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)

    assert telegram.send_message("T", "c", "hi") is True


def test_send_message_returns_false_on_not_ok(monkeypatch):
    telegram = _reload_telegram()
    stub, _ = _make_urlopen_stub(factory=lambda: _not_ok_response("BAD_REQUEST"))
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)

    assert telegram.send_message("T", "c", "hi") is False


def test_send_message_returns_false_on_network_error(monkeypatch):
    telegram = _reload_telegram()
    stub, _ = _make_urlopen_stub(sequence=[urllib.error.URLError("refused")])
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)

    assert telegram.send_message("T", "c", "hi") is False


# ---------------------------------------------------------------------------
# send_chunked
# ---------------------------------------------------------------------------


def test_send_chunked_single_call_when_short(monkeypatch):
    telegram = _reload_telegram()
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)
    # Disable the inter-chunk sleep so tests are fast
    monkeypatch.setattr(telegram.time, "sleep", lambda *a: None)

    result = telegram.send_chunked("T", "c", "short message")
    assert result == 1
    assert captured["count"] == 1


def test_send_chunked_splits_long_message_on_line_boundaries(monkeypatch):
    telegram = _reload_telegram()
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)
    monkeypatch.setattr(telegram.time, "sleep", lambda *a: None)

    # Build a message > 4000 chars from multiple lines
    lines = [f"line {i}: " + ("x" * 100) for i in range(50)]
    long_text = "\n".join(lines)
    assert len(long_text) > 4000

    result = telegram.send_chunked("T", "c", long_text)
    assert result >= 2  # split into multiple chunks
    assert captured["count"] >= 2
    # Every chunk should fit under the max size
    for req in captured["requests"]:
        body = json.loads(req.data)
        assert len(body["text"]) <= 4000


def test_send_chunked_intermediate_chunks_always_silent(monkeypatch):
    """Only the final chunk respects the caller's silent preference.
    Intermediate chunks are forced silent to avoid a buzz per chunk."""
    telegram = _reload_telegram()
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)
    monkeypatch.setattr(telegram.time, "sleep", lambda *a: None)

    lines = [f"line {i}: " + ("x" * 100) for i in range(50)]
    long_text = "\n".join(lines)
    telegram.send_chunked("T", "c", long_text, silent=False)

    # All but the last chunk should be silent=True
    for i, req in enumerate(captured["requests"]):
        body = json.loads(req.data)
        is_last = (i == len(captured["requests"]) - 1)
        if is_last:
            assert body["disable_notification"] is False, "last chunk should honor silent=False"
        else:
            assert body["disable_notification"] is True, f"chunk {i} should be silent"


# ---------------------------------------------------------------------------
# wait_for_top_of_hour
# ---------------------------------------------------------------------------


def test_wait_for_top_of_hour_sleeps_in_gather_window(monkeypatch):
    telegram = _reload_telegram()
    slept_for: list[float] = []

    fake_now = datetime(2026, 4, 14, 10, 55, 30, tzinfo=timezone.utc)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    monkeypatch.setattr(telegram, "datetime", _FakeDatetime)
    monkeypatch.setattr(telegram.time, "sleep", lambda s: slept_for.append(s))

    telegram.wait_for_top_of_hour()

    # Should sleep for (60-55)*60 - 30 = 270 seconds
    assert len(slept_for) == 1
    assert slept_for[0] == 270


def test_wait_for_top_of_hour_no_sleep_outside_window(monkeypatch):
    telegram = _reload_telegram()
    slept_for: list[float] = []

    # :20 is outside both the gather window (:40+) and the overshoot warn (:<=10)
    fake_now = datetime(2026, 4, 14, 10, 20, 0, tzinfo=timezone.utc)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    monkeypatch.setattr(telegram, "datetime", _FakeDatetime)
    monkeypatch.setattr(telegram.time, "sleep", lambda s: slept_for.append(s))

    telegram.wait_for_top_of_hour()
    assert slept_for == []


# ---------------------------------------------------------------------------
# resolve_credentials
# ---------------------------------------------------------------------------


def test_resolve_credentials_reads_env(monkeypatch):
    telegram = _reload_telegram()
    monkeypatch.setenv("MY_BOT_TOKEN", "secret_token_123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat_abc")

    token, chat_id = telegram.resolve_credentials("MY_BOT_TOKEN")
    assert token == "secret_token_123"
    assert chat_id == "chat_abc"


def test_resolve_credentials_raises_on_missing_token(monkeypatch):
    telegram = _reload_telegram()
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat_abc")

    with pytest.raises(RuntimeError, match="MISSING_TOKEN"):
        telegram.resolve_credentials("MISSING_TOKEN")


def test_resolve_credentials_raises_on_missing_chat_id(monkeypatch):
    telegram = _reload_telegram()
    monkeypatch.setenv("MY_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        telegram.resolve_credentials("MY_BOT_TOKEN")


# ---------------------------------------------------------------------------
# deliver_from_file — end-to-end orchestration
# ---------------------------------------------------------------------------


def test_deliver_from_file_success(tmp_path, monkeypatch):
    telegram = _reload_telegram()
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("hello from file", encoding="utf-8")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")
    stub, captured = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)
    monkeypatch.setattr(telegram.time, "sleep", lambda *a: None)

    result = telegram.deliver_from_file(str(msg_file), wait_for_hour=False)

    assert result["status"] == "ok"
    assert result["chars"] == len("hello from file")
    assert result["chunks_sent"] == 1
    assert captured["count"] == 1


def test_deliver_from_file_raises_on_missing_file(tmp_path, monkeypatch):
    telegram = _reload_telegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")

    with pytest.raises(FileNotFoundError):
        telegram.deliver_from_file(str(tmp_path / "nope.txt"), wait_for_hour=False)


def test_deliver_from_file_raises_on_empty_file(tmp_path, monkeypatch):
    telegram = _reload_telegram()
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n\n", encoding="utf-8")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")

    with pytest.raises(ValueError, match="empty"):
        telegram.deliver_from_file(str(empty), wait_for_hour=False)


# ---------------------------------------------------------------------------
# parse_cli_argv + cli_main
# ---------------------------------------------------------------------------


def test_parse_cli_argv_minimal():
    telegram = _reload_telegram()
    msg_file, silent, token_env = telegram.parse_cli_argv(["/tmp/msg.txt"])
    assert msg_file == "/tmp/msg.txt"
    assert silent is False
    assert token_env == "TELEGRAM_BOT_TOKEN"


def test_parse_cli_argv_with_all_flags():
    telegram = _reload_telegram()
    msg_file, silent, token_env = telegram.parse_cli_argv(
        ["/tmp/msg.txt", "--silent", "--token-env", "SHOPPING_BOT_TOKEN"]
    )
    assert msg_file == "/tmp/msg.txt"
    assert silent is True
    assert token_env == "SHOPPING_BOT_TOKEN"


def test_parse_cli_argv_raises_on_missing_msg_file():
    telegram = _reload_telegram()
    with pytest.raises(ValueError, match="usage"):
        telegram.parse_cli_argv([])


def test_parse_cli_argv_raises_on_dangling_token_env():
    telegram = _reload_telegram()
    with pytest.raises(ValueError, match="--token-env"):
        telegram.parse_cli_argv(["/tmp/msg.txt", "--token-env"])


def test_cli_main_prints_script_contract_json_on_success(tmp_path, monkeypatch, capsys):
    telegram = _reload_telegram()
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("hello", encoding="utf-8")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")
    stub, _ = _make_urlopen_stub(factory=_ok_response)
    monkeypatch.setattr(telegram.urllib.request, "urlopen", stub)
    monkeypatch.setattr(telegram.time, "sleep", lambda *a: None)

    rc = telegram.cli_main([str(msg_file)])

    assert rc == 0  # exit 0 per script contract
    captured = capsys.readouterr()
    line = captured.out.strip()
    result = json.loads(line)
    assert result["status"] == "ok"
    assert result["chunks_sent"] == 1


def test_cli_main_wraps_errors_in_script_contract_json(tmp_path, monkeypatch, capsys):
    telegram = _reload_telegram()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    rc = telegram.cli_main([str(tmp_path / "nothing.txt")])

    assert rc == 0  # exit 0 even on error per script contract
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["status"] == "error"
    assert "TELEGRAM_BOT_TOKEN" in result["error"]
