"""Canonical JSON, defined once for every hash, id, and byte count in Contexto.

Canonical JSON is ``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
encoded as UTF-8: keys sorted, no whitespace, non-ASCII characters kept as themselves.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")
