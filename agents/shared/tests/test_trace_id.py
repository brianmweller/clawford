"""P0.3 — structured logging with trace_id + parameters_hash.

Tests the forensic envelope fields added in Phase P0.3 of the security
hardening roadmap (see C:/Users/operator/.claude/plans/snug-frolicking-summit.md):

  (A) contract_wrap.py auto-injects `trace_id`, `agent_id`, `tool_name`
      into the wrapped-output envelope. These fields can be overridden by
      target scripts and propagate to subprocesses via the
      CLAWFORD_TRACE_ID env var.
  (B) `parameters_hash(payload)` is exported from contract_wrap for
      scripts to compute a stable SHA-256 of any action payload, for
      rate-limiting (P1.3) and anomaly detection (P0.2 Doctor Agent).
  (C) llm.py's infer() accepts an optional `trace_id` kwarg, reads
      CLAWFORD_TRACE_ID from the environment as a fallback, attaches
      the id to the returned InferResult, and emits one stderr log
      line per call so forensics can reconstruct "which LLM call
      produced which action".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
WRAPPER = SHARED_DIR / "contract_wrap.py"

sys.path.insert(0, str(SHARED_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _write_script(dirpath: Path, name: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text(body, encoding="utf-8")
    return p


def _write_fake_agent_script(tmp_path: Path, agent_name: str, script_name: str, body: str) -> Path:
    """Mirror the repo layout: tmp/agents/<agent>/scripts/<script>.py."""
    scripts_dir = tmp_path / "agents" / agent_name / "scripts"
    return _write_script(scripts_dir, script_name, body)


def _run_wrapper(target: Path, *, env: dict | None = None, timeout: int = 15) -> dict:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), str(target)],
        capture_output=True, text=True,
        env=merged_env,
        timeout=timeout,
    )
    assert proc.returncode == 0, f"wrapper must exit 0; stderr={proc.stderr}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, "wrapper must print at least one JSON line"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# (A) contract_wrap.py auto-injection
# ---------------------------------------------------------------------------


def test_envelope_has_trace_id(tmp_path: Path) -> None:
    """Wrapper auto-injects trace_id when target doesn't provide one."""
    target = _write_fake_agent_script(
        tmp_path, "demo-agent", "doit.py",
        'import json; print(json.dumps({"status": "ok"}))\n',
    )
    result = _run_wrapper(target)
    trace_id = result.get("trace_id")
    assert trace_id, f"envelope must contain trace_id; got {result!r}"
    assert UUID_RE.match(trace_id), f"trace_id must be UUID-shaped; got {trace_id!r}"


def test_envelope_has_agent_id_derived_from_path(tmp_path: Path) -> None:
    """agent_id is inferred from `.../agents/<agent>/scripts/...`."""
    target = _write_fake_agent_script(
        tmp_path, "shopping", "costco-orders.py",
        'import json; print(json.dumps({"status": "ok"}))\n',
    )
    result = _run_wrapper(target)
    assert result.get("agent_id") == "shopping", result


def test_envelope_has_tool_name_derived_from_path(tmp_path: Path) -> None:
    """tool_name is the target script's stem."""
    target = _write_fake_agent_script(
        tmp_path, "shopping", "costco-orders.py",
        'import json; print(json.dumps({"status": "ok"}))\n',
    )
    result = _run_wrapper(target)
    assert result.get("tool_name") == "costco-orders", result


def test_trace_id_from_env_var_preserved(tmp_path: Path) -> None:
    """If CLAWFORD_TRACE_ID is set on the wrapper's env, envelope uses it."""
    target = _write_fake_agent_script(
        tmp_path, "demo-agent", "doit.py",
        'import json; print(json.dumps({"status": "ok"}))\n',
    )
    result = _run_wrapper(target, env={"CLAWFORD_TRACE_ID": "fixed-trace-from-outside"})
    assert result.get("trace_id") == "fixed-trace-from-outside"


def test_wrapper_generates_trace_id_when_absent(tmp_path: Path) -> None:
    """No env var, target silent → wrapper still provides a UUID trace_id."""
    target = _write_fake_agent_script(
        tmp_path, "demo-agent", "doit.py",
        'import json; print(json.dumps({"status": "ok"}))\n',
    )
    env_no_trace = {k: v for k, v in os.environ.items() if k != "CLAWFORD_TRACE_ID"}
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), str(target)],
        capture_output=True, text=True, env=env_no_trace, timeout=15,
    )
    assert proc.returncode == 0
    result = json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])
    trace_id = result.get("trace_id")
    assert trace_id
    assert UUID_RE.match(trace_id), trace_id


