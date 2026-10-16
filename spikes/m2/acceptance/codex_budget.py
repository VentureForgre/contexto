"""Worst-case token estimate for the Codex acceptance run (M2 A16), for intake's budget line.

A closed formula over the D27 script, not a simulation: every term is an upper bound with its source,
so the total can only be exceeded if a stated assumption breaks (and the driver's cap stops the run
before it can be). Tokens are counted the way the Responses API bills them: ``input_tokens`` (cached
included) plus ``output_tokens``.

H1 (``contexto run codex``, bound B):
- every request is at most B input tokens (the engine's bound; acceptance 1 fails otherwise), so each
  request costs at most ``B + R`` with R the per-request output allowance;
- requests: one per content turn, and ``1 + T`` per probe turn (one per tool round: Codex calls the
  Contexto tools inside one ``exec`` call per round, then answers);
- one billed retry per turn (Codex retries a dropped stream; the retried request is billed again).

Baseline (the same script and turns with no rotation, ``--bound`` above the history and Codex's own
compaction disabled, tools off as Claude's replay sets them): request k sends the full history so far,
``head + Σ turns ≤ k + replies``, plus R output; one request per turn and the same retry allowance. It
must fit under Codex's compaction point (``baseline_fits``), which caps the bound near 64,000.

Total = (H1 + baseline) × (1 + margin). The driver stops H1 at ``approved - baseline_worst``."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from acceptance import script

# One adapter over budget.py (lane B): builder-4 is changing that module (Params.recalls becomes
# tool_rounds), so everything A16 takes from it passes through here and a rename is one line.
from acceptance import budget as _budget


def turn_tokens(turn_bytes: int) -> int:
    """Tokens for a pasted log turn at the dense log ratio (budget.BYTES_PER_TOKEN_LOG, 1.8)."""
    return _budget.turn_tokens(turn_bytes)


CODEX_HEAD = 21_312  # A6 pre-flight: the seed estimate of Codex 0.154.0's first request with the
# contexto MCP server registered (docs/research/M2-codex-smoke.md); the live head measured 15,309,
# so the larger figure is used as the bound.
OUTPUT_ALLOWANCE = 32_000  # per request: Codex sets no max_output_tokens, so the guard reserves the
# same output budget.py reserves per Claude request (budget.Params.max_tokens_reserve)
REPLY_TOKENS = 20  # "OK" per content turn (budget.Params.reply_tokens)
PROBE_ANSWER_TOKENS = 4_000  # one probe turn's answer lines (budget.Params.probe_output)
CODEX_COMPACT_AT = 244_800  # gpt-6-astra: Codex compacts at this many tokens of its 272,000 window
# (docs/research/M2-codex-adapter.md); a full-context baseline must stay under it


@dataclass
class CodexBudget:
    bound: int
    content_turns: int
    probe_turns: int
    tool_rounds: int
    retries_per_turn: int
    margin: float
    h1_requests: int = 0
    h1_worst: int = 0
    baseline_requests: int = 0
    baseline_worst: int = 0
    baseline_max_input: int = 0
    total_worst: int = 0
    baseline_fits: bool = False  # the baseline's largest request stays under Codex's own compaction
    terms: dict[str, str] = field(default_factory=dict)


def estimate(s: script.Script, bound: int, tool_rounds: int = script.MAX_RECALLS, retries_per_turn: int = 1,
             margin: float = 0.10, head: int = CODEX_HEAD, output: int = OUTPUT_ALLOWANCE) -> CodexBudget:
    """The worst case for one H1 run plus one baseline run of script ``s`` (module docstring)."""
    b = CodexBudget(bound, s.n_content, len(s.probe_turn_ids), tool_rounds, retries_per_turn, margin)
    per_probe_turn = 1 + tool_rounds
    b.h1_requests = s.n_content + b.probe_turns * per_probe_turn + (s.n_content + b.probe_turns) * retries_per_turn
    b.h1_worst = b.h1_requests * (bound + output)

    history, cost, requests = head, 0, 0
    for size in s.content_bytes:
        history += turn_tokens(size)
        turn = (1 + retries_per_turn) * (history + output)
        cost, requests, history = cost + turn, requests + 1 + retries_per_turn, history + REPLY_TOKENS
    for size in s.probe_bytes:
        # The baseline has the whole history in view and its tools disabled (as Claude's replay sets
        # tool_choice none): one request per probe turn.
        history += turn_tokens(size)
        cost += (1 + retries_per_turn) * (history + output)
        requests += 1 + retries_per_turn
        history += PROBE_ANSWER_TOKENS
    b.baseline_requests, b.baseline_worst, b.baseline_max_input = requests, cost, history
    b.baseline_fits = history <= CODEX_COMPACT_AT
    b.total_worst = math.ceil((b.h1_worst + b.baseline_worst) * (1 + margin))
    b.terms = {
        "bound": f"{bound:,} (H1 --bound)",
        "head": f"{head:,} (A6 pre-flight seed estimate, docs/research/M2-codex-smoke.md)",
        "output": f"{output:,} per request (budget.Params.max_tokens_reserve)",
        "content turns": f"{s.n_content} (script, D27: history at least 2.5x the bound)",
        "probe turns": f"{b.probe_turns} x {script.PROBES_PER_TURN} probes (D27)",
        "tool rounds": f"{tool_rounds} per probe turn in H1 (script.MAX_RECALLS); the baseline has none",
        "retries": f"{retries_per_turn} billed retry per turn",
        "margin": f"{margin:.0%}",
    }
    return b


def formula(b: CodexBudget) -> str:
    return (f"H1 = ({b.content_turns} + {b.probe_turns} x (1 + {b.tool_rounds}) + ({b.content_turns} + {b.probe_turns}) x "
            f"{b.retries_per_turn}) x ({b.bound:,} + {OUTPUT_ALLOWANCE:,}) = {b.h1_requests} x {b.bound + OUTPUT_ALLOWANCE:,} "
            f"= {b.h1_worst:,}; baseline = {b.baseline_worst:,} over {b.baseline_requests} requests (largest input "
            f"{b.baseline_max_input:,}); total = (H1 + baseline) x {1 + b.margin:.2f} = {b.total_worst:,}")
