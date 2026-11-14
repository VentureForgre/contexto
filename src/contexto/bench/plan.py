"""The token plan of a live benchmark run: what ``--dry-run`` prints and what the live run gates on.

No calls are made. For each arm and script the plan simulates the requests the arm is expected to
send and the most it could plausibly send, and prices each in tokens by class. The live run refuses
to start when ``--budget-tokens`` is below the plan's worst case for the units it still has to run.

The model, and where each figure comes from. "Run #3" is docs/research/M2-usage-savings-run3.md and
"run #4" is docs/research/M2-acceptance-run4.md: the two recorded live runs of the 54-probe session
through Contexto on claude-haiku-5-5 at bound 150,000, each with a six-request full-context baseline.
No plain Claude Code run and no run of the v2 turns has been recorded, so every figure for those is
marked [assumed] and should be replaced by the first live run's measurements.

Sizes. Pasted log text and file text count 1.8 bytes per token and other text 3.5, the rates
``contexto.bench.script.estimate`` uses (run #3 fitted 1.8274 bytes per token on its own requests).
The worst case adds ``ESTIMATE_MARGIN`` to every input.

``contexto`` arm. A port of the acceptance harness's budget simulation (spikes/m2/acceptance/budget.py):
the engine's turn-start rotation rules over the script's turns, with the head, preamble and headroom
it uses. Tool rounds, each re-sending the window: a probe turn takes ``PROBE_ROUNDS_EXPECTED`` rounds
of ``ROUND_TOKENS_EXPECTED`` (run #3: 20 rounds over 6 probe turns, 2,094 tokens a round; run #4: 19
rounds) and at worst ``PROBE_ROUNDS_WORST`` of ``ROUND_TOKENS_WORST`` (the most and the largest in
run #3); a content turn takes one request in 9 of 10 cases (run #4: 20 requests for 18 content turns)
and at worst three (the Opus run in run #4's comparison table: 44 requests for 18 content turns); the
tool-loop turn takes one round per file, each adding that file to the input, and the loop never
rotates [assumed: no recorded run; one round per half file at worst].

``plain`` arm [assumed, except where a stub run is cited]. Claude Code resends its whole history
each request. Before a request it compares the last response's usage, plus its own estimate of the
text added since, with the trigger (the bound); at or over it, a summary request reads the whole
context and the history restarts at the head, the summary and the files Claude Code re-attaches. Two
things were seen with Claude Code 2.1.322 against a stub upstream that reports modelled usage (the
full-size v2 script, bound 150,000, 2026-11-12), and the expected case uses them: Claude Code sizes
text it has not sent yet at about 3 bytes per token, where this log text is 1.8 (``PENDING_FACTOR``),
so a session passes the bound by up to a turn before it compacts (158,168 input tokens at the
largest); and after each of four compactions the next request was about 25,000 tokens larger than
the head and the new turn (``RESTORED_TOKENS_EXPECTED``, for a script with files). A probe turn takes
one request. The worst case takes the Contexto arm's worst rounds, two summary requests per
compaction (the stub run had two; the stub's summary is not in the form Claude Code asks for, so the
second may be a retry) and 70,000 tokens of summary and re-attached files.

``full`` arm. One request per probe turn carrying the whole script: the history estimate, the probe
turns so far and this arm's earlier answers. Run #4's baseline measured 426,266 to 429,087 input
tokens over six requests, about 2,000 output tokens each.

Cache classes, expected case. A request reads what the previous request of the same prefix sent and
writes the rest; the first request after a rotation or a compaction writes everything (run #4: 38 of
45 requests read the cache, the other 7 were the first of each epoch; its baseline read the prefix
on 4 of 5 later requests). Uncached input is a few tokens a request (run #4: 90 over 45 requests; 27
a request in its baseline). The worst case counts every input token as a cache write, the dearest
class, and an output reservation: ``OUTPUT_RESERVE`` for a Claude Code request (what the acceptance
harness's guard reserved) and ``FULL_MAX_TOKENS`` for a full-context request.

The worst case is a planning figure, not a bound on what the API could bill: Claude Code 2.1.322
asks for ``max_tokens`` 128,000 on claude-haiku-5-5 (seen by a stub upstream), and a reply that long
on every request would cost more than this plan. The run still cannot pass its budget, because the
meter's guard reserves each request's real ``max_tokens`` while it is in flight and refuses the
request that could pass the budget (``contexto.bench.meter``); such a run stops early instead.

Models. The tool rounds are calibrated on claude-haiku-5-5. Where a recorded run of the same session
exists for another model, the expected tokens of the two Claude Code arms are scaled by the ratio of
that run's Contexto tokens to run #4's (``RECORDED``): claude-opus-5-5 spent 4,726,388 tokens on the
Contexto arm (docs/research/M2-acceptance-opus.md, 62 requests: it called tools on content turns)
against 3,422,988 on claude-haiku-5-5 in run #4, a factor of 1.381. The full arm's figures come from
the script alone and are not scaled; neither is the worst case. For any other model the plan is the
claude-haiku-5-5 plan, and ``calibration`` says so.

Heads. Claude Code 2.1.322 with a fresh configuration sent a first request of 51,270 bytes in the
plain arm and 52,325 under ``contexto run`` (stub upstream, 2026-11-12; Contexto's tools are deferred
behind tool search, so they add about 1,000 bytes until the model loads them). Real token counts of
that head were measured by the engine on 2.1.314 at 18,641 to 27,925 (D55); the plan uses the top of
that range for the Contexto arm and 1,000 tokens less for the plain arm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from contexto.bench import script as bench_script
from contexto.bench.model import Script

ESTIMATE_MARGIN = 1.10  # [assumed] how far the byte rates may undercount a request
BYTES_PER_TOKEN_TEXT = 3.5
HEADROOM_FLOOR = 25_000  # contexto.launcher.DEFAULT_HEADROOM_FLOOR
WINDOW_FRACTION = 0.5
OUTPUT_RESERVE = 32_000
FULL_MAX_TOKENS = 8_192
PROBE_ROUNDS_EXPECTED = 20 / 6  # run #3: 4/2/1/4/5/4 rounds over its six probe turns
PROBE_ROUNDS_WORST = 5
ROUND_TOKENS_EXPECTED = 2_100  # run #3's rounds-weighted mean growth, 2,094
ROUND_TOKENS_WORST = 8_200  # run #3's largest per-round growth, 8,164
CONTENT_EXTRA_EXPECTED = 2 / 18  # run #4: 2 extra requests over 18 content turns
CONTENT_ROUNDS_WORST = 2  # the Opus run: 44 requests for 18 content turns, 2.44 a turn
CONTENT_ROUND_TOKENS = 500  # [assumed] a pin call and its result
OUTPUT_PER_REQUEST = 400  # run #3: 22,693 output tokens over 44 requests; run #4: 13,802 over 45
FULL_OUTPUT_EXPECTED = 2_000  # run #4's baseline: 12,023 output tokens over 6 requests
SUMMARY_TOKENS_EXPECTED = 3_000  # [assumed] a compaction summary
RESTORED_TOKENS_EXPECTED = 25_000  # files re-attached after a compaction: about 24,900 in the stub run
PENDING_FACTOR = 0.6  # Claude Code's size for unsent log text (3 bytes per token) over its modelled size (1.8)
SUMMARY_TOKENS_WORST = 20_000  # Claude Code's summary output reserve (research: min(max output, 20,000))
RESTORED_TOKENS_WORST = 50_000  # [assumed] files Claude Code re-attaches after a compaction
COMPACTION_RESERVE = 33_000  # Claude Code compacts at its window minus this (read in the 2.1.322 bundle)
TOOL_CALL_TOKENS = 100  # [assumed] a tool call and the frame of its result
UNCACHED_PER_REQUEST = 2  # run #4: 90 over 45 requests
UNCACHED_PER_FULL_REQUEST = 27  # run #4's baseline
CLASSES = ("uncached", "cache_write", "cache_read", "output")
CALIBRATED_ON = "claude-haiku-5-5"
CALIBRATION_TOKENS = 3_422_988  # run #4's Contexto arm on claude-haiku-5-5 (docs/research/M2-acceptance-run4.md)
# model -> (the Contexto arm's tokens on the same 54-probe session, where it is recorded)
RECORDED = {"claude-opus-5-5": (4_726_388, "docs/research/M2-acceptance-opus.md")}
SCALED_ARMS = ("contexto", "plain")
# What --dry-run prints under its tables. The module docstring has the whole model.
NOTES = (
    "The model behind these figures (contexto.bench.plan states it in full):",
    "- Sizes: log and file text at 1.8 bytes per token, other text at 3.5, as the script's own estimate.",
    "- contexto: the engine's rotation rules over the script's turns. Tool rounds are calibrated on the two recorded "
    "live runs (docs/research/M2-usage-savings-run3.md and M2-acceptance-run4.md): a probe turn takes 3.3 rounds of "
    "2,100 tokens expected and 5 of 8,200 at worst; a content turn takes one request in 9 of 10 cases and three at "
    "worst; the tool-loop turn takes one round per file [assumed].",
    "- plain [assumed: no live plain run has been recorded; two figures come from Claude Code 2.1.322 against a "
    "stub upstream]: the whole history every request; when the last usage plus Claude Code's estimate of the new "
    "text reaches the trigger, one summary request and a restart from the head, a 3,000-token summary and 25,000 "
    "tokens of re-attached files; a probe turn takes one request. At worst: the Contexto arm's worst rounds, two "
    "summary requests per compaction, 70,000 tokens of summary and files.",
    "- full: one request per probe turn carrying the whole script (run #4's baseline: 426,266 to 429,087 input "
    "tokens a request).",
    "- Cache, expected case: a request reads what the previous request sent and writes the rest; the first request "
    "after a rotation or a compaction writes everything (run #4: 38 of 45 requests read the cache).",
)


@dataclass(frozen=True)
class Params:
    # Claude Code's head (system prompt, tools, session-start context); see "Heads" above.
    head_contexto: int = 28_000  # contexto.launcher.ASSUMED_OVERHEAD_DEFAULT, the top of D55's measured range
    head_plain: int = 27_000  # the same without Contexto's tool definitions and server instructions
    head_full: int = 400  # the full arm's one-line system prompt and one tool definition
    preamble_base: int = 380  # the pinned text's fixed lines (budget.fixed_preamble_tokens, D51)
    pinned_tokens: int = 1_500
    index_tokens_per_page: int = 60
    min_turn_room: int = 12_000  # contexto.rewrite.Config.min_turn_room; ``engine_params`` reads the real one


def engine_params() -> Params:
    """``Params`` with the rewrite engine's own ``min_turn_room``, so the simulation follows the engine."""
    from contexto.rewrite import Config

    return Params(min_turn_room=int(Config().min_turn_room))


