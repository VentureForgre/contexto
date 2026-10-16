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
HEADROOM_FLOOR = 25_000


# The contract's default (PR #116, a6bafa6), used only until lane A's engine Config carries the field.
CONTRACT_MIN_TURN_ROOM = 12_000


def engine_default(name: str, fallback: int) -> int:
    """A rewrite-engine Config default, so the simulation follows the engine, not a copy."""
    try:
        from contexto.rewrite import Config
    except ImportError:
        return fallback
    value = getattr(Config(), name, None)
    return int(value) if isinstance(value, int) else fallback


@dataclass(frozen=True)
class Params:
    bound: int = 150_000  # intake 00:40Z: the shipped default; history must reach 2.5x (375,000)
    head: int = 47_600  # the engine's fixed overhead F in the stub dry run through the launcher (M0 figure: 54,500)
    window_fraction: float = 0.5  # rule 4: retained window targets this share of W
    reply_tokens: int = 20  # "OK" plus a little thinking per build turn
    preamble_base: int = 300  # the preamble's fixed lines
    index_tokens_per_page: int = 60
    pinned_tokens: int = 1_500  # what the model pins over the run [inferred]
    probe_output: int = 4_000  # answers plus thinking on the probe turn [measured: 4,169 in L2f pilot 3]
    recalls: int = 2  # contexto_recall round trips on the probe turn: the script's cap (intake, 2026-10-16)
    # Tokens one recall adds. None: the largest chunk B9 (PR #56) can return, RECALL_LIMIT characters of
    # log text at 1.8 bytes per token. B9 returns less when W / 4 is smaller, so this errs high.
    recall_tokens: int | None = None
    max_tokens_reserve: int = 32_000  # the guard reserves this much output per request
    # A12 (contract PR #115): a turn also rotates when the recall room it would publish,
    # bound - estimate - headroom floor, would be under this. The engine's own default, when it has one.
    min_turn_room: int = field(default_factory=lambda: engine_default("min_turn_room", CONTRACT_MIN_TURN_ROOM))


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
    recall_tokens: int = 0  # tokens each recall is priced at
    b9_recall_tokens: int = 0  # what B9 would return at the simulated W (informational)
    probe_rooms: list[int] = field(default_factory=list)  # A9 turn room at each probe turn's start
    baseline_requests: list[int] = field(default_factory=list)  # D27 baseline: input of each request
    room_rotations: int = 0  # rotations fired by A12's room condition (contract PR #115)
    turn_room_low_epochs: list[int] = field(default_factory=list)  # epochs where A12 logged turn_room_low


def recall_tokens(p: Params) -> int:
    """Tokens each ``contexto_recall`` is priced at: the largest chunk B9 can return, read as log text."""
    return p.recall_tokens if p.recall_tokens is not None else math.ceil(RECALL_LIMIT / BYTES_PER_TOKEN_LOG)


def b9_recall_tokens(window_budget: int) -> int:
    """Tokens B9 returns per recall for window budget ``W``: ``W / 4`` at 1.5 characters per token (the
    session's measured ratio runs near 2 for this content), capped at RECALL_LIMIT, read as log text."""
    chars, _ = recall_limit(max(1, window_budget), 1.5)
    return math.ceil(chars / BYTES_PER_TOKEN_LOG)


def turn_tokens(turn_bytes: int) -> int:
    return int(turn_bytes / BYTES_PER_TOKEN_LOG) + 1


def simulate(turn_bytes: list[int], probe_bytes: int | list[int], p: Params = Params()) -> Budget:
    """Simulate the proxy per contract rule 4 (window budget after fixed overhead) and rule 5.

    ``probe_bytes`` is one probe turn's size, or (D27) a list of probe turns asked in order. Each
    probe turn starts like any turn (rotation rules apply), A9's turn room is set from its first
    request (``max(0, bound - (input + output) - headroom floor)``), and its recalls, each priced at
    ``recall_tokens(p)``, stop at that room. Its answers and recalled evidence stay in the history.
    """
    b = Budget()
    sizes = [turn_tokens(x) for x in turn_bytes]
    probes = [int(x / BYTES_PER_TOKEN_TEXT) + 1 for x in (probe_bytes if isinstance(probe_bytes, list) else [probe_bytes])]
    output_per_probe_turn = max(200, p.probe_output // len(probes))
    retained: list[int] = []  # retained complete turns (user text + reply), oldest first
    paged = 0
    epoch = 1
    max_growth = 0
    b.recall_tokens = recall_tokens(p)

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
        estimate = fixed + sum(retained) + u
        if retained and estimate > p.bound - headroom:  # rule 4's bound trigger
            if window_budget <= 0 or window_budget < retained[-1] + u:  # the engine's check: newest complete + new
                b.bound_too_small += 1
                note = "bound_too_small: sent unrotated"
            else:
                target = int(p.window_fraction * window_budget)
                before = paged
                while len(retained) > 1 and sum(retained) + u > target:
                    retained.pop(0)
                    paged += 1
                if paged > before:
                    epoch += 1
                    b.rotations += 1
                    note = "rotation"
                    preamble = p.preamble_base + p.pinned_tokens + paged * p.index_tokens_per_page
                else:
                    note = "rotation no-op (newest complete turn already alone)"
        elif retained and p.bound - estimate - HEADROOM_FLOOR < p.min_turn_room:
            # A12 (contract PR #115): the room this turn would publish is under min_turn_room. Rotate to
            # 0.5 * (W - min_turn_room), falling back to the minimal window (the new turn alone), and
            # only if that restores a room of at least min_turn_room; otherwise log turn_room_low
            # once per epoch and do not rotate.
            target = int(p.window_fraction * (window_budget - p.min_turn_room))
            keep = list(retained)
            while keep and sum(keep) + u > target:
                keep.pop(0)
            dropped = len(retained) - len(keep)
            new_preamble = p.preamble_base + p.pinned_tokens + (paged + dropped) * p.index_tokens_per_page
            restored = p.bound - (p.head + new_preamble + sum(keep) + u) - HEADROOM_FLOOR
            if dropped and restored >= p.min_turn_room:
                retained[:] = keep
                paged += dropped
                epoch += 1
                b.rotations += 1
                b.room_rotations += 1
                preamble = new_preamble
                note = "rotation (A12 room)"
            elif epoch not in b.turn_room_low_epochs:
                b.turn_room_low_epochs.append(epoch)
                note = "turn_room_low: rotation would not restore the room"
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
        recalls = 0
        while recalls < p.recalls and added + b.recall_tokens <= room:
            recalls += 1
            added += b.recall_tokens
            last = recalls == p.recalls or added + b.recall_tokens > room
            r_inp = inp + added
            b.rows.append(Row(len(sizes) + k, "recall", r_inp, len(retained), paged, epoch, r_inp > p.bound,
                              output_per_probe_turn if last else 200, "tool loop: never rotates"))
        if recalls == 0:
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