def test_target_can_override_agent_id(tmp_path: Path) -> None:
    """If the target's own JSON carries agent_id, that value wins."""
    target = _write_fake_agent_script(
        tmp_path, "shopping", "costco-orders.py",
        'import json; print(json.dumps({"status": "ok", "agent_id": "hilda"}))\n',
    )
    result = _run_wrapper(target)
    assert result.get("agent_id") == "hilda"


def test_target_can_override_tool_name(tmp_path: Path) -> None:
    """Same for tool_name — target's own value wins."""
    target = _write_fake_agent_script(
        tmp_path, "shopping", "costco-orders.py",
        'import json; print(json.dumps({"status": "ok", "tool_name": "costco-orders-refresh"}))\n',
    )
    result = _run_wrapper(target)
    assert result.get("tool_name") == "costco-orders-refresh"


def test_target_can_override_trace_id(tmp_path: Path) -> None:
    """Target can emit its own trace_id (e.g., inherited from a parent cron)."""
    target = _write_fake_agent_script(
        tmp_path, "demo-agent", "doit.py",
        'import json; print(json.dumps({"status": "ok", "trace_id": "parent-trace-123"}))\n',
    )
    result = _run_wrapper(target)
    assert result.get("trace_id") == "parent-trace-123"


def test_subprocess_inherits_trace_id_env_var(tmp_path: Path) -> None:
    """Target reads CLAWFORD_TRACE_ID from its env; value matches wrapper's id."""
    target = _write_fake_agent_script(
        tmp_path, "demo-agent", "doit.py",
        'import json, os\n'
        'print(json.dumps({"status": "ok", '
        '"_echo_trace_id": os.environ.get("CLAWFORD_TRACE_ID", "")}))\n',
    )
    env_no_trace = {k: v for k, v in os.environ.items() if k != "CLAWFORD_TRACE_ID"}
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), str(target)],
        capture_output=True, text=True, env=env_no_trace, timeout=15,
    )
    assert proc.returncode == 0
    result = json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])
    # The id the wrapper generated and the one the subprocess saw must match.
    assert result["trace_id"] == result["_echo_trace_id"]
    assert result["trace_id"], "must be non-empty"


def test_error_envelope_also_has_trace_id(tmp_path: Path) -> None:
    """Even when the target crashes, the envelope carries forensic fields."""
    target = _write_fake_agent_script(
        tmp_path, "demo-agent", "boom.py",
        'import sys; sys.exit(1)\n',
    )
    result = _run_wrapper(target)
    assert result["status"] == "error"
    assert result.get("trace_id")
    assert result.get("agent_id") == "demo-agent"
    assert result.get("tool_name") == "boom"


# ---------------------------------------------------------------------------
# (B) parameters_hash helper
# ---------------------------------------------------------------------------


def test_parameters_hash_is_deterministic() -> None:
    from contract_wrap import parameters_hash  # type: ignore
    payload = {"to": "@operator", "body": "hello"}
    assert parameters_hash(payload) == parameters_hash(payload)


def test_parameters_hash_is_key_order_independent() -> None:
    from contract_wrap import parameters_hash  # type: ignore
    a = {"to": "@operator", "body": "hello", "attachments": []}
    b = {"attachments": [], "body": "hello", "to": "@operator"}
    assert parameters_hash(a) == parameters_hash(b)


def test_parameters_hash_distinguishes_different_payloads() -> None:
    from contract_wrap import parameters_hash  # type: ignore
    a = {"to": "@operator", "body": "hello"}
    b = {"to": "@operator", "body": "hello world"}
    assert parameters_hash(a) != parameters_hash(b)


def test_parameters_hash_returns_sha256_hex() -> None:
    from contract_wrap import parameters_hash  # type: ignore
    h = parameters_hash({"x": 1})
    assert re.match(r"^[0-9a-f]{64}$", h), f"expected 64-char lowercase hex; got {h!r}"


