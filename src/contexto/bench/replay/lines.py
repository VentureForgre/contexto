"""The two modelled lines of the request-size chart: full context, and plain Claude Code.

Neither is a run. Both are computed from the script and the head's size, one request per turn at the
turn's full size, on the scale of the script's own estimate (``contexto.bench.script.estimate``: log
text at 1.8 bytes per token plus 20 tokens per reply, probe turns at 3.5 bytes per token).

``full``
    The head plus the whole history so far, never reduced: what a client sends when nothing bounds
    the conversation.
``plain``
    The same growth until the next request would pass the compaction trigger, here the bound. Then
    one compaction request (the whole context so far goes in, a summary comes out) and the turn is
    served from the head, the summary and the new turn; then growth again.

What the plain model takes from this repository, and what it assumes:

- Summary size, ``SUMMARY_TOKENS`` = 7,920: the mean output of the three compaction summary calls
  measured in docs/research/M0-proxy-measurements.md, finding 6 (5,510, 8,908 and 9,342 tokens;
  Claude Code 2.1.301, claude-haiku-5-5, ``--autocompact 100000``).
- What survives a compaction: the head, the summary and the turn being served. Nothing else is kept
  and nothing is re-attached. This is an assumption. The repository has no measurement of what
  Claude Code re-attaches after compacting a session of pasted logs: the same finding's requests
  after a compaction (75,873 and 38,887 tokens) include files the model read again, and
  docs/research/M0-cost-model.md marks its own 15,000 tokens of re-attached context as inferred.
  The assumption errs toward plain Claude Code: anything re-attached after a compaction would make
  its requests larger and its compactions more frequent than the line shows.
- The trigger: a request above the bound. Claude Code's real trigger is its auto-compact window less
  a margin (docs/research/M0-interception.md, section 3), and whether it can be set to a given bound
  is for the live run to report; a replay only draws where a compaction at the bound would fall.
"""

from __future__ import annotations

from typing import Any

from contexto.bench import script as bench_script
from contexto.bench.d27 import BYTES_PER_TOKEN_LOG, BYTES_PER_TOKEN_TEXT, REPLY_TOKENS, turn_tokens
from contexto.bench.model import Script

SUMMARY_TOKENS = 7_920
SUMMARY_SOURCE = ("docs/research/M0-proxy-measurements.md, finding 6: the mean output of three measured compaction "
                  "summary calls (5,510, 8,908 and 9,342 tokens)")
SURVIVES = ("assumed, not measured: the head, the summary and the turn being served; nothing else is kept or "
            "re-attached. This errs toward plain Claude Code: anything re-attached would make its requests larger "
            "and its compactions more frequent")
RATES_TEXT = (f"log text at {BYTES_PER_TOKEN_LOG} bytes per token plus {REPLY_TOKENS} tokens per reply, probe turns "
              f"at {BYTES_PER_TOKEN_TEXT} bytes per token")


def turn_estimates(script: Script) -> list[int]:
    """Each turn's token estimate, by the rule of the output schema: a content turn is
    ``turn_tokens(load_bytes) + REPLY_TOKENS``, a probe turn ``int(bytes / 3.5) + 1``."""
    out = []
    for i in range(len(script.turns)):
        if i < script.n_content:
            out.append(turn_tokens(bench_script.load_bytes(script, i)) + REPLY_TOKENS)
        else:
            out.append(int(script.turn_bytes[i] / BYTES_PER_TOKEN_TEXT) + 1)
    return out


def history_tokens(script: Script) -> list[int]:
    """For each turn, how much conversation the user has had by that turn: the sum of the estimates
    of turns 0 to it. The last value equals ``script.estimate(s)["tokens"]``."""
    out, total = [], 0
    for estimate in turn_estimates(script):
        total += estimate
        out.append(total)
    return out


def _row(arm: str, seed: int, n: int, turn: int, history: int, tokens: int, output: int, *, main: bool = True,
         compaction: bool = False) -> dict[str, Any]:
    return {"arm": arm, "seed": seed, "repeat": 0, "model": None, "n": n, "turn": turn, "main": main,
            "history_tokens": history, "uncached": tokens, "cache_write": 0, "cache_read": 0, "output": output,
            "status": 200, "compaction": compaction, "basis": "modelled"}


def _output(script: Script, turn: int) -> int:
    return REPLY_TOKENS if turn < script.n_content else 0


def full_rows(script: Script, head_tokens: int) -> list[dict[str, Any]]:
    """The ``full`` arm's ``requests.jsonl`` rows: one request per turn, the head plus the history."""
    return [_row("full", script.seed, turn, turn, history, head_tokens + history, _output(script, turn))
            for turn, history in enumerate(history_tokens(script))]


def plain_rows(script: Script, head_tokens: int, trigger: int) -> list[dict[str, Any]]:
    """The ``plain`` arm's rows: one main request per turn, and before a turn whose request would pass
    ``trigger`` one compaction request (``compaction`` true, ``main`` false) whose input is the
    context it summarises and whose output is the summary."""
    rows: list[dict[str, Any]] = []
    carried = 0  # what the next request carries besides the head and the new turn
    history = history_tokens(script)
    for turn, estimate in enumerate(turn_estimates(script)):
        if carried and head_tokens + carried + estimate > trigger:
            rows.append(_row("plain", script.seed, len(rows), turn, history[turn], head_tokens + carried,
                             SUMMARY_TOKENS, main=False, compaction=True))
            carried = SUMMARY_TOKENS
        rows.append(_row("plain", script.seed, len(rows), turn, history[turn], head_tokens + carried + estimate,
                         _output(script, turn)))
        carried += estimate
    return rows


def compactions(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if row["compaction"])