@dataclass
class Request:
    input: int
    output: int  # expected output
    fresh: bool  # nothing of its prefix is cached: the first request, or the first after a rotation or compaction
    side: bool = False


@dataclass
class Estimate:
    """One arm's plan for one script, or a sum of such plans."""

    requests_expected: int = 0
    requests_worst: int = 0
    expected: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CLASSES, 0))
    worst: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CLASSES, 0))
    over_bound_expected: int = 0  # planned requests above the bound (Contexto arm)
    compactions_expected: int = 0  # plain arm

    @property
    def expected_total(self) -> int:
        return sum(self.expected.values())

    @property
    def worst_total(self) -> int:
        return sum(self.worst.values())

    def add(self, other: Estimate) -> None:
        self.requests_expected += other.requests_expected
        self.requests_worst += other.requests_worst
        for name in CLASSES:
            self.expected[name] += other.expected[name]
            self.worst[name] += other.worst[name]
        self.over_bound_expected += other.over_bound_expected
        self.compactions_expected += other.compactions_expected


def _text_tokens(size: int) -> int:
    return int(size / BYTES_PER_TOKEN_TEXT) + 1


def _spread(total: int, slots: int) -> list[int]:
    """``total`` rounds over ``slots`` turns, as evenly as possible, the extra ones first."""
    if slots <= 0:
        return []
    base, extra = divmod(total, slots)
    return [base + (1 if k < extra else 0) for k in range(slots)]


