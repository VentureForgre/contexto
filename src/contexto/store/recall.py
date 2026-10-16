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

import os
import re
from collections import Counter
from collections.abc import Iterable

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


# A key in key=value, or a dotted, underscored, dashed, or slashed name starting with a letter.
_KEY = re.compile(r"([A-Za-z][\w.\-/]*?)=")
_NAME = re.compile(r"(?<![\w.\-/])[A-Za-z][A-Za-z0-9]*(?:[._\-/][A-Za-z0-9]+)+")
_VALUE = re.compile(r"=[^\s,;]*")


def _value_like(ident: str) -> bool:
    """Any digit marks a value: ids (AI-93532), cohorts (onyx-4681), regions (west-lo2), versions."""
    return any(c.isdigit() for c in ident)


def salient_identifiers(texts: Iterable[str], limit: int = SALIENT_MAX, max_chars: int = SALIENT_CHARS) -> list[str]:
    """The page's rarest identifiers: keys of key=value pairs and multi-part names, never values.

    Values are never listed, so the index names what a page is about without giving its answers
    away: anything right after ``=``, and any identifier containing a digit (ids such as AI-93532,
    cohorts such as onyx-4681, regions such as west-lo2, versions such as v2.13.0) are left out. The
    cost is that names with an incidental digit (s3-bucket) are not listed either; search still
    finds them in the page text. Identifiers are ranked by how rarely they occur in
    the page (a one-off key outranks a field repeated on every log line), then by first appearance.
    """
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    order = 0
    for text in texts:
        keys = _KEY.findall(text)
        names = _NAME.findall(_VALUE.sub("=", text))
        for ident in keys + names:
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
