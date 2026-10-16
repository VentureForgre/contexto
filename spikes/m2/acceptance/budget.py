"""Dry-run budget for the acceptance run (H1) and its full-context baseline. No model calls.

H1 is simulated with the contract's rotation rules (M2 contract, rules 3 to 5): before each new
turn, if the estimated upstream input would exceed ``bound - headroom``, page out the oldest turns
until the retained turns fit half the window budget ``W = bound - headroom - F`` (``F`` = head plus
preamble), always keeping the newest complete turn and the new one; if ``W`` cannot hold those two
turns, the request goes unrotated (``bound_too_small``), as the merged engine does. ``headroom`` is the running max of
per-turn growth plus 25%, floor 25,000.

Token estimates: this script's pasted log text counts at 1.8 bytes per token, the rate M0 measured
for numeric log content (about 0.55 tokens per character in E1, E2, and L2f). Before the first real
usage, the contract's estimator counts ``tool_result`` content at 1.5 bytes per token, which errs
high for this content, and other text at 3.5, which undercounts it by about half (contract PR #44).
The pasted logs are user text, so the run keeps them as such only under the 10 percent rule in the
harness doc (option (a)). Token counting replaces the estimate where available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from contexto.store.render import RECALL_LIMIT, recall_limit

BYTES_PER_TOKEN_LOG = 1.8
BYTES_PER_TOKEN_TEXT = 3.5
OTHER_BYTES_PER_TOKEN = 3.0  # every non-log byte, as the stub dry run models it (stubrun.OTHER_BYTES_PER_TOKEN)

# The head (Params.head), derived (intake 2026-10-16 09:42Z). 47,600 is the engine's fixed overhead F
# measured in the stub dry runs through the launcher (dry runs #1 to #4, main up to c8f0b08): system,
# tools and preamble. The part of it this repo controls is Contexto's own tool definitions in Claude
# Code's request form; when they change, the head moves by their byte change at OTHER_BYTES_PER_TOKEN.
# B19 and B21 (main 8571095, dry run #5) grew them from 2,743 to 2,827 bytes: 47,600 + ceil(84 / 3.0) =
# 47,628. test_head_follows_contexto_tool_bytes fails when the definitions change without this.
HEAD_MEASURED = 47_600
CONTEXTO_TOOL_BYTES_AT_MEASURE = 2_743  # main c8f0b08
CONTEXTO_TOOL_BYTES = 2_827  # main 8571095
HEAD = HEAD_MEASURED + math.ceil((CONTEXTO_TOOL_BYTES - CONTEXTO_TOOL_BYTES_AT_MEASURE) / OTHER_BYTES_PER_TOKEN)

# Tool-loop rounds, calibrated on live run #3 (main fbb1303, 2026-10-16; intake 10:06Z). Its probe
# turns took 5/3/2/5/6/5 Messages requests: 4/2/1/4/5/4 tool-loop rounds after the first request, 20
# in all, for 78 contexto_search calls. The input grew by these tokens per round (the turn's last input
# minus its first, over its rounds). Params prices the worst case at the most rounds and the largest
# growth, rounded up to 100, and the expected case at the mean rounds and the rounds-weighted mean
# growth, rounded up to 100. test_tool_rounds_are_calibrated_on_live_run_3 pins both.
TOOL_ROUNDS_RUN3 = (4, 2, 1, 4, 5, 4)
ROUND_GROWTH_RUN3 = (1_534, 2_771, 8_164, 1_676, 1_302, 2_206)
H1_SPENT_RUN3 = 3_748_084
HEADROOM_FLOOR = 25_000


def engine_default(name: str) -> int:
    """A rewrite-engine Config default, so the simulation follows the engine, not a copy (A12 is on
    main; there is no fallback: a missing field is an error the guard test catches)."""
    from contexto.rewrite import Config

    return int(getattr(Config(), name))


@dataclass(frozen=True)
class Params:
    bound: int = 150_000  # intake 00:40Z: the shipped default; history must reach 2.5x (375,000)
    head: int = HEAD  # the engine's fixed overhead F, derived above (M0 figure: 54,500)
    window_fraction: float = 0.5  # rule 4: retained window targets this share of W
    reply_tokens: int = 20  # "OK" plus a little thinking per build turn
    preamble_base: int = 300  # the preamble's fixed lines
    index_tokens_per_page: int = 60
    pinned_tokens: int = 1_500  # what the model pins over the run [inferred]
    probe_output: int = 4_000  # answers plus thinking on the probe turn [measured: 4,169 in L2f pilot 3]
    # Tool-loop rounds per probe turn and what each adds to the input (calibrated on live run #3,
    # intake 2026-10-16 10:06Z; see TOOL_ROUNDS_RUN3 below). Each round re-sends the whole window, so the
    # round count drives the cost. The worst case prices the observed maximum, the expected case the
    # observed mean; neither is limited by the turn room, since run #3's rounds were not.
    tool_rounds: int = 5  # worst: the most rounds any probe turn took in run #3
    round_tokens: int = 8_200  # worst: the largest per-round input growth in run #3 (8,164), rounded up
    tool_rounds_expected: float = 20 / 6  # expected: run #3's 20 rounds over 6 probe turns
    round_tokens_expected: int = 2_100  # expected: run #3's rounds-weighted mean growth (2,094), rounded up
    max_tokens_reserve: int = 32_000  # the guard reserves this much output per request
    # A12 (contract PR #115): a turn also rotates when the recall room it would publish,
    # bound - estimate - headroom floor, would be under this. The engine's own default, when it has one.
    min_turn_room: int = field(default_factory=lambda: engine_default("min_turn_room"))


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
    bound_too_small: int = 0
    window_budget_first: int = 0
    window_budget_probe: int = 0  # W at the probe turn, as simulated
    recall_tokens: int = 0  # tokens each worst-case tool-loop round adds (Params.round_tokens)
    b9_recall_tokens: int = 0  # what B9 would return at the simulated W (informational)
    expected_tool_rounds: int = 0  # tool-loop rounds the expected pass priced over all probe turns
    probe_rooms: list[int] = field(default_factory=list)  # A9 turn room at each probe turn's start
    baseline_requests: list[int] = field(default_factory=list)  # D27 baseline: input of each request
    room_rotations: int = 0  # rotations fired by A12's room condition (contract PR #115)
    window_minimal: int = 0  # rule 4 rotations to the minimal window (contract PR #50)
    turn_room_low_epochs: list[int] = field(default_factory=list)  # epochs where A12 logged turn_room_low


def recall_tokens(p: Params) -> int:
    """The worst-case input growth of one tool-loop round."""
    return p.round_tokens


def b9_largest_chunk_tokens() -> int:
    """The largest chunk B9 can return in one recall, read as log text (informational since run #3)."""
    return math.ceil(RECALL_LIMIT / BYTES_PER_TOKEN_LOG)


def b9_recall_tokens(window_budget: int) -> int:
    """Tokens B9 returns per recall for window budget ``W``: ``W / 4`` at 1.5 characters per token (the
    session's measured ratio runs near 2 for this content), capped at RECALL_LIMIT, read as log text."""
    chars, _ = recall_limit(max(1, window_budget), 1.5)
    return math.ceil(chars / BYTES_PER_TOKEN_LOG)


def turn_tokens(turn_bytes: int) -> int:
    return int(turn_bytes / BYTES_PER_TOKEN_LOG) + 1


def expected_rounds(p: Params, probe_turns: int) -> list[int]:
    """The expected rounds per probe turn: round(mean x turns) in total, spread as evenly as possible,
    the extra rounds on the earliest turns."""
    total = round(p.tool_rounds_expected * probe_turns)
    base, extra = divmod(total, probe_turns)
    return [base + (1 if k < extra else 0) for k in range(probe_turns)]


def simulate(turn_bytes: list[int], probe_bytes: int | list[int], p: Params = Params()) -> Budget:
    """The worst-case simulation (``_simulate`` at ``tool_rounds`` rounds of ``round_tokens``), with
    ``h1_expected`` from a second pass at the expected rounds and growth (live run #3 calibration)."""
    probes = probe_bytes if isinstance(probe_bytes, list) else [probe_bytes]
    b = _simulate(turn_bytes, probe_bytes, p, [p.tool_rounds] * len(probes), p.round_tokens)
    e = _simulate(turn_bytes, probe_bytes, p, expected_rounds(p, len(probes)), p.round_tokens_expected)
    b.h1_expected = e.h1_expected
    b.expected_tool_rounds = sum(expected_rounds(p, len(probes)))
    return b


def _simulate(turn_bytes: list[int], probe_bytes: int | list[int], p: Params, rounds: list[int],
              round_tokens: int) -> Budget:
    """Simulate the proxy per contract rule 4 (window budget after fixed overhead) and rule 5.

    ``probe_bytes`` is one probe turn's size, or (D27) a list of probe turns asked in order. Each
    probe turn starts like any turn (rotation rules apply), A9's turn room is set from its first
    request (``max(0, bound - (input + output) - headroom floor)``). Its tool loop then runs
    ``rounds[k]`` rounds, each adding ``round_tokens`` to the input; the loop never rotates. Its answers
    and the tool output stay in the history.
    """
    b = Budget()
    sizes = [turn_tokens(x) for x in turn_bytes]
    probes = [int(x / BYTES_PER_TOKEN_TEXT) + 1 for x in (probe_bytes if isinstance(probe_bytes, list) else [probe_bytes])]
    output_per_probe_turn = max(200, p.probe_output // len(probes))
    retained: list[int] = []  # retained complete turns (user text + reply), oldest first
    paged = 0
    epoch = 1
    max_growth = 0
    b.recall_tokens = round_tokens

    def start_turn(t: int, u: int, kind: str) -> tuple[int, int, int]:
        """Apply the turn-start rotation rules for a new turn of ``u`` tokens; return its first input,
        the preamble, and the window budget."""
        nonlocal paged, epoch, max_growth
        max_growth = max(max_growth, u + p.reply_tokens)
        headroom = max(HEADROOM_FLOOR, int(1.25 * max_growth))
        preamble = 0 if paged == 0 else p.preamble_base + p.pinned_tokens + paged * p.index_tokens_per_page
        fixed = p.head + preamble  # rule 4: F = system + tools + carried + preamble
        window_budget = p.bound - headroom - fixed
        note = ""
        # The engine's order (contexto.rewrite.rotate_at_turn_start, A12; the contract follows it,
        # intake 9ed1b32): the size trigger and A12's room condition are both read from the estimate.
        estimate = fixed + sum(retained) + u
        trigger = estimate > p.bound - headroom
        room_low = p.bound - estimate - HEADROOM_FLOOR < p.min_turn_room
        epoch_before = epoch
        room_keep: list[int] | None = None
        restores = False
        if room_low and retained:
            room_target = int(p.window_fraction * (window_budget - p.min_turn_room))
            if room_target > 0 and retained[-1] + u <= room_target:
                room_keep = list(retained)  # choose_k: page oldest first, keep the newest complete turn
                while len(room_keep) > 1 and sum(room_keep) + u > room_target:
                    room_keep.pop(0)
            else:
                room_keep = []  # the minimal window: only the new turn
            room = p.bound - HEADROOM_FLOOR - fixed - (sum(room_keep) + u)
            restores = len(room_keep) < len(retained) and room >= p.min_turn_room

        def page_to(keep: list[int], label: str) -> None:
            nonlocal paged, epoch, preamble
            paged += len(retained) - len(keep)
            retained[:] = keep
            epoch += 1
            b.rotations += 1
            preamble = p.preamble_base + p.pinned_tokens + paged * p.index_tokens_per_page

        if retained and room_low and not trigger:
            if restores:  # a rotation for the room alone
                page_to(room_keep, "room")
                b.room_rotations += 1
                note = "rotation (A12 room)"
        elif retained and trigger:
            if window_budget <= 0 or retained[-1] + u > window_budget:
                # Rule 4 window_minimal (contract PR #50): keep only the new turn; bound_too_small
                # as well if even the new turn exceeds W.
                page_to([], "window_minimal")
                b.window_minimal += 1
                note = "window_minimal"
                if window_budget <= 0 or u > window_budget:
                    b.bound_too_small += 1
                    note = "window_minimal + bound_too_small"
            else:
                target = int(p.window_fraction * window_budget)
                keep = list(retained)
                while len(keep) > 1 and sum(keep) + u > target:
                    keep.pop(0)
                if restores and room_keep is not None and len(room_keep) < len(keep):
                    keep = room_keep  # both conditions: the room-sized target (no back-to-back rotations)
                    b.room_rotations += 1
                    note = "rotation (bound + A12 room)"
                if len(keep) < len(retained):
                    page_to(keep, "bound")
                    note = note or "rotation"
                else:
                    note = "rotation no-op (newest complete turn already alone)"
        if room_low and epoch not in b.turn_room_low_epochs:
            # turn_room_low after ANY decision that leaves the room under min_turn_room, once per epoch.
            sent_room = (p.bound - estimate - HEADROOM_FLOOR if epoch == epoch_before
                         else p.bound - HEADROOM_FLOOR - fixed - (sum(retained) + u))
            if sent_room < p.min_turn_room:
                b.turn_room_low_epochs.append(epoch)
                note = (note + "; " if note else "") + "turn_room_low"
        inp = p.head + preamble + sum(retained) + u
        over = inp > p.bound
        b.rows.append(Row(t, kind, inp, len(retained), paged, epoch, over,
                          p.reply_tokens if kind == "turn" else 200, note))
        b.planned_over_bound += int(over)
        return inp, preamble, window_budget

    for t, u in enumerate(sizes, start=1):
        start_turn(t, u, "turn")
        retained.append(u + p.reply_tokens)
    b.history_tokens = sum(sizes) + len(sizes) * p.reply_tokens
    for k, u in enumerate(probes, start=1):
        inp, preamble, window_budget = start_turn(len(sizes) + k, u, "probe")
        if k == 1:
            b.window_budget_probe = window_budget
            b.b9_recall_tokens = b9_recall_tokens(window_budget)
        room = max(0, p.bound - (inp + 200) - HEADROOM_FLOOR)  # A9, from the turn's first request
        b.probe_rooms.append(room)
        added = 0
        n = rounds[k - 1]
        for r in range(1, n + 1):
            added += round_tokens
            r_inp = inp + added
            b.rows.append(Row(len(sizes) + k, "round", r_inp, len(retained), paged, epoch, r_inp > p.bound,
                              output_per_probe_turn if r == n else 200, "tool loop: never rotates"))
        if n == 0:
            b.rows[-1].output_expected = output_per_probe_turn
        retained.append(u + added + output_per_probe_turn)
    b.h1_expected = sum(r.input_tokens + r.output_expected for r in b.rows)
    b.h1_worst = sum(r.input_tokens + p.max_tokens_reserve for r in b.rows)
    # D27's symmetric baseline (intake 05:42Z): one full-context request per probe turn, request k
    # carrying probe turns 1..k-1 with the baseline's own answers. Every input token counts toward
    # the approval whether cached or not: caching lowers the price, not the token count.
    b.baseline_requests = []
    for k in range(1, len(probes) + 1):
        b.baseline_requests.append(p.head + b.history_tokens + sum(probes[:k]) + (k - 1) * output_per_probe_turn)
    b.baseline_replay = sum(b.baseline_requests) + len(probes) * output_per_probe_turn
    b.baseline_replay_worst = sum(x + p.max_tokens_reserve for x in b.baseline_requests)
    full = p.head + b.history_tokens + sum(probes)
    b.baseline_cli = sum(p.head + sum(sizes[:t]) + t * p.reply_tokens for t in range(1, len(sizes) + 1)) + full + p.probe_output
    b.window_budget_first = p.bound - HEADROOM_FLOOR - p.head
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
        f"({b.history_tokens / p.bound:.2f}x the bound); rotations {b.rotations}; planned over-bound requests "
        f"{b.planned_over_bound}; bound_too_small {b.bound_too_small}; window budget W at the floor headroom "
        f"{b.window_budget_first:,} (the launcher warns below 25,000).",
        "",
        approval_table(b),
    ]
    return "\n".join(out)


def turn_estimates(b: Budget) -> list[int]:
    """One spend estimate per turn the driver sends, in order: the largest request of each turn, a
    probe turn's recall round trips included. driver.Run checks estimates[i] before sending turn i,
    so D27's six probe turns need six entries, not one."""
    largest: dict[int, int] = {}
    for r in b.rows:
        largest[r.turn] = max(largest.get(r.turn, 0), r.input_tokens)
    return [largest[t] for t in sorted(largest)]


def approval_table(b: Budget) -> str:
    """The approval lines. ``run.py live`` gates on the combined worst case (H1 plus the D27 baseline)
    from the same ``Budget``, and the harness doc's table is this function's output (a test keeps
    them equal)."""
    return "\n".join([
        "| Line | Expected tokens | Worst case (guard reserve) |",
        "|---|---|---|",
        f"| H1 acceptance run through the proxy, {sum(r.kind == 'probe' for r in b.rows)} probe turns (D27) | {b.h1_expected:,} | {b.h1_worst:,} |",
        f"| Full-context baseline, {len(b.baseline_requests)} direct requests, one per probe turn (D27) | {b.baseline_replay:,} | {b.baseline_replay_worst:,} |",
        f"| Full-context baseline through a plain CLI session (alternative) | {b.baseline_cli:,} | - |",
        f"| **Total, H1 + baseline (live refuses an approval below the worst case)** | **{b.h1_expected + b.baseline_replay:,}** | **{b.h1_worst + b.baseline_replay_worst:,}** |",
    ])


def sweep(probe_bytes: int | list[int], p: Params,
          turn_kbytes: tuple[int, ...] = (20, 26, 30, 34, 38, 44)) -> list[dict[str, Any]]:
    """Smallest content-turn count reaching 2.5x the bound, per turn size; reports cost and over-bound."""
    rows = []
    for kb in turn_kbytes:
        size = kb * 1_000
        per_turn = turn_tokens(size) + p.reply_tokens
        n = -(-int(2.5 * p.bound) // per_turn)
        b = simulate([size] * n, probe_bytes, p)
        rows.append({"turn_kbytes": kb, "turns": n, "h1_expected": b.h1_expected, "baseline_replay": b.baseline_replay,
                     "total": b.h1_expected + b.baseline_replay, "planned_over_bound": b.planned_over_bound,
                     "rotations": b.rotations, "bound_too_small": b.bound_too_small})
    return rows