def test_parameters_hash_handles_nested_structures() -> None:
    from contract_wrap import parameters_hash  # type: ignore
    a = {"items": [{"id": 1, "qty": 2}, {"id": 3, "qty": 4}]}
    b = {"items": [{"qty": 2, "id": 1}, {"qty": 4, "id": 3}]}
    assert parameters_hash(a) == parameters_hash(b)


# ---------------------------------------------------------------------------
# (C) llm.py trace_id threading
# ---------------------------------------------------------------------------


# Tiny copies of the stubs from test_llm — keeping them local avoids
# cross-test-file imports (tests/ isn't packaged) and keeps this file
# self-contained.

import io


class FakeHTTPResponse:
    def __init__(self, body_bytes: bytes, *, status: int = 200):
        self._body = body_bytes
        self._stream = io.BytesIO(body_bytes)
        self.status = status
        self.headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._stream.close()

    def __iter__(self):
        return iter(self._stream.readlines())

    def read(self, *a):
        return self._body

    def close(self):
        try:
            self._stream.close()
        except Exception:
            pass


def _reload_llm():
    for mod in list(sys.modules):
        if mod == "llm" or mod.startswith("llm."):
            del sys.modules[mod]
    import llm  # type: ignore
    return llm


def _sse_happy_path(*, text: str = "hello", model: str = "gpt-5.4") -> bytes:
    """Minimal SSE byte stream the llm shim parses into a success InferResult."""
    events = [
        f'data: {{"type":"response.output_text.done","text":{json.dumps(text)}}}\n',
        f'data: {{"type":"response.completed","response":'
        f'{{"id":"resp_abc","model":{json.dumps(model)},'
        f'"usage":{{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}}}}\n',
    ]
    return "".join(events).encode("utf-8")


@pytest.fixture
def stub_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "tokens": {"access_token": "tok", "refresh_token": "ref", "account_id": "acct"},
    }))
    monkeypatch.setenv("CLAWFORD_CODEX_AUTH_PATH", str(auth_file))


def test_infer_result_carries_trace_id(
    monkeypatch: pytest.MonkeyPatch, stub_auth: None,
) -> None:
    """Explicit trace_id kwarg lands on the returned InferResult."""
    llm = _reload_llm()
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *a, **kw: FakeHTTPResponse(_sse_happy_path()),
    )
    r = llm.infer("say hi", trace_id="my-trace-xyz")
    assert r.ok
    assert getattr(r, "trace_id", None) == "my-trace-xyz"


def test_infer_uses_env_trace_id_when_kwarg_absent(
    monkeypatch: pytest.MonkeyPatch, stub_auth: None,
) -> None:
    """If CLAWFORD_TRACE_ID is set, infer() picks it up."""
    monkeypatch.setenv("CLAWFORD_TRACE_ID", "from-env-abc")
    llm = _reload_llm()
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *a, **kw: FakeHTTPResponse(_sse_happy_path()),
    )
    r = llm.infer("say hi")
    assert r.ok
    assert getattr(r, "trace_id", None) == "from-env-abc"


def test_infer_logs_trace_id_to_stderr(
    monkeypatch: pytest.MonkeyPatch, stub_auth: None, capsys: pytest.CaptureFixture,
) -> None:
    """Each infer() call writes one structured stderr log line tagged with trace_id."""
    llm = _reload_llm()
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *a, **kw: FakeHTTPResponse(_sse_happy_path()),
    )
    r = llm.infer("hello", trace_id="log-me-123")
    assert r.ok
    captured = capsys.readouterr()
    # The log line should be on stderr, contain the trace_id, and be a
    # single parseable shape (e.g., "[llm trace=...]" or a JSON line).
    assert "log-me-123" in captured.err, (
        f"expected trace_id in stderr; got stderr={captured.err!r}"
    )


def test_infer_without_trace_id_still_works(
    monkeypatch: pytest.MonkeyPatch, stub_auth: None,
) -> None:
    """Back-compat: callers not passing trace_id still get a working InferResult."""
    # Ensure no env trace id pollutes this test.
    monkeypatch.delenv("CLAWFORD_TRACE_ID", raising=False)
    llm = _reload_llm()
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *a, **kw: FakeHTTPResponse(_sse_happy_path()),
    )
    r = llm.infer("hello")
    assert r.ok
    # Absent trace_id is represented as empty string (consistent with other
    # optional string fields on InferResult), never None.
    assert getattr(r, "trace_id", "") == ""
