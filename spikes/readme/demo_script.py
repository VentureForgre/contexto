"""The scripted session of the README recording: what the stub CLI says and how sizes are counted.

Shared by ``stub_claude.py`` (the stand-in for Claude Code) and ``demo_upstream.py`` (the stand-in for
the model API), so both count a request the same way. Nothing here is a model: the "model" is the
three rules in ``demo_upstream.scripted_reply``.

Sizes: there is no tokenizer in a run without a model, so both sides count one token per
``BYTES_PER_TOKEN`` bytes of request body. 3.5 is the rate Contexto's own estimator assumes for text
before it has seen real usage (``contexto.tokens.TEXT_BYTES_PER_TOKEN``).
"""

from __future__ import annotations

import math

BYTES_PER_TOKEN = 3.5
STUB_LABEL = "[stub claude]"
UPSTREAM_LABEL = "[fake upstream]"
SYNTHETIC_LABEL = "Synthetic session: a stub CLI and a fake upstream on 127.0.0.1. No model."

GROWTH_TURNS = 8
TURN_BYTES = 42_000  # about 12,000 tokens of pasted build log a turn
PLANT_TURN = 2
# A made-up name, not a credential. It sits after the log, so it is not in the opening 200 characters
# that Contexto's page index keeps in view: only a search of the paged-out turn finds it.
PLANT_FACT = "amber-kestrel-17"
PLANT = f"The staging deploy token is named {PLANT_FACT}."
QUESTION = "What is the staging deploy token named?"
QUERY = "staging deploy token"
SEARCH_TOOL = "mcp__contexto__contexto_search"
MODEL = "stub-model"


def tokens(nbytes: int) -> int:
    """Tokens both stand-ins count for ``nbytes`` of request body."""
    return math.ceil(nbytes / BYTES_PER_TOKEN)


def build_log(turn: int, nbytes: int) -> str:
    """About ``nbytes`` of made-up build log for ``turn``. Deterministic, at most 60 columns a line, and
    free of the words in ``QUERY``, so only the planted sentence matches the search."""
    lines = []
    size = 0
    n = 0
    while size < nbytes:
        n += 1
        ms = (turn * 7919 + n * 104729) % 900 + 100
        kind = ("compiled", "linked", "tested", "packed")[(turn + n) % 4]
        line = f"build {turn:02d}.{n:04d} {kind} module mod_{(turn * 31 + n) % 257:03d} in {ms} ms"
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def prompt(turn: int) -> str:
    """The user message of growth turn ``turn`` (1-based)."""
    text = f"Turn {turn}: here is the build log for step {turn}. Keep it for reference.\n{build_log(turn, TURN_BYTES)}"
    if turn == PLANT_TURN:
        text += f"\n{PLANT}"
    return text
