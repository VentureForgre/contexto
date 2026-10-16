"""Recall guidance in the pinned text and the page index, and its variants (M2 H1 recall fix).

The pinned text always opens with the contract's mandatory line: earlier turns are paged out and
how to recall them. It must also tell the model to recall before relying on a paged-out detail and
to pin decisions and obligations as it makes them (M2 contract, MCP server).

- ``default``: the conformant opening lines, a worked example of one search and one recall, and
  index lines that list each page's salient identifiers. This is variant V2 of the H1 experiment,
  which intake chose (2026-10-16 03:26Z).
- ``legacy``: the text shipped before the H1 fix, kept for diagnosis: it reproduces the preamble
  of a run recorded before the fix byte for byte.

Placement of the preamble is lane A's (the rewrite engine).

The variant is chosen by ``CONTEXTO_RECALL_VARIANT`` or an explicit argument, and a session store
records the first one it resolves so the text stays fixed for the session.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

VARIANTS = ("default", "legacy")
ENV_VAR = "CONTEXTO_RECALL_VARIANT"

LEGACY_FIRST_LINE = (
    "Earlier turns of this conversation are paged out. Each line of the page index below is one "
    "paged turn; call contexto_recall with its page id to read it, or contexto_search to find one."
)

RULE_LINES = (
    "Earlier turns of this conversation are paged out of your context; to read one, call "
    "contexto_search with an exact term (a key, name, or identifier) and then contexto_recall with a "
    "page id it returns.",
    "Do this before you rely on a detail from an earlier turn, and before you answer that something is "
    "unknown or not in the conversation. The page index below lists the paged turns, newest first.",
    "As you work, record decisions and obligations with contexto_pin, and close finished obligations "
    "with contexto_close, so they stay in view.",
)

EXAMPLE_LINE = (
    'Example: to find the value of cache.ttl_seconds, call contexto_search with {"query": '
    '"cache.ttl_seconds"}; it lists matching pages such as "pg_0123456789abcdef (turn 3): ...". Then '
    'call contexto_recall with {"page_id": "pg_0123456789abcdef"} and read the value there.'
)

DEFAULT_LINES = (*RULE_LINES, EXAMPLE_LINE)

SALIENT_MAX = 12  # identifiers per index line
SALIENT_CHARS = 240  # and at most this many characters of them


def check_variant(variant: str) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"unknown recall variant {variant!r}; expected one of {', '.join(VARIANTS)}")
    return variant


def env_variant() -> str:
    value = os.environ.get(ENV_VAR, "").strip()
    return check_variant(value) if value else "default"


def opening_lines(variant: str) -> tuple[str, ...]:
    """The lines the pinned text (and the full state) start with, for ``variant``."""
    return (LEGACY_FIRST_LINE,) if variant == "legacy" else DEFAULT_LINES


def lists_identifiers(variant: str) -> bool:
    return variant != "legacy"


# An allowlist: only tokens in key position, and file paths, are ever listed (intake, 2026-10-16
# 03:54Z). A value, a list item, or an array element is never in key position, so a value syntax
# not anticipated here cannot leak into the index.
_IDENT = r"[A-Za-z][\w.\-/]*"
_EQ_KEY = re.compile(rf"(?<![\w.\-/=:\"'])({_IDENT})[ \t]*=(?!=)")  # key=value, key = value
_LINE_KEY = re.compile(rf"^[ \t]*(?:-[ \t]+)?[\"']?({_IDENT})[\"']?[ \t]*:(?=[ \t]|$)", re.MULTILINE)  # YAML, logs
_QUOTED_KEY = re.compile(rf"\"({_IDENT})\"[ \t]*:")  # JSON that does not parse (for example, cut off)
_FILE_EXTENSIONS = ("json|jsonl|yaml|yml|toml|ini|cfg|conf|env|py|js|ts|tsx|go|rs|java|rb|sh|sql|md|txt|log|csv"
                    "|html|css|xml|lock")
_PATH = re.compile(rf"(?<![\w.\-/])(?:(?:[\w.\-]+/)+[\w\-]*[A-Za-z][\w\-]*\.[A-Za-z]{{1,5}}"
                   rf"|[A-Za-z][\w\-]*\.(?:{_FILE_EXTENSIONS}))(?![\w/]|\.\w)")


def _value_like(ident: str) -> bool:
    """Any digit marks a value: ids (AI-93532), cohorts (onyx-4681), regions (west-lo2), versions."""
    return any(c.isdigit() for c in ident)


def _json_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [k for key, item in value.items() for k in [key, *_json_keys(item)] if isinstance(k, str)]
    if isinstance(value, list):
        return [k for item in value for k in _json_keys(item)]
    return []


def _parse_json(text: str) -> tuple[bool, list[str]]:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False, []
    try:
        return True, _json_keys(json.loads(stripped))
    except ValueError:
        return False, []


def _in_value_position(text: str, start: int) -> bool:
    """After =, :, [ or a comma, or on a '- ' list line: a path there is a value or a list item."""
    i = start - 1
    while i >= 0 and text[i] in " \t\"'":
        i -= 1
    if i >= 0 and text[i] in "=:[,":
        return True
    return text[text.rfind("\n", 0, start) + 1 : start].lstrip().startswith("-")


def _key_position_tokens(text: str) -> list[str]:
    """Key-position tokens and file paths of one text, in order of appearance."""
    parsed, keys = _parse_json(text)
    if parsed:
        return keys  # valid JSON: object keys only, never a value or an array element
    found: list[tuple[int, str]] = []
    rest: list[str] = []
    offset = 0  # length of rest so far, so each line costs its own length, not the text's
    for line in text.splitlines(keepends=True):  # JSON Lines: each object line contributes keys only
        parsed, keys = _parse_json(line)
        if parsed:
            found.extend((offset, k) for k in keys)
            line = "\n"
        rest.append(line)
        offset += len(line)
    plain = "".join(rest)
    for pattern in (_EQ_KEY, _LINE_KEY, _QUOTED_KEY):
        found.extend((m.start(1), m.group(1)) for m in pattern.finditer(plain))
    found.extend((m.start(), m.group()) for m in _PATH.finditer(plain) if not _in_value_position(plain, m.start()))
    return [token for _, token in sorted(found, key=lambda pair: pair[0])]


def salient_identifiers(texts: Iterable[str], limit: int = SALIENT_MAX, max_chars: int = SALIENT_CHARS) -> list[str]:
    """The page's rarest identifiers in key position, and file paths; never values.

    The index should name what a page is about without giving its answers away, so it lists only
    an allowlist of positions: JSON object keys (when the text, or a line of it, parses as JSON),
    the left side of ``key=value`` and ``key = value``, ``key:`` at the start of a line (YAML,
    including ``- key:``), ``"key":``, and file paths that are not themselves in value or list
    position. Values, list items, array elements, and names in prose are never listed. Neither is
    any identifier containing a digit (ids such as AI-93532 used as keys), so names with an
    incidental digit (s3-bucket) are left to search, which finds them in the page text.

    Limit: a value written in key position (an answer used as a JSON key, or a line that starts
    with it followed by a colon) is listed like any key. Identifiers are ranked by how rarely they
    occur in the page (a one-off key outranks a field repeated on every log line), then by first
    appearance.
    """
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    order = 0
    for text in texts:
        for ident in _key_position_tokens(text):
            ident = ident.strip("./-")
            if not ident or not any(c.isalpha() for c in ident) or _value_like(ident):
                continue
            counts[ident] += 1
            if ident not in first:
                first[ident] = order
                order += 1
    ranked = sorted(counts, key=lambda i: (counts[i], first[i]))
    out: list[str] = []
    used = 0
    for ident in ranked:
        if any(ident != o and ident in o.split(".") for o in out):
            continue  # a part of an identifier already listed adds nothing
        if len(out) >= limit or used + len(ident) + 2 > max_chars:
            break
        out.append(ident)
        used += len(ident) + 2
    return out
