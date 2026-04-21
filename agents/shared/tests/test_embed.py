"""Tests for agents/shared/embed.py — local embedding wrapper.

The wrapper exists so cross-run semantic dedupe in the Gmail miner has a
cheap, deterministic similarity signal without sending text to any
external API. We lazy-load a fastembed singleton (ONNX under the hood,
CPU-only) the first time embed() is called; every subsequent call
reuses the cached encoder.

Contract pinned by these tests:
- embed(text) returns a list[float] of fixed dim (384 for BGE-small),
  OR None when the backend is unavailable (import error, model load
  failure, runtime error). Never raises.
- cosine(a, b) is pure math, NaN-safe, returns 0.0 for empty / bad
  inputs rather than blowing up.
- The singleton cache means a second embed() call does NOT re-load
  the model (expensive).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


def _reload():
    """Reload embed to reset the singleton between tests."""
    for mod in list(sys.modules):
        if mod == "embed" or mod.startswith("embed."):
            del sys.modules[mod]
    import embed  # type: ignore
    return embed


# ─── cosine helper ───────────────────────────────────────────────────


def test_cosine_identity_is_1():
    mod = _reload()
    v = [0.1, 0.2, 0.3, 0.4]
    assert abs(mod.cosine(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_is_0():
    mod = _reload()
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert mod.cosine(a, b) == 0.0


def test_cosine_opposite_is_negative_one():
    mod = _reload()
    a = [1.0, 1.0, 1.0]
    b = [-1.0, -1.0, -1.0]
    assert abs(mod.cosine(a, b) - (-1.0)) < 1e-9


def test_cosine_empty_returns_zero():
    mod = _reload()
    assert mod.cosine([], []) == 0.0
    assert mod.cosine([1.0], []) == 0.0


def test_cosine_mismatched_length_returns_zero():
    mod = _reload()
    # Rather than raise — callers can safely feed failed embeds to cosine.
    assert mod.cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_zero_vector_returns_zero():
    mod = _reload()
    assert mod.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ─── embed degrades open ─────────────────────────────────────────────


def test_embed_returns_none_when_fastembed_missing(monkeypatch):
    """If fastembed isn't installed (test env doesn't bundle it), embed
    must return None so callers fall through to heuristic dedupe
    instead of crashing the miner."""
    mod = _reload()
    # Force the lazy loader to fail as if the package were missing.
    monkeypatch.setattr(mod, "_load_encoder", lambda: None)
    assert mod.embed("hello world") is None


def test_embed_returns_none_on_runtime_error(monkeypatch):
    """Any runtime exception from the encoder must surface as None,
    not propagate. The caller's fall-through path is what keeps the
    miner alive under transient failure."""
    mod = _reload()

    class _Broken:
        def embed(self, texts):
            raise RuntimeError("simulated")

    monkeypatch.setattr(mod, "_load_encoder", lambda: _Broken())
    # Reset singleton
    mod._ENCODER = None   # type: ignore[attr-defined]
    assert mod.embed("hello") is None


def test_embed_returns_list_of_floats_on_success(monkeypatch):
    mod = _reload()
    fake_vec = [0.1] * 384

    class _FakeEncoder:
        def embed(self, texts):
            # fastembed returns a generator of numpy arrays; we fake
            # the float-list shape we want out of our wrapper.
            return iter([fake_vec])

    monkeypatch.setattr(mod, "_load_encoder", lambda: _FakeEncoder())
    mod._ENCODER = None   # type: ignore[attr-defined]
    out = mod.embed("hello world")
    assert isinstance(out, list)
    assert len(out) == 384
    assert all(isinstance(x, float) for x in out)


def test_embed_caches_singleton(monkeypatch):
    """Second call must NOT re-invoke the loader — loading the ONNX
    model is expensive (~400ms cold), unacceptable per call."""
    mod = _reload()
    calls = {"n": 0}
    fake_vec = [0.0] * 384

    class _FakeEncoder:
        def embed(self, texts):
            return iter([fake_vec])

    def _loader():
        calls["n"] += 1
        return _FakeEncoder()

    monkeypatch.setattr(mod, "_load_encoder", _loader)
    mod._ENCODER = None   # type: ignore[attr-defined]
    mod.embed("one")
    mod.embed("two")
    mod.embed("three")
    assert calls["n"] == 1


def test_embed_empty_string_returns_none():
    """Don't waste an encoder invocation on whitespace; return None so
    the caller skips the pair entirely."""
    mod = _reload()
    assert mod.embed("") is None
    assert mod.embed("   \n ") is None
