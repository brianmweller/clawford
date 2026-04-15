"""Unit tests for agents/shared/llm.py — direct-HTTP LLM broker.

The shim makes direct HTTPS calls to the ChatGPT-subscription-backed
Responses endpoint at https://chatgpt.com/backend-api/codex/responses,
using OAuth credentials from ~/.codex/auth.json (shared with the codex
CLI). It parses SSE responses and extracts both text and usage metadata.

Tests stub urllib.request.urlopen so no real network calls happen.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload_llm():
    """Force-reimport the llm module so env changes take effect."""
    for mod in list(sys.modules):
        if mod == "llm" or mod.startswith("llm."):
            del sys.modules[mod]
    import llm  # type: ignore
    return llm


# ---------------------------------------------------------------------------
# Fake HTTP response — mimics urllib.response.addinfourl for both SSE
# iteration (codex/responses) and JSON body reads (oauth/token refresh).
# ---------------------------------------------------------------------------


class FakeHTTPResponse:
    def __init__(self, body_bytes: bytes, *, status: int = 200, headers: dict | None = None):
        self._body = body_bytes
        self._stream = io.BytesIO(body_bytes)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._stream.close()

    def __iter__(self):
        return iter(self._stream.readlines())

    def read(self, *a):
        return self._body


def _sse_body(events: Iterable[dict]) -> bytes:
    """Serialize a list of SSE event dicts into wire bytes.

    Each event becomes:  data: <json>\\n\\n
    """
    lines = []
    for ev in events:
        lines.append(f"data: {json.dumps(ev)}")
        lines.append("")  # blank line between events
    return ("\n".join(lines) + "\n").encode("utf-8")


def _default_responses_events(text: str = "pong", model: str = "gpt-5.4",
                               input_tokens: int = 26, output_tokens: int = 5) -> list[dict]:
    return [
        {"type": "response.created"},
        {"type": "response.output_text.done", "text": text},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_fake",
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            },
        },
    ]


def _make_fake_auth_file(tmp_path: Path, *,
                         access_token: str = "test_access_abc",
                         refresh_token: str = "test_refresh_xyz",
                         account_id: str = "acct-123") -> Path:
    auth = {
        "auth_mode": "ChatGPT",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": "test_id_token",
            "account_id": account_id,
        },
        "last_refresh": "2026-04-14T10:00:00Z",
    }
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(auth))
    return path


@pytest.fixture
def fake_auth(tmp_path, monkeypatch):
    """Every test gets its own auth.json pointed at by CLAWFORD_CODEX_AUTH_PATH."""
    path = _make_fake_auth_file(tmp_path)
    monkeypatch.setenv("CLAWFORD_CODEX_AUTH_PATH", str(path))
    return path


def _make_urlopen_stub(
    responses_factory: Callable[[], FakeHTTPResponse] | None = None,
    *,
    sequence: list[Any] | None = None,
) -> tuple[Callable, dict]:
    """Build a urlopen stub.

    Either give a single `responses_factory` (returns a FakeHTTPResponse
    for every call), or a `sequence` (list of entries consumed in order;
    each entry can be a FakeHTTPResponse, a callable returning one, or an
    exception to raise).

    Returns (stub, captured) where captured tracks the requests made.
    """
    captured: dict = {"count": 0, "requests": [], "timeouts": []}

    def stub(req, timeout=None):
        captured["count"] += 1
        captured["requests"].append(req)
        captured["timeouts"].append(timeout)
        if sequence is not None:
            entry = sequence[captured["count"] - 1]
            if isinstance(entry, Exception):
                raise entry
            if callable(entry):
                return entry()
            return entry
        assert responses_factory is not None
        return responses_factory()

    return stub, captured


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


def test_infer_returns_inferresult_on_success(fake_auth, monkeypatch):
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events("pong")))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("ping")
    assert result.ok
    assert result.text == "pong"
    assert result.model == "gpt-5.4"
    assert result.provider == "openai-codex"
    assert captured["count"] == 1


def test_infer_extracts_usage_from_completed_event(fake_auth, monkeypatch):
    llm = _reload_llm()
    stub, _ = _make_urlopen_stub(
        lambda: FakeHTTPResponse(
            _sse_body(_default_responses_events("hi", input_tokens=42, output_tokens=7))
        )
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("hello")
    assert result.input_tokens == 42
    assert result.output_tokens == 7
    assert result.total_tokens == 49


# ---------------------------------------------------------------------------
# Tests — request construction
# ---------------------------------------------------------------------------


def test_infer_posts_to_codex_responses_endpoint(fake_auth, monkeypatch):
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events()))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    llm.infer("ping")

    req = captured["requests"][0]
    assert req.full_url == "https://chatgpt.com/backend-api/codex/responses"
    assert req.get_method() == "POST"


def test_infer_sends_authorization_and_account_headers(fake_auth, monkeypatch):
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events()))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    llm.infer("ping")

    req = captured["requests"][0]
    # urllib normalizes header keys to capitalized form
    assert req.get_header("Authorization") == "Bearer test_access_abc"
    assert req.get_header("Chatgpt-account-id") == "acct-123"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Accept") == "text/event-stream"


def test_infer_sends_clawford_attribution_headers(fake_auth, monkeypatch):
    """Polite attribution: we identify as clawford, not as openclaw or codex."""
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events()))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    llm.infer("ping")

    req = captured["requests"][0]
    ua = req.get_header("User-agent") or ""
    originator = req.get_header("Originator") or ""
    assert "clawford" in ua.lower()
    assert originator == "clawford"


def test_infer_body_has_required_shape(fake_auth, monkeypatch):
    """Body must be {model, instructions, input, store:false, stream:true}.
    Missing any of those fields produces 400 from the real endpoint."""
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events()))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    llm.infer("what's the weather?", model="gpt-5.4", instructions="Be terse.")

    req = captured["requests"][0]
    body = json.loads(req.data)
    assert body["model"] == "gpt-5.4"
    assert body["instructions"] == "Be terse."
    assert body["store"] is False
    assert body["stream"] is True
    # input is a list of {role, content} messages
    assert isinstance(body["input"], list)
    assert body["input"][0]["role"] == "user"
    assert body["input"][0]["content"] == "what's the weather?"


def test_infer_json_mode_adds_text_format(fake_auth, monkeypatch):
    """json_mode=True → body has text.format.type = json_object."""
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events('{"x": 1}')))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    llm.infer("give json", json_mode=True)

    req = captured["requests"][0]
    body = json.loads(req.data)
    assert body.get("text", {}).get("format", {}).get("type") == "json_object"


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------


def test_infer_http_400_returns_not_ok(fake_auth, monkeypatch):
    llm = _reload_llm()
    err = urllib.error.HTTPError(
        url="https://chatgpt.com/backend-api/codex/responses",
        code=400,
        msg="Bad Request",
        hdrs={},
        fp=io.BytesIO(b'{"detail":"Stream must be set to true"}'),
    )
    stub, _ = _make_urlopen_stub(sequence=[err])
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("ping")
    assert not result.ok
    assert result.returncode == 400
    assert "400" in (result.error or "")


def test_infer_network_error_returns_not_ok(fake_auth, monkeypatch):
    llm = _reload_llm()
    stub, _ = _make_urlopen_stub(
        sequence=[urllib.error.URLError("connection refused")]
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("ping")
    assert not result.ok
    assert result.error is not None


def test_infer_missing_auth_file_returns_not_ok(tmp_path, monkeypatch):
    """If CLAWFORD_CODEX_AUTH_PATH points at a nonexistent file, the
    shim must return a clean error — not crash."""
    monkeypatch.setenv("CLAWFORD_CODEX_AUTH_PATH", str(tmp_path / "nope.json"))
    llm = _reload_llm()

    result = llm.infer("ping")
    assert not result.ok
    assert result.error is not None
    assert "auth" in result.error.lower() or "nope.json" in result.error


# ---------------------------------------------------------------------------
# Tests — 401 refresh flow
# ---------------------------------------------------------------------------


def test_infer_401_refreshes_and_retries_successfully(fake_auth, monkeypatch):
    """On 401 from codex/responses, the shim posts to oauth/token with the
    refresh_token, updates auth.json, and retries the original call."""
    llm = _reload_llm()

    err_401 = urllib.error.HTTPError(
        url="https://chatgpt.com/backend-api/codex/responses",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"error": "expired_token"}'),
    )
    refresh_response = FakeHTTPResponse(
        json.dumps(
            {
                "access_token": "new_access_token_xyz",
                "id_token": "new_id_token",
                "refresh_token": "new_refresh_token",
            }
        ).encode()
    )
    success_response = FakeHTTPResponse(_sse_body(_default_responses_events("pong-retry")))

    stub, captured = _make_urlopen_stub(sequence=[err_401, refresh_response, success_response])
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("ping")
    assert result.ok
    assert result.text == "pong-retry"

    # Three calls: initial (401) → refresh → retry (success)
    assert captured["count"] == 3
    # Second call targets the oauth endpoint
    assert captured["requests"][1].full_url == "https://auth.openai.com/oauth/token"
    # Third call targets responses again, with the NEW access token
    assert captured["requests"][2].get_header("Authorization") == "Bearer new_access_token_xyz"


def test_infer_refresh_persists_new_tokens_to_disk(fake_auth, monkeypatch):
    """Refreshed tokens get written back to auth.json so subsequent
    invocations use them without another refresh."""
    llm = _reload_llm()

    err_401 = urllib.error.HTTPError(
        url="https://chatgpt.com/backend-api/codex/responses",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"error": "expired_token"}'),
    )
    refresh_response = FakeHTTPResponse(
        json.dumps(
            {
                "access_token": "new_access",
                "id_token": "new_id",
                "refresh_token": "new_refresh",
            }
        ).encode()
    )
    success_response = FakeHTTPResponse(_sse_body(_default_responses_events()))

    stub, _ = _make_urlopen_stub(sequence=[err_401, refresh_response, success_response])
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    llm.infer("ping")

    # Re-read auth.json from disk and confirm new tokens are there
    persisted = json.loads(fake_auth.read_text())
    assert persisted["tokens"]["access_token"] == "new_access"
    assert persisted["tokens"]["refresh_token"] == "new_refresh"
    assert persisted["tokens"]["id_token"] == "new_id"


def test_infer_refresh_request_uses_correct_client_id_and_grant(fake_auth, monkeypatch):
    """The refresh POST body must have client_id, grant_type=refresh_token,
    and the current refresh_token — matching the codex Rust reference
    implementation (codex-rs/login/src/auth/manager.rs)."""
    llm = _reload_llm()

    err_401 = urllib.error.HTTPError(
        url="https://chatgpt.com/backend-api/codex/responses",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b"{}"),
    )
    refresh_response = FakeHTTPResponse(
        json.dumps(
            {"access_token": "new", "id_token": "new", "refresh_token": "new"}
        ).encode()
    )
    success_response = FakeHTTPResponse(_sse_body(_default_responses_events()))

    stub, captured = _make_urlopen_stub(sequence=[err_401, refresh_response, success_response])
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    llm.infer("ping")

    refresh_req = captured["requests"][1]
    body = json.loads(refresh_req.data)
    assert body["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "test_refresh_xyz"


def test_infer_refresh_failure_returns_error(fake_auth, monkeypatch):
    """If the refresh call itself fails, the shim returns a not-ok result
    with an informative error — it does NOT retry indefinitely."""
    llm = _reload_llm()

    err_401 = urllib.error.HTTPError(
        url="https://chatgpt.com/backend-api/codex/responses",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b"{}"),
    )
    refresh_err = urllib.error.HTTPError(
        url="https://auth.openai.com/oauth/token",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"error": "invalid_grant"}'),
    )

    stub, _ = _make_urlopen_stub(sequence=[err_401, refresh_err])
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("ping")
    assert not result.ok
    assert "refresh" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Tests — tool use (native Responses function calling)
# ---------------------------------------------------------------------------


def _function_call_events(call_id: str = "fc_123", name: str = "get_weather",
                          arguments: str = '{"location":"SF"}',
                          model: str = "gpt-5.4") -> list[dict]:
    """SSE events that emit a single tool call and no text."""
    return [
        {"type": "response.created"},
        {"type": "response.in_progress"},
        {"type": "response.output_item.added"},
        {"type": "response.function_call_arguments.delta",
         "delta": arguments[:5], "item_id": call_id, "output_index": 0},
        {"type": "response.function_call_arguments.delta",
         "delta": arguments[5:], "item_id": call_id, "output_index": 0},
        {"type": "response.function_call_arguments.done",
         "arguments": arguments, "item_id": call_id, "output_index": 0},
        {"type": "response.output_item.done",
         "item": {
             "type": "function_call",
             "call_id": call_id,
             "name": name,
             "arguments": arguments,
             "id": call_id,
         }},
        {"type": "response.completed",
         "response": {
             "id": "resp_fake_tool",
             "model": model,
             "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40},
         }},
    ]


def test_infer_accepts_tools_param_and_sends_in_body(fake_auth, monkeypatch):
    """When tools=[...] is passed, the request body must include the tools field."""
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events("pong")))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    tools = [{
        "type": "function",
        "name": "get_weather",
        "description": "Get the current weather in a given location",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    }]
    llm.infer("what's the weather?", tools=tools)

    req = captured["requests"][0]
    body = json.loads(req.data)
    assert body["tools"] == tools


def test_infer_without_tools_omits_tools_field(fake_auth, monkeypatch):
    """Backwards compat: calls without tools must NOT have a tools field in body."""
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events("hello")))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    llm.infer("hello")

    body = json.loads(captured["requests"][0].data)
    assert "tools" not in body


def test_infer_parses_function_call_from_sse_stream(fake_auth, monkeypatch):
    """When the model emits a function_call, InferResult.function_call must
    contain {call_id, name, arguments (dict)} and text may be empty."""
    llm = _reload_llm()
    stub, _ = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(
            _function_call_events(call_id="fc_abc", name="get_fleet_health",
                                  arguments='{"verbose":true}')
        ))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("how's the fleet?", tools=[{"type": "function", "name": "get_fleet_health"}])

    assert result.ok
    assert result.function_call is not None
    assert result.function_call["call_id"] == "fc_abc"
    assert result.function_call["name"] == "get_fleet_health"
    assert result.function_call["arguments"] == {"verbose": True}


def test_infer_text_only_response_has_no_function_call(fake_auth, monkeypatch):
    """When the model replies with plain text (no tool call), function_call
    on the result must be None."""
    llm = _reload_llm()
    stub, _ = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events("just chatting")))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("hi", tools=[{"type": "function", "name": "x"}])

    assert result.ok
    assert result.text == "just chatting"
    assert result.function_call is None


def test_infer_accepts_input_items_list_for_multi_turn(fake_auth, monkeypatch):
    """The dispatcher needs to feed a list of input items (prior user turns,
    assistant replies, function_call, function_call_output) back to the
    model to continue a multi-turn conversation. infer() must accept this
    as `input_items` and pass it as `input` in the body, bypassing the
    simple prompt-wrapper."""
    llm = _reload_llm()
    stub, captured = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events("ok")))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    items = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hi back"},
        {"role": "user", "content": "and now?"},
    ]
    llm.infer(prompt=None, input_items=items)

    body = json.loads(captured["requests"][0].data)
    assert body["input"] == items


def test_infer_prompt_and_input_items_are_mutually_exclusive(fake_auth, monkeypatch):
    """Passing both prompt= and input_items= is a programmer error.
    infer() should return a not-ok result with a clear message rather
    than silently preferring one over the other."""
    llm = _reload_llm()
    stub, _ = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events()))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer(prompt="hi", input_items=[{"role": "user", "content": "hi"}])
    assert not result.ok
    assert "mutually exclusive" in (result.error or "").lower() or "both" in (result.error or "").lower()


def test_infer_result_captures_response_id(fake_auth, monkeypatch):
    """InferResult.response_id is populated from response.completed.response.id.
    The dispatcher uses this for debug-traceability (not for chaining — store
    must be False per codex backend)."""
    llm = _reload_llm()
    stub, _ = _make_urlopen_stub(
        lambda: FakeHTTPResponse(_sse_body(_default_responses_events("pong")))
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", stub)

    result = llm.infer("ping")
    assert result.response_id == "resp_fake"
