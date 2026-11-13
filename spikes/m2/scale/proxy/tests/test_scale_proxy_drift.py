"""The scale harness against the package it measures: fail when the package moves from under it.

The harness wraps timers around methods of the store, the proxy handler and the engine by name
(``worker.py``). On 2026-11-12 it had not run since the store was split into ``contexto/store/*.py``:
``SessionStore._pages`` was gone and nothing in the suite noticed. These tests name every attribute the
harness reaches for, so the next refactor fails here instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import worker  # noqa: E402

from contexto.store import SessionStore  # noqa: E402


def test_every_instrumented_name_is_an_attribute_of_its_class():
    missing = [f"SessionStore.{name}" for name in worker.STORE_METHODS if not hasattr(SessionStore, name)]
    assert not missing, f"the harness instruments names the package no longer has: {missing}"
