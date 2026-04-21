"""Local embedding wrapper for semantic similarity.

Exists so cross-run fact-dedupe in the Gmail miner has a cheap,
deterministic similarity signal without sending text to an external
API (and without violating the no-API-keys-ever rule). The encoder is
``fastembed`` with the BAAI/bge-small-en-v1.5 model — ONNX backend,
CPU-only, ~50ms per short-text embed once loaded, 384-dim vectors.

Degrades open: any import error, model-load failure, or runtime
exception from the encoder returns None from ``embed()``. Callers
MUST tolerate None and fall back to the heuristic dedupe path rather
than treating embed() as required infrastructure — the miner can
never take down its own cron over an embedding failure.

Lazy singleton: ``_load_encoder()`` only fires on the first embed()
call (cold start ~400ms to parse the ONNX graph). Subsequent calls
reuse the cached instance.
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)


# Exposed as module global so tests can reset between cases.
_ENCODER: Any = None


def _load_encoder() -> Any:
    """Import fastembed and construct the encoder. Returns None when
    the package is missing or the model cannot be loaded. Never
    raises."""
    try:
        from fastembed import TextEmbedding  # type: ignore
    except ImportError:
        log.info("embed: fastembed not installed; embedding unavailable")
        return None
    try:
        # Model name is stable across fastembed versions; cache dir
        # defaults to ~/.cache/fastembed (created on first use).
        return TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    except Exception as exc:  # noqa: BLE001 — any model-load failure degrades open
        log.warning("embed: model load failed: %s", exc)
        return None


def _encoder() -> Any:
    """Return the cached encoder singleton, lazy-loading on first call.
    Returns None when the backend is unavailable (subsequent calls
    re-try cheaply by re-invoking the loader — low cost once it's
    confirmed absent because Python import caching remembers the
    ModuleNotFoundError)."""
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = _load_encoder()
    return _ENCODER


def embed(text: str) -> list[float] | None:
    """Return a list[float] embedding of the input text, or None.

    None on any of:
      - empty / whitespace-only text (skip cost)
      - fastembed unavailable or model-load failure
      - any runtime exception from the encoder
    """
    if not text or not text.strip():
        return None
    encoder = _encoder()
    if encoder is None:
        return None
    try:
        # fastembed.TextEmbedding.embed yields numpy arrays; we want
        # plain list[float] so the result is JSON-serializable and has
        # no numpy dependency in the caller.
        vec = next(iter(encoder.embed([text])))
    except Exception as exc:  # noqa: BLE001
        log.warning("embed: runtime error: %s", exc)
        return None
    try:
        return [float(x) for x in vec]
    except Exception:  # noqa: BLE001
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 for empty vectors, mismatched
    lengths, or zero-norm vectors — all degraded cases rather than
    exceptions so the dedupe loop stays alive on bad input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom
