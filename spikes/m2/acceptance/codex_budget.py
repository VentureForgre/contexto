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

import json
import math
import sys
from pathlib import Path
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance import script  # noqa: E402

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


CODEX_WINDOW = 272_000  # gpt-6-astra's context window (docs/research/M2-codex-adapter.md); a replayed
# request goes to the upstream directly, so Codex's own compaction does not apply, but the window does


def baseline_breakdown(s: script.Script, head: int = CODEX_HEAD, output: int = OUTPUT_ALLOWANCE,
                       retries_per_turn: int = 1) -> dict[str, int]:
    """The session baseline's worst case (``estimate``) split into its terms: every request's input
    (head plus history), the output allowance, and the retry allowance that repeats each request."""
    history, inputs, requests = head, 0, 0
    for size in s.content_bytes:
        history += turn_tokens(size)
        inputs, requests, history = inputs + history, requests + 1, history + REPLY_TOKENS
    for size in s.probe_bytes:
        history += turn_tokens(size)
        inputs, requests, history = inputs + history, requests + 1, history + PROBE_ANSWER_TOKENS
    first = inputs + requests * output
    return {"requests": requests, "head_per_request": head, "head_total": head * requests,
            "history_total": inputs - head * requests, "input_total": inputs, "output_total": requests * output,
            "once": first, "retries": retries_per_turn * first, "total": (1 + retries_per_turn) * first,
            "retry_requests": retries_per_turn * requests}


def replay_baseline(s: script.Script, head: int = CODEX_HEAD, output: int = OUTPUT_ALLOWANCE,
                    retries_per_turn: int = 1) -> dict[str, int | bool]:
    """A cheaper baseline, built as Claude's (run.py replay): only the 6 probe requests, each sent once
    to the upstream with the whole history before it (from Codex's rollout), tools off. Every request
    must fit the model's window with its output allowance."""
    history = head + sum(turn_tokens(x) + REPLY_TOKENS for x in s.content_bytes)
    inputs = []
    for size in s.probe_bytes:
        history += turn_tokens(size)
        inputs.append(history)
        history += PROBE_ANSWER_TOKENS
    once = sum(x + output for x in inputs)
    return {"requests": len(inputs) * (1 + retries_per_turn), "largest_input": max(inputs),
            "fits": max(inputs) + output <= CODEX_WINDOW, "total": (1 + retries_per_turn) * once}


def history_turns(bound: int, turn_bytes: int, seed: int = script.DEFAULT_SEED) -> script.Script:
    """D27's script: the fewest content turns whose history reaches 2.5x the bound (run.build_script)."""
    n = 4
    while True:
        s = script.build(seed, n, turn_bytes)
        if sum(turn_tokens(x) + REPLY_TOKENS for x in s.content_bytes) >= 2.5 * bound:
            return s
        n += 1


def staged(s: script.Script, bound: int, margin: float = 0.10) -> dict[str, int | bool]:
    """Intake's staged design: step 1 is the H1 arm alone (acceptance 1 and 3), step 2 the baseline replay
    and the fidelity score (acceptance 2), run only if step 1 passes. Each step's worst case carries the
    margin, and each is its own stop cap."""
    b = estimate(s, bound, margin=margin)
    replay = replay_baseline(s)
    step1 = math.ceil(b.h1_worst * (1 + margin))
    step2 = math.ceil(replay["total"] * (1 + margin))
    return {"bound": bound, "content_turns": s.n_content, "step1_h1": step1, "step2_baseline_replay": step2,
            "total": step1 + step2, "replay_fits": replay["fits"], "replay_largest_input": replay["largest_input"],
            "session_baseline": b.baseline_worst, "session_baseline_fits": b.baseline_fits}


def main() -> None:
    """The figures the plan cites: the shipped default bound (150,000, launcher.DEFAULT_BOUND) at D27's 40 KB
    turns, and 64,000 at 20 KB and 40 KB turns."""
    for bound, kb in ((150_000, 40), (64_000, 20), (64_000, 40)):
        s = history_turns(bound, kb * 1000)
        print(json.dumps({"turn_kbytes": kb, **staged(s, bound)}))
    s = history_turns(64_000, 20_000)
    print(json.dumps({"baseline_breakdown_64000_20kb": baseline_breakdown(s)}))


if __name__ == "__main__":
    main()
