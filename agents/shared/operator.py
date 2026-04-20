"""Operator identity loader.

Agent scripts that distinguish the operator's own inbound/outbound
traffic from other humans need to know which email addresses, name
variants, and Google account belong to the operator. Those values are
personal identifiers and do not belong in tracked source.

Real values live at ``~/.clawford/operator.json``. A committed template
lives at ``agents/shared/operator.json.example``. On a fresh clone,
copy the example to ``~/.clawford/operator.json`` and edit it.

Tests override the path by passing ``config_path`` explicitly or by
setting the ``CLAWFORD_OPERATOR_CONFIG`` environment variable.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.clawford/operator.json"))

_EXAMPLE_HINT = (
    "Copy agents/shared/operator.json.example to ~/.clawford/operator.json "
    "and fill in real values, or set CLAWFORD_OPERATOR_CONFIG=<path>."
)


@dataclass(frozen=True)
class Operator:
    emails: frozenset[str]
    name_variants: frozenset[str]
    slugs: frozenset[str]
    google_email: str


def _normalize_str_set(values) -> frozenset[str]:
    return frozenset(v.strip().lower() for v in values if v and v.strip())


def load_operator(config_path: Path | None = None) -> Operator:
    resolved: Path
    if config_path is not None:
        resolved = Path(config_path)
    elif env_path := os.environ.get("CLAWFORD_OPERATOR_CONFIG"):
        resolved = Path(os.path.expanduser(env_path))
    else:
        resolved = DEFAULT_CONFIG_PATH
    return _load_resolved(resolved)


@lru_cache(maxsize=8)
def _load_resolved(path: Path) -> Operator:
    if not path.exists():
        raise FileNotFoundError(
            f"Operator config not found at {path}. {_EXAMPLE_HINT}"
        )

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    return Operator(
        emails=_normalize_str_set(data.get("emails", [])),
        name_variants=_normalize_str_set(data.get("name_variants", [])),
        slugs=_normalize_str_set(data.get("slugs", [])),
        google_email=data["google_email"].strip().lower(),
    )
