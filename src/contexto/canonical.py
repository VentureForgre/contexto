"""Canonical JSON, defined once for every hash, id, and byte count in Contexto, and the one bounded
reader for JSON that comes from outside Contexto.

Canonical JSON is ``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
encoded as UTF-8: keys sorted, no whitespace, non-ASCII characters kept as themselves.
"""

from __future__ import annotations

import json
from typing import Any

# The deepest nesting of objects and arrays Contexto reads, the outermost counted as level 1 (rule 18).
# The Messages API refuses a request body nested more than 127 levels (spikes/m2/deepjson). From
# Python 3.14, json.loads parses deeper than json.dumps can write (about 116,000 against 61,500 for
# objects), and a recursive Python walk stops near 1,000 (the default recursion limit). Every value
# read through ``loads_bounded`` can therefore be serialized and walked on every supported Python.
MAX_JSON_DEPTH = 512


class JSONTooDeepError(ValueError):
    """A JSON document nests objects and arrays deeper than ``MAX_JSON_DEPTH``. A ValueError, so a
    caller that treats invalid JSON as unreadable treats this the same way."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def nesting_exceeds(value: Any, limit: int = MAX_JSON_DEPTH) -> bool:
    """Whether ``value`` (as json.loads returns it) nests dicts and lists more than ``limit`` levels.
    Iterative, one level at a time, so any depth json.loads produced can be measured."""
    level = [value] if type(value) is dict or type(value) is list else []
    depth = 0
    while level:
        depth += 1
        if depth > limit:
            return True
        below = []
        for container in level:
            for item in container.values() if type(container) is dict else container:
                if type(item) is dict or type(item) is list:
                    below.append(item)
        level = below
    return False


def loads_bounded(data: str | bytes | bytearray) -> Any:
    """``json.loads`` for a document from outside Contexto (a request or response body, an SSE event,
    an MCP line, tool output, a page file). Raises ``JSONTooDeepError`` when the document is nested
    deeper than ``MAX_JSON_DEPTH``, including when json.loads itself runs out of recursion."""
    try:
        value = json.loads(data)
    except RecursionError:
        raise JSONTooDeepError(f"nested deeper than {MAX_JSON_DEPTH} levels") from None
    if nesting_exceeds(value):
        raise JSONTooDeepError(f"nested deeper than {MAX_JSON_DEPTH} levels")
    return value