def turn_specs(script: Script, worst: bool, recall: bool = True) -> list[tuple[int, list[int], int]]:
    """Per turn: the user message's tokens, the input each tool round adds, and the last reply's
    expected output. Shared by the two Claude Code arms; ``recall`` is False for the arm with no
    recall tools, whose probe turns are expected to take one request."""
    probe_turns = [i for i, k in enumerate(script.kinds) if k == "probe"]
    content = [i for i, k in enumerate(script.kinds) if k != "probe"]
    expected_rounds = round(PROBE_ROUNDS_EXPECTED * len(probe_turns)) if recall else 0
    probe_rounds = ([PROBE_ROUNDS_WORST] * len(probe_turns) if worst
                    else _spread(expected_rounds, len(probe_turns)))
    extra = set(content[:round(CONTENT_EXTRA_EXPECTED * len(content))])
    specs = []
    for index, kind in enumerate(script.kinds):
        size = script.turn_bytes[index]
        if kind == "probe":
            growth = ROUND_TOKENS_WORST if worst else ROUND_TOKENS_EXPECTED
            rounds = [growth] * probe_rounds[probe_turns.index(index)]
            specs.append((_text_tokens(size), rounds, 600))
        elif kind == "toolloop":
            files = [bench_script.turn_tokens(len(script.files[path].encode("utf-8"))) + TOOL_CALL_TOKENS
                     for path in script.reads]
            if worst:  # each file read in two halves
                files = [part for tokens in files for part in (tokens // 2 + TOOL_CALL_TOKENS, tokens - tokens // 2)]
            specs.append((_text_tokens(size), files, bench_script.REPLY_TOKENS))
        else:
            tokens = bench_script.turn_tokens(size) if kind == "log" else _text_tokens(size)
            rounds = [CONTENT_ROUND_TOKENS] * (CONTENT_ROUNDS_WORST if worst else int(index in extra))
            specs.append((tokens, rounds, bench_script.REPLY_TOKENS))
    return specs


def _preamble(params: Params, paged: int) -> int:
    """The pinned text and page index the engine sends once turns have been paged out."""
    return 0 if paged == 0 else params.preamble_base + params.pinned_tokens + paged * params.index_tokens_per_page


def simulate_contexto(script: Script, bound: int, params: Params, worst: bool) -> list[Request]:
    """The requests of the Contexto arm: the engine's turn-start rotation rules (M2 contract, rules 3
    to 5 and A12), as spikes/m2/acceptance/budget.py simulates them. A tool loop never rotates."""
    requests: list[Request] = []
    retained: list[int] = []  # retained complete turns, oldest first
    paged = 0
    max_growth = 0
    for user, rounds, last_output in turn_specs(script, worst):
        max_growth = max(max_growth, user + bench_script.REPLY_TOKENS)
        headroom = max(HEADROOM_FLOOR, int(1.25 * max_growth))

        fixed = params.head_contexto + _preamble(params, paged)
        window_budget = bound - headroom - fixed
        estimate = fixed + sum(retained) + user
        trigger = estimate > bound - headroom
        room_low = bound - estimate - HEADROOM_FLOOR < params.min_turn_room
        room_keep: list[int] | None = None
        restores = False
        if room_low and retained:
            target = int(WINDOW_FRACTION * (window_budget - params.min_turn_room))
            if target > 0 and retained[-1] + user <= target:
                room_keep = list(retained)
                while len(room_keep) > 1 and sum(room_keep) + user > target:
                    room_keep.pop(0)
            else:
                room_keep = []
            room = bound - HEADROOM_FLOOR - fixed - (sum(room_keep) + user)
            restores = len(room_keep) < len(retained) and room >= params.min_turn_room
        keep: list[int] | None = None
        if retained and room_low and not trigger:
            if restores:
                keep = room_keep
        elif retained and trigger:
            if window_budget <= 0 or retained[-1] + user > window_budget:
                keep = []  # the minimal window: only the new turn
            else:
                target = int(WINDOW_FRACTION * window_budget)
                keep = list(retained)
                while len(keep) > 1 and sum(keep) + user > target:
                    keep.pop(0)
                if restores and room_keep is not None and len(room_keep) < len(keep):
                    keep = room_keep
        rotated = keep is not None and len(keep) < len(retained)
        if rotated:
            assert keep is not None
            paged += len(retained) - len(keep)
            retained[:] = keep
        current = params.head_contexto + _preamble(params, paged) + sum(retained) + user
        fresh = rotated or not requests
        added = 0
        for n in range(len(rounds) + 1):
            last = n == len(rounds)
            requests.append(Request(current + added, last_output if last else OUTPUT_PER_REQUEST // 2, fresh))
            fresh = False
            if not last:
                added += rounds[n]
        retained.append(user + added + last_output)
    return requests


def simulate_plain(script: Script, bound: int, params: Params, worst: bool) -> tuple[list[Request], int]:
    """The requests of the plain arm and how many compactions it makes: the whole history every
    request, one summary request when the last usage has reached the trigger."""
    from contexto.bench.arms.plain import compaction_setting

    trigger = int(compaction_setting(bound)["intended_trigger"])
    restored = RESTORED_TOKENS_EXPECTED if script.files else 0
    summary = SUMMARY_TOKENS_WORST if worst else SUMMARY_TOKENS_EXPECTED
    kept = summary + (RESTORED_TOKENS_WORST if worst else restored)
    pending = 1.0 if worst else PENDING_FACTOR  # the worst case compacts as early as it can
    requests: list[Request] = []
    context = params.head_plain  # what the next request re-sends
    last_usage = 0
    fresh = True
    compactions = 0
    for user, rounds, last_output in turn_specs(script, worst, recall=False):
        added = user
        for n in range(len(rounds) + 1):
            if last_usage + int(added * pending) >= trigger and context > params.head_plain + kept:
                requests += [Request(context + 1_000, summary, False, side=True)] * (2 if worst else 1)
                compactions += 1
                context = params.head_plain + kept
                fresh = True
            last = n == len(rounds)
            output = last_output if last else OUTPUT_PER_REQUEST // 2
            requests.append(Request(context + added, output, fresh))
            fresh = False
            context += added + output
            last_usage = context
            added = rounds[n] if not last else 0
    return requests, compactions


def simulate_full(script: Script, params: Params) -> list[Request]:
    sizes = bench_script.estimate(script)
    probes = [_text_tokens(script.turn_bytes[i]) for i, k in enumerate(script.kinds) if k == "probe"]
    tool_frames = len(script.reads) * TOOL_CALL_TOKENS
    return [Request(params.head_full + sizes["history_tokens"] + tool_frames + sum(probes[:k + 1])
                    + k * FULL_OUTPUT_EXPECTED, FULL_OUTPUT_EXPECTED, k == 0) for k in range(len(probes))]


def _price(expected: list[Request], worst: list[Request], reserve: int, uncached: int) -> Estimate:
    out = Estimate(requests_expected=len(expected), requests_worst=len(worst))
    previous = 0
    for request in expected:
        read = 0 if request.fresh else min(previous, request.input)
        out.expected["uncached"] += min(uncached, request.input - read)
        out.expected["cache_read"] += read
        out.expected["cache_write"] += request.input - read - min(uncached, request.input - read)
        out.expected["output"] += request.output
        if not request.side:
            previous = request.input
    for request in worst:
        out.worst["cache_write"] += math.ceil(request.input * ESTIMATE_MARGIN)
        out.worst["output"] += reserve
    return out


def model_factor(model: str | None) -> float:
    """The factor on the Claude Code arms' expected tokens for ``model`` (1.0 without a recorded run)."""
    recorded = RECORDED.get(model or "")
    return recorded[0] / CALIBRATION_TOKENS if recorded else 1.0


def calibration(model: str | None) -> str:
    """One sentence on what the expected figures for ``model`` are calibrated on."""
    recorded = RECORDED.get(model or "")
    if recorded:
        return (f"Calibration: the tool rounds are fitted on {CALIBRATED_ON}; {model} has a recorded run of the same "
                f"session ({recorded[1]}: {recorded[0]:,} tokens on the Contexto arm against {CALIBRATION_TOKENS:,} "
                f"for {CALIBRATED_ON} in run #4), so the expected tokens of the contexto arm are scaled by "
                f"{model_factor(model):.3f}, and the plain arm's by the same factor [assumed: no run of the plain arm "
                "has been recorded on either model]. The full arm and the worst case are not scaled.")
    name = model or "the model"
    if model == CALIBRATED_ON:
        return f"Calibration: the tool rounds are fitted on recorded runs of {CALIBRATED_ON}, the model given."
    return (f"Calibration: the plan is calibrated on {CALIBRATED_ON}; no run of {name} has been recorded, and a model "
            "that calls more tools (claude-opus-5-5 spent 38% more on the Contexto arm) costs more than this.")


def unit_estimate(arm: str, script: Script, bound: int, params: Params | None = None,
                  model: str | None = None) -> Estimate:
    """The plan of one arm on one script: expected and worst-case requests and tokens by class. With a
    ``model`` that has a recorded run, the Claude Code arms' expected tokens are scaled (``model_factor``)."""
    out = _unit_estimate(arm, script, bound, params or engine_params())
    factor = model_factor(model) if arm in SCALED_ARMS else 1.0
    if factor != 1.0:
        out.expected = {name: round(value * factor) for name, value in out.expected.items()}
    return out


def _unit_estimate(arm: str, script: Script, bound: int, params: Params) -> Estimate:
    if arm == "contexto":
        expected = simulate_contexto(script, bound, params, worst=False)
        worst = simulate_contexto(script, bound, params, worst=True)
        out = _price(expected, worst, OUTPUT_RESERVE, UNCACHED_PER_REQUEST)
        out.over_bound_expected = sum(1 for r in expected if r.input > bound)
        return out
    if arm == "plain":
        expected, compactions = simulate_plain(script, bound, params, worst=False)
        worst, _ = simulate_plain(script, bound, params, worst=True)
        out = _price(expected, worst, OUTPUT_RESERVE, UNCACHED_PER_REQUEST)
        out.compactions_expected = compactions
        return out
    if arm == "full":
        requests = simulate_full(script, params)
        return _price(requests, requests, FULL_MAX_TOKENS, UNCACHED_PER_FULL_REQUEST)
    raise ValueError(f"unknown arm {arm!r}")


def units(seeds: list[int], repeats: int, arms: tuple[str, ...]) -> list[tuple[str, int, int]]:
    """Every unit of a run as (arm, seed, repeat), in the order it runs: seed by seed, the first seed
    once per repeat, the arms in an order rotated by one at each step so that no arm always runs
    first, or always just after another has warmed the vendor's side."""
    out = []
    step = 0
    for position, seed in enumerate(seeds):
        for repeat in range(repeats if position == 0 else 1):
            shift = step % len(arms)
            out += [(arm, seed, repeat) for arm in arms[shift:] + arms[:shift]]
            step += 1
    return out


def estimate(scripts: dict[int, Script], todo: list[tuple[str, int, int]], bound: int,
             params: Params | None = None, model: str | None = None) -> dict[str, Any]:
    """The plan for the units in ``todo``: per arm (``units``, ``turns``, an ``Estimate``) and in total."""
    params = params or engine_params()
    cache: dict[tuple[str, int], Estimate] = {}
    arms: dict[str, dict[str, Any]] = {}
    total = Estimate()
    for arm, seed, _repeat in todo:
        if (arm, seed) not in cache:
            cache[(arm, seed)] = unit_estimate(arm, scripts[seed], bound, params, model)
        row = arms.setdefault(arm, {"units": 0, "turns": 0, "estimate": Estimate()})
        row["units"] += 1
        row["turns"] += len(scripts[seed].turns)
        row["estimate"].add(cache[(arm, seed)])
        total.add(cache[(arm, seed)])
    return {"arms": arms, "total": total, "units": len(todo), "turns": sum(row["turns"] for row in arms.values())}


def budget_to_pass(worst_total: int) -> int:
    """The ``--budget-tokens`` figure a plan needs: its worst case, rounded up to 10,000."""
    return math.ceil(worst_total / 10_000) * 10_000


def table(plan: dict[str, Any], case: str) -> list[str]:
    """The lines of one plan table; ``case`` is ``"expected"`` or ``"worst"``."""
    lines = ["| Arm | Units | Turns | Requests | Uncached input | Cache write | Cache read | Output | Total tokens |",
             "|---|---|---|---|---|---|---|---|---|"]
    rows = [(arm, row["units"], row["turns"], row["estimate"]) for arm, row in sorted(plan["arms"].items())]
    rows.append(("total", plan["units"], plan["turns"], plan["total"]))
    for name, count, turns, est in rows:
        classes = est.expected if case == "expected" else est.worst
        requests = est.requests_expected if case == "expected" else est.requests_worst
        cells = [name, str(count), f"{turns:,}", f"{requests:,}", *(f"{classes[c]:,}" for c in CLASSES),
                 f"{sum(classes.values()):,}"]
        lines.append("| " + " | ".join(cells) + " |")
    return lines
