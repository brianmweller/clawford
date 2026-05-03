"""agents/shared/llm.py — Clawford LLM broker.

Lineage: the original liberation plan (Phase 1) called for a dispatch shim
with `_run_openclaw` / `_run_codex` backends selected via
CLAWFORD_LLM_BACKEND. That shim was never needed — by the time Phase 4
landed the fleet, every agent already called infer() directly and the
OpenClaw backend path was dead. Phase 6 (2026-04-15) confirmed the
codex-only shape and promoted it from "current default" to "only option."

Calls ChatGPT-subscription-backed OpenAI Responses API directly at
https://chatgpt.com/backend-api/codex/responses, using the OAuth
credentials stored in ~/.codex/auth.json (shared with the codex CLI).

This is the same wire protocol OpenClaw's openai-codex provider uses at
scale — we replicate it directly in Python, bypassing the codex binary
to avoid the ~8k-token-per-call agent framing the CLI prepends.

Per-call overhead dropped from ~8100 tokens (codex CLI agentic framing)
to ~25 tokens (just the model's own system-prompt tokens for the shim's
tiny instructions string + the user prompt). A ~260x reduction.

Contract: infer(prompt, ...) → InferResult. Always returned, never
raises. Check .ok to branch on success/failure.

Behavior:
- Loads auth from ~/.codex/auth.json (path overridable via
  CLAWFORD_CODEX_AUTH_PATH env var).
- POSTs to /backend-api/codex/responses with {model, instructions,
  input, store:false, stream:true} — those fields are all required
  by the endpoint; dropping any of them produces a 400.
- Parses SSE stream for the final `response.output_text.done` and
  `response.completed` events.
- On 401, calls https://auth.openai.com/oauth/token with the
  refresh_token to get a new access_token, persists the rotated
  tokens back to auth.json atomically, and retries the original call
  once.

Example:
    from llm import infer
    r = infer("summarize this: ...", json_mode=True, timeout=30)
    if not r.ok:
        print(f"llm failed: {r.error}")
        return None
    return json.loads(r.text)
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Endpoints and constants
# ---------------------------------------------------------------------------

CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"

# Public PKCE client id used by the codex CLI for the ChatGPT OAuth flow.
# Confirmed in codex-rs/login/src/auth/manager.rs:
#   pub const CLIENT_ID: &str = "app_EMoamEEZ73f0CkXaXp7hrann";
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

AUTH_PATH_ENV_VAR = "CLAWFORD_CODEX_AUTH_PATH"
DEFAULT_AUTH_PATH = "~/.codex/auth.json"

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_INSTRUCTIONS = "You are a terse, helpful assistant."
DEFAULT_TIMEOUT_S = 90

CLAWFORD_VERSION = "0.1.0"
CLAWFORD_USER_AGENT = f"clawford/{CLAWFORD_VERSION}"

# P0.3: Forensics. When scripts are invoked via contract_wrap.py, the
# wrapper sets CLAWFORD_TRACE_ID in the subprocess env so every LLM call
# inside the script tags its stderr log with the same id the envelope
# will carry. Callers can also pass trace_id= directly.
TRACE_ID_ENV_VAR = "CLAWFORD_TRACE_ID"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class InferResult:
    """Normalized result of an LLM inference call.

    On success: text/model populated, usage counts set, returncode==0,
    error is None, .ok is True. If the model chose to call a tool
    instead of replying with text, .function_call is populated with
    {call_id, name, arguments (dict)} and .text is empty — callers
    check .function_call first, execute the tool, and feed the result
    back via a second infer() call with input_items=[...].

    On failure: .ok is False, .error carries a human-readable reason,
    .text may be empty. Token counts may be zero or partial depending
    on where the failure occurred.
    """

    text: str = ""
    model: str = ""
    provider: str = "openai-codex"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    returncode: int = 0
    error: str | None = None
    function_call: dict | None = None
    response_id: str = ""
    trace_id: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def infer(
    prompt: str | None = None,
    *,
    instructions: str = DEFAULT_INSTRUCTIONS,
    model: str = DEFAULT_MODEL,
    json_mode: bool = False,
    timeout: int = DEFAULT_TIMEOUT_S,
    tools: list[dict] | None = None,
    input_items: list[dict] | None = None,
    trace_id: str | None = None,
) -> InferResult:
    """Run an LLM inference call through the ChatGPT-subscription codex
    responses endpoint.

    Args:
        prompt: Simple user message — builds `input=[{role, content}]`
            for you. Mutually exclusive with input_items.
        instructions: System-prompt-equivalent sent as the `instructions`
            field. Keep this small — every token costs.
        model: Model ID. Default "gpt-5.4". Pass "gpt-5.3-codex" if you
            hit model routing issues.
        json_mode: If True, sets text.format.type=json_object so the
            model returns valid JSON directly. The caller can then
            json.loads(result.text) without fence-stripping hacks.
        timeout: Socket timeout in seconds for the HTTPS call.
        tools: Optional list of tool definitions in the OpenAI Responses
            API shape ({type:function, name, description, parameters}).
            When present, the model may emit a function_call which lands
            in InferResult.function_call instead of .text.
        input_items: Multi-turn mode: pass a full input-items list
            (prior user turns, assistant replies, function_call items,
            function_call_output items). Mutually exclusive with prompt.
            Used by tool_use.py to chain calls.
        trace_id: Forensic tag propagated from contract_wrap.py via the
            CLAWFORD_TRACE_ID env var. If not passed, falls back to the
            env var; if neither is set, the call is untagged (empty
            string on the returned InferResult). Every call emits one
            stderr log line tagged with this id so the full chain of
            "which LLM call produced which action" can be reconstructed
            via grep.

    Returns:
        InferResult. Always returned, never raises — check .ok.
    """
    effective_trace_id = trace_id if trace_id is not None else os.environ.get(
        TRACE_ID_ENV_VAR, ""
    )

    result = _infer_impl(
        prompt=prompt,
        instructions=instructions,
        model=model,
        json_mode=json_mode,
        timeout=timeout,
        tools=tools,
        input_items=input_items,
    )
    result.trace_id = effective_trace_id
    _emit_forensic_log(result)
    return result


def _infer_impl(
    *,
    prompt: str | None,
    instructions: str,
    model: str,
    json_mode: bool,
    timeout: int,
    tools: list[dict] | None,
    input_items: list[dict] | None,
) -> InferResult:
    """Actual network + parsing. Pure function of its inputs; trace_id
    tagging and forensic logging live in the outer infer() wrapper."""
    if prompt is not None and input_items is not None:
        return InferResult(
            returncode=22,
            error="prompt and input_items are mutually exclusive",
        )
    if prompt is None and input_items is None:
        return InferResult(
            returncode=22,
            error="must pass either prompt or input_items",
        )

    auth_path = _auth_path()
    try:
        auth = _load_auth(auth_path)
    except FileNotFoundError:
        return InferResult(
            returncode=127,
            error=f"auth.json not found at {auth_path}",
        )
    except (json.JSONDecodeError, KeyError) as e:
        return InferResult(
            returncode=127,
            error=f"auth.json at {auth_path} is malformed: {e}",
        )

    body = _build_request_body(
        prompt=prompt,
        instructions=instructions,
        model=model,
        json_mode=json_mode,
        tools=tools,
        input_items=input_items,
    )

    try:
        response = _post_responses(auth, body, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Token expired. Refresh once and retry.
            try:
                auth = _refresh_access_token(auth, auth_path)
            except urllib.error.HTTPError as refresh_err:
                return InferResult(
                    returncode=401,
                    error=_format_refresh_error(refresh_err),
                )
            except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as refresh_err:
                return InferResult(
                    returncode=401,
                    error=f"refresh call failed: {refresh_err}",
                )
            # Retry the original call with the new access token.
            try:
                response = _post_responses(auth, body, timeout=timeout)
            except urllib.error.HTTPError as retry_err:
                return InferResult(
                    returncode=retry_err.code,
                    error=_format_http_error(retry_err),
                )
            except (urllib.error.URLError, TimeoutError, OSError) as retry_err:
                return InferResult(
                    returncode=500,
                    error=f"network error after refresh: {retry_err}",
                )
        else:
            return InferResult(
                returncode=e.code,
                error=_format_http_error(e),
            )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return InferResult(
            returncode=500,
            error=f"network error: {e}",
        )

    try:
        return _parse_sse_response(response)
    finally:
        try:
            response.close()
        except Exception:
            pass


def _emit_forensic_log(result: InferResult) -> None:
    """Print one structured line to stderr summarizing this LLM call.

    Format is parseable by simple grep / awk: `trace=<id>` always
    appears first so forensic reconstruction like
    `grep 'trace=abc-123' /var/log/clawford-*.log` pulls the whole
    chain of a single cron invocation (wrapper envelope + every LLM
    call inside the script).
    """
    parts = [
        f"trace={result.trace_id}",
        f"ok={1 if result.ok else 0}",
        f"model={result.model or '-'}",
        f"in={result.input_tokens}",
        f"out={result.output_tokens}",
    ]
    if result.error:
        parts.append(f"error={result.error[:120]!r}")
    print(f"[llm {' '.join(parts)}]", file=sys.stderr)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _auth_path() -> Path:
    raw = os.environ.get(AUTH_PATH_ENV_VAR, DEFAULT_AUTH_PATH)
    return Path(os.path.expanduser(raw))


def _load_auth(auth_path: Path) -> dict:
    with open(auth_path, "r", encoding="utf-8") as f:
        auth = json.load(f)
    # Validate required fields exist so KeyError is raised here, not deeper.
    _ = auth["tokens"]["access_token"]
    return auth


def _build_request_body(
    *,
    prompt: str | None,
    instructions: str,
    model: str,
    json_mode: bool,
    tools: list[dict] | None = None,
    input_items: list[dict] | None = None,
) -> dict:
    if input_items is not None:
        input_value = input_items
    else:
        input_value = [{"role": "user", "content": prompt}]

    body: dict = {
        "model": model,
        "instructions": instructions,
        "input": input_value,
        "store": False,
        "stream": True,
    }
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    if tools:
        body["tools"] = tools
    return body


def _post_responses(auth: dict, body: dict, *, timeout: int):
    """POST to the codex/responses endpoint and return the open HTTP
    response for SSE streaming. Caller must iterate/read it and then
    close it."""
    access_token = auth["tokens"]["access_token"]
    account_id = auth["tokens"].get("account_id", "") or ""
    req = urllib.request.Request(
        CODEX_RESPONSES_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "chatgpt-account-id": account_id,
            "User-Agent": CLAWFORD_USER_AGENT,
            "originator": "clawford",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _parse_sse_response(response) -> InferResult:
    """Read an SSE stream and extract text, usage, and tool calls.

    Events of interest:
      - response.output_text.done: final text in `.text`
      - response.output_item.done: if item.type=="function_call", the
        complete function-call item is here with {call_id, name, arguments}
      - response.completed: model, usage, response_id
    """
    final_text = ""
    model = ""
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    function_call: dict | None = None
    response_id = ""

    for raw_line in response:
        try:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
        except AttributeError:
            line = str(raw_line).rstrip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = event.get("type", "")
        if etype == "response.output_text.done":
            final_text = event.get("text", "")
        elif etype == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                raw_args = item.get("arguments", "")
                try:
                    parsed_args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    parsed_args = {"__raw": raw_args}
                function_call = {
                    "call_id": item.get("call_id", "") or item.get("id", ""),
                    "name": item.get("name", ""),
                    "arguments": parsed_args,
                }
        elif etype == "response.completed":
            resp = event.get("response") or {}
            model = resp.get("model", "") or model
            response_id = resp.get("id", "") or response_id
            usage = resp.get("usage") or {}
            input_tokens = usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("output_tokens", 0) or 0
            total_tokens = usage.get("total_tokens", 0) or (input_tokens + output_tokens)

    return InferResult(
        text=final_text,
        model=model,
        provider="openai-codex",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        returncode=0,
        function_call=function_call,
        response_id=response_id,
    )


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path):
    """Inter-process exclusive lock on a sidecar file. POSIX uses
    fcntl.flock; Windows is a no-op (the fleet doesn't run there).

    Used to serialize the read-refresh-write critical section in
    _refresh_access_token. Without this, two agents that both hit a
    401 in the same minute will both POST to auth.openai.com with the
    same refresh_token, OpenAI marks the family as reused and revokes
    every issued token, and the entire fleet is bricked until the
    operator re-authenticates interactively. (Observed 2026-05-03.)
    """
    try:
        import fcntl  # POSIX-only
    except ImportError:
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


def _refresh_access_token(auth: dict, auth_path: Path) -> dict:
    """Post to https://auth.openai.com/oauth/token with the refresh_token
    and get a new access_token. Persist the rotated tokens to auth.json
    atomically. Return the updated auth dict.

    Race protection: holds an exclusive file lock on a sidecar across
    the entire critical section, then re-reads auth.json after acquiring
    the lock. If a sibling process already rotated the tokens (the
    on-disk access_token differs from our stale one), use the on-disk
    auth and skip the network call entirely. Only the first arriver
    actually posts to auth.openai.com — preventing refresh_token_reused
    revocation when the fleet's morning crons overlap.

    Raises the underlying HTTPError/URLError on failure so the caller
    can decide what to do.
    """
    stale_access_token = auth["tokens"].get("access_token", "")
    lock_path = auth_path.with_name(auth_path.name + ".lock")

    with _exclusive_lock(lock_path):
        # Re-read after acquiring the lock — a sibling may have already
        # refreshed while we were waiting (or while we were assembling
        # the request that just got 401'd).
        try:
            current = _load_auth(auth_path)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            current = auth

        if current["tokens"].get("access_token", "") != stale_access_token:
            # Someone else already rotated. Use their tokens; do not
            # spend our (now-stale) refresh_token a second time.
            return current

        refresh_token = current["tokens"]["refresh_token"]
        req_body = {
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=json.dumps(req_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": CLAWFORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        new_tokens = json.loads(raw)

        # Merge the rotated fields. Keep auth_mode, account_id, etc. intact.
        current["tokens"]["access_token"] = new_tokens["access_token"]
        if "id_token" in new_tokens:
            current["tokens"]["id_token"] = new_tokens["id_token"]
        if "refresh_token" in new_tokens:
            current["tokens"]["refresh_token"] = new_tokens["refresh_token"]
        current["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Atomic write: write to a sibling tmp file, then rename.
        tmp_path = auth_path.with_name(auth_path.name + ".tmp")
        tmp_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        tmp_path.replace(auth_path)
        try:
            os.chmod(auth_path, 0o600)
        except OSError:
            # Windows filesystems may reject chmod — ignore, the posix
            # semantic isn't meaningful there anyway.
            pass

        return current


def _format_http_error(e: urllib.error.HTTPError) -> str:
    body = ""
    try:
        body = e.read().decode("utf-8", errors="replace")[:300]
    except Exception:
        pass
    return f"HTTP {e.code} {e.reason}: {body}"


def _format_refresh_error(e: urllib.error.HTTPError) -> str:
    body = ""
    try:
        body = e.read().decode("utf-8", errors="replace")[:300]
    except Exception:
        pass
    return f"token refresh failed: HTTP {e.code} {e.reason}: {body}"
