"""Fleet-wide pytest fixture: clean leaked sys.modules stubs after each test.

Several test files install bare ``types.ModuleType("pkg")`` stubs into
``sys.modules`` so a script's import line (e.g. ``from curl_cffi import
requests``) succeeds on a machine without the real package, or to
short-circuit network calls. Those stubs were installed with raw
``sys.modules[...] = ...`` and never cleaned up, so later tests that
wanted the REAL package (``.Session()``, ``.errors.HttpError``, etc.)
hit ``AttributeError`` or ``ModuleNotFoundError`` under cross-suite
runs.

This fixture is autouse at the ``agents/`` root, so every test in the
fleet has teardown that removes any stub (bare ModuleType, no __file__)
matching the known-polluted prefixes. Real installed packages stay
untouched because they have ``__file__``. Tests that re-stub per-test
via ``_load_script`` continue to work: next test's call re-installs.
"""
from __future__ import annotations

import sys

import pytest

_POLLUTION_PREFIXES = (
    "googleapiclient",
    "google.auth",
    "google.oauth2",
    "curl_cffi",
    "camoufox",
)


def _is_stub_module(name: str) -> bool:
    mod = sys.modules.get(name)
    if mod is None:
        return False
    # Real packages from site-packages have __file__ (or __path__ for
    # namespace pkgs). Bare types.ModuleType("foo") has neither.
    return getattr(mod, "__file__", None) is None and not hasattr(mod, "__path__")


@pytest.fixture(autouse=True)
def _drop_leaked_module_stubs():
    yield
    for name in list(sys.modules):
        if not any(name == p or name.startswith(p + ".") for p in _POLLUTION_PREFIXES):
            continue
        if _is_stub_module(name):
            del sys.modules[name]
