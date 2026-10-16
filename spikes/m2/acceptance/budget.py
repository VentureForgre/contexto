"""Dry-run budget for the acceptance run (H1) and its full-context baseline. No model calls.

H1 is simulated with the contract's rotation rule (M2 contract, rewrite rules 3 to 5): before each
new turn, if the estimated upstream input would exceed ``bound - headroom``, page out the oldest
turns until the retained turns fit ``window_target``, always keeping the newest complete turn and
the new one. ``headroom`` is the running max of per-turn growth plus 25%, floor 25,000.

Token estimates use the contract's conservative estimator, 2.0 bytes per token for tool output
and 3.5 for other text, except that this script's pasted log text counts at 2.0 too: it is numeric
log content, measured near 0.55 tokens per character in M0 (E1, E2, L2f), which 3.5 would
undercount by about half.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BYTES_PER_TOKEN_LOG = 2.0
BYTES_PER_TOKEN_TEXT = 3.5
HEADROOM_FLOOR = 25_000


@dataclass(frozen=True)
class Params:
    bound: int = 100_000
    head: int = 45_925  # a default Claude Code throwaway's fixed head on the rig route (lane 1, relayed 2026-10-15)
    window_target_ratio: float = 0.5
    reply_tokens: int = 20  # "OK" plus a little thinking per build turn
    preamble_base: int = 300  # the preamble's fixed lines
    index_tokens_per_page: int = 60
    pinned_tokens: int = 1_500  # what the model pins over the run [inferred]
    probe_output: int = 4_000  # answers plus thinking on the probe turn [measured: 4,169 in L2f pilot 3]
    recalls: int = 6  # contexto_recall round trips on the probe turn [inferred]
    recall_tokens: int = 8_000  # tokens a recall result adds, up to 32,000 characters
    max_tokens_reserve: int = 32_000  # the guard reserves this much output per request


@dataclass
class Row:
    turn: int
    kind: str
    input_tokens: int
    retained_turns: int
    paged_turns: int
    epoch: int
    over_bound: bool
    output_expected: int
    note: str = ""


@dataclass
class Budget:
    rows: list[Row] = field(default_factory=list)
    history_tokens: int = 0  # local history (messages) at the probe turn
    h1_expected: int = 0
    h1_worst: int = 0
    baseline_replay: int = 0
    baseline_replay_worst: int = 0
    baseline_cli: int = 0
    rotations: int = 0
    planned_over_bound: int = 0


def turn_tokens(turn_bytes: int) -> int:
    return int(turn_bytes / BYTES_PER_TOKEN_LOG) + 1


def simulate(turn_bytes: list[int], probe_bytes: int, p: Params = Params()) -> Budget:
    b = Budget()
    sizes = [turn_tokens(x) for x in turn_bytes]
    window_target = int(p.bound * p.window_target_ratio)
    retained: list[int] = []  # sizes of retained turns (user text + reply)
    paged = 0
    epoch = 1
    max_growth = 0
    for t, u in enumerate(sizes, start=1):
        max_growth = max(max_growth, u + p.reply_tokens)
        headroom = max(HEADROOM_FLOOR, int(1.25 * max_growth))
        preamble = 0 if paged == 0 else p.preamble_base + p.pinned_tokens + paged * p.index_tokens_per_page
        estimate = p.head + preamble + sum(retained) + u
        note = ""
        if retained and estimate > p.bound - headroom:
            # Rotate: smallest k such that the retained turns plus the new one fit window_target,
            # keeping at least the newest complete turn.
            # Never page the newest complete turn (contract rule 4).
            while len(retained) > 1 and sum(retained) + u > window_target:
                retained.pop(0)
                paged += 1
            epoch += 1
            b.rotations += 1
            note = "rotation"
            preamble = p.preamble_base + p.pinned_tokens + paged * p.index_tokens_per_page
        inp = p.head + preamble + sum(retained) + u
        over = inp > p.bound
        b.rows.append(Row(t, "turn", inp, len(retained), paged, epoch, over, p.reply_tokens, note))
        b.planned_over_bound += int(over)
        retained.append(u + p.reply_tokens)
    b.history_tokens = sum(sizes) + len(sizes) * p.reply_tokens
    probe = int(probe_bytes / BYTES_PER_TOKEN_TEXT) + 1
    preamble = p.preamble_base + p.pinned_tokens + paged * p.index_tokens_per_page
    base = p.head + preamble + sum(retained) + probe
    for r in range(p.recalls + 1):  # the probe request, then one request per recall round trip
        inp = base + r * p.recall_tokens
        b.rows.append(Row(len(sizes) + 1, "probe" if r == 0 else "recall", inp, len(retained), paged, epoch,
                          inp > p.bound, p.probe_output if r == p.recalls else 200, "tool loop: never rotates" if r else ""))
    b.h1_expected = sum(r.input_tokens + r.output_expected for r in b.rows)
    b.h1_worst = sum(r.input_tokens + p.max_tokens_reserve for r in b.rows)
    full = p.head + b.history_tokens + probe
    b.baseline_replay = full + p.probe_output
    b.baseline_replay_worst = full + p.max_tokens_reserve
    b.baseline_cli = sum(p.head + sum(sizes[:t]) + t * p.reply_tokens for t in range(1, len(sizes) + 1)) + full + p.probe_output
    return b


def table(b: Budget, p: Params) -> str:
    out = ["| turn | kind | upstream input | retained turns | paged turns | epoch | over bound | note |",
           "|---|---|---|---|---|---|---|---|"]
    for r in b.rows:
        out.append(f"| {r.turn} | {r.kind} | {r.input_tokens:,} | {r.retained_turns} | {r.paged_turns} | {r.epoch} | "
                   f"{'yes' if r.over_bound else 'no'} | {r.note} |")
    out += [
        "",
        f"Bound {p.bound:,}; head {p.head:,}; local history at the probe turn {b.history_tokens:,} tokens "
        f"({b.history_tokens / p.bound:.2f}x the bound); rotations {b.rotations}; planned over-bound requests {b.planned_over_bound}.",
        "",
        "| Line | Expected tokens | Worst case (guard reserve) |",
        "|---|---|---|",
        f"| H1 acceptance run through the proxy | {b.h1_expected:,} | {b.h1_worst:,} |",
        f"| Full-context baseline, one direct replay request (recommended) | {b.baseline_replay:,} | {b.baseline_replay_worst:,} |",
        f"| Full-context baseline through a plain CLI session (alternative) | {b.baseline_cli:,} | - |",
        f"| **Total, H1 + replay baseline** | **{b.h1_expected + b.baseline_replay:,}** | **{b.h1_worst + b.baseline_replay_worst:,}** |",
    ]
    return "\n".join(out)


def sweep(probe_bytes: int, p: Params, turn_kbytes: tuple[int, ...] = (20, 26, 30, 34, 38, 44)) -> list[dict[str, Any]]:
    """Smallest content-turn count reaching 2.5x the bound, per turn size; reports cost and over-bound."""
    rows = []
    for kb in turn_kbytes:
        size = kb * 1_000
        per_turn = turn_tokens(size) + p.reply_tokens
        n = -(-int(2.5 * p.bound) // per_turn)
        b = simulate([size] * n, probe_bytes, p)
        rows.append({"turn_kbytes": kb, "turns": n, "h1_expected": b.h1_expected, "baseline_replay": b.baseline_replay,
                     "total": b.h1_expected + b.baseline_replay, "planned_over_bound": b.planned_over_bound,
                     "rotations": b.rotations})
    return rows
