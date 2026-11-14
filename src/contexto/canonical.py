"""Canonical JSON, defined once for every hash, id, and byte count in Contexto, and the one bounded
reader for JSON that comes from outside Contexto.

Canonical JSON is ``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
encoded as UTF-8: keys sorted, no whitespace, non-ASCII characters kept as themselves. A lone UTF-16
surrogate, which has no UTF-8 form, is written as its JSON escape (``utf8``), so the bytes still
parse back to the same value (D58).
"""

from __future__ import annotations

import json
from collections.abc import Callable
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


def utf8(text: str) -> bytes:
    """``text`` as UTF-8, with a lone UTF-16 surrogate (tool output cut in the middle of an emoji, say)
    written as its backslash-u escape instead of raising (D58). Strings without one encode exactly as
    ``str.encode("utf-8")`` does. In JSON text the escape is the same string again, as in
    ``proxy.serialize_request``; elsewhere it is a stable stand-in for hashes and byte counts."""
    return text.encode("utf-8", "backslashreplace")


def has_lone_surrogate(text: str) -> bool:
    """Whether ``text`` holds a lone UTF-16 surrogate, which has no UTF-8 form (a pair in a Python
    string is already one character)."""
    return any("\ud800" <= c <= "\udfff" for c in text)


def canonical_bytes(obj: Any) -> bytes:
    return utf8(canonical_json(obj))


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


def loads_bounded(data: str | bytes | bytearray, *, parse_constant: Callable[[str], Any] | None = None) -> Any:
    """``json.loads`` for a document from outside Contexto (a request or response body, an SSE event,
    an MCP line, tool output, a page file). Raises ``JSONTooDeepError`` when the document is nested
    deeper than ``MAX_JSON_DEPTH``, including when json.loads itself runs out of recursion.
    ``parse_constant`` is json.loads's: by default ``NaN``, ``Infinity`` and ``-Infinity`` are accepted."""
    try:
        value = json.loads(data, parse_constant=parse_constant)
    except RecursionError:
        raise JSONTooDeepError(f"nested deeper than {MAX_JSON_DEPTH} levels") from None
    if nesting_exceeds(value):
        raise JSONTooDeepError(f"nested deeper than {MAX_JSON_DEPTH} levels")
    return value
