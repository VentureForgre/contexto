"""The acceptance session script: turns to send, plants, and probes with ground truth.

Acceptance 2 uses the chain-heavy mix of the L2f harder probes (intake, PR #20). Acceptance 3 needs
at least one probe answerable only from a paged-out turn: those plants go in the first turn, which
the run is guaranteed to page out because its local history reaches 2.5 times the bound, and they
are unflagged config lines nobody is asked to pin.

The script is deterministic for a seed. Nothing it generates is committed.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from pathlib import Path

_SPIKES = Path(__file__).resolve().parents[2] / "m0"
if str(_SPIKES) not in sys.path:
    sys.path.insert(0, str(_SPIKES))

from fidelity.harder import Mix, _group, _merge, _Pools  # noqa: E402
from fidelity.l2f import Probe  # noqa: E402
from fidelity.session import _filler_text  # noqa: E402

DEFAULT_SEED = 20261016
# Chain-heavy: deep supersession chains were the only type that failed at small contexts.
CHAIN_HEAVY = Mix(multihop=((3, 4), (4, 4)), chain=((6, 15), (8, 15)), obligation_single=4, obligation_sets=2,
                  set_size=6, distractor=((8, 6),), reasoning=0)
PAGED_ONLY = 4  # probes whose only statement sits in turn 1
# A report-only recall cap per probe turn (detectors.recalls_over_budget). Intake (2026-10-16): a cost
# guard only, not told to the model. The budget prices tool-loop rounds instead (budget.Params.tool_rounds,
# calibrated on live run #3).
MAX_RECALLS = 2
# Intake D27 (2026-10-16 05:37Z): the probes are asked over PROBE_TURNS user turns of PROBES_PER_TURN,
# by a fixed assignment (assign_probes), written down before any result and never tuned.
PROBE_TURNS = 6
PROBES_PER_TURN = 9
PROBE_HEADER = (
    "Answer the questions below about this conversation. Reply with one line per question, in the form",
    "q_id: answer",
    "with the answer only. For a list, give the ids separated by commas. If the conversation does not "
    "contain it, say unknown.",
)
INSTRUCTIONS = (
    "This is a long working session. Each message pastes the next part of a working log. Read it, keep "
    "what matters, and reply with exactly: OK"
)


@dataclass(frozen=True)
class Script:
    seed: int
    turns: tuple[str, ...]  # user texts, in order: the content turns, then the probe turns
    probes: tuple[Probe, ...]
    paged_only: tuple[str, ...]  # probe ids answerable only from turn 1
    turn_bytes: tuple[int, ...]
    probe_turn_ids: tuple[tuple[str, ...], ...] = ()  # probe ids asked in each probe turn, in order

    @property
    def n_content(self) -> int:
        return len(self.turns) - len(self.probe_turn_ids)

    @property
    def content_turns(self) -> tuple[str, ...]:
        return self.turns[: self.n_content]

    @property
    def content_bytes(self) -> tuple[int, ...]:
        return self.turn_bytes[: self.n_content]

    @property
    def probe_texts(self) -> tuple[str, ...]:
        return self.turns[self.n_content:]

    @property
    def probe_bytes(self) -> tuple[int, ...]:
        return self.turn_bytes[self.n_content:]


def assign_probes(probes: list[Probe], seed: int = DEFAULT_SEED, turns: int = PROBE_TURNS,
                  per_turn: int = PROBES_PER_TURN) -> list[list[Probe]]:
    """D27's fixed assignment, written down before any result (2026-10-16 05:39Z): shuffle a copy of
    the probes in script order with random.Random(seed).shuffle, then turn k asks positions
    [per_turn * (k - 1), per_turn * k)."""
    if len(probes) != turns * per_turn:
        raise ValueError(f"{len(probes)} probes do not fill {turns} turns of {per_turn}")
    order = list(probes)
    random.Random(seed).shuffle(order)
    return [order[per_turn * k: per_turn * (k + 1)] for k in range(turns)]


def build(seed: int = DEFAULT_SEED, turns: int = 20, turn_bytes: int = 30_000, mix: Mix = CHAIN_HEAVY) -> Script:
    """``turns`` content turns of about ``turn_bytes`` bytes each, then the D27 probe turns.

    Plants are merged in random order (each plant's statements stay in order) and spread across
    turns 2 to ``turns``; turn 1 carries only the paged-only plants and filler.
    """
    if turns < 4:
        raise ValueError("need at least 4 content turns")
    rng = random.Random(seed)
    pools = _Pools(rng)
    probes, plants = _group(pools, mix, "a", 0)
    events = _merge(rng, plants)

    paged: list[Probe] = []
    first_lines: list[str] = []
    for i in range(PAGED_ONLY):
        svc = pools.services.pop()
        key = f"{svc}.retention_days"
        value = pools.value()
        line = f"cfg {key}={value}"
        first_lines.append(line)
        paged.append(Probe(f"q_p_{i + 1:02d}", "a", "paged_only", "turn-1", f"What value did {key} have in the logs "
                           "earlier in this conversation? Answer with the value only.", value, subjects=(key,),
                           statements=(line,)))

    per_turn: list[list[str]] = [[] for _ in range(turns)]
    per_turn[0] = first_lines
    for j, ev in enumerate(events):
        per_turn[1 + j * (turns - 1) // len(events)].append(ev.text)

    texts: list[str] = []
    sizes: list[int] = []
    sample = _filler_text(random.Random(0), 200, "s00")
    line_bytes = (len(sample.encode("utf-8")) + 1) / 200  # measured on the generator, not assumed
    for t, items in enumerate(per_turn):
        used = sum(len(x.encode("utf-8")) + 1 for x in items)
        filler_lines = max(0, int((turn_bytes - used - 80) / line_bytes))
        log = _filler_text(rng, filler_lines, f"s{t}").split("\n") if filler_lines else []
        # Spread the turn's plants through its log in statement order: sorted slots, each shifted by
        # the plants already placed. Independent random slots put two links of a chain planted in
        # the same turn in reverse order (13 of 30 chains in live run #2's script; intake 08:10Z).
        slots = sorted(rng.randrange(0, len(log) + 1) for _ in items)
        for k, (slot, item) in enumerate(zip(slots, items)):
            log.insert(slot + k, item)
        head = INSTRUCTIONS if t == 0 else "Next part of the working log:"
        text = "\n".join([head, *log, "Reply with exactly: OK"])
        texts.append(text)
        sizes.append(len(text.encode("utf-8")))
    all_probes = tuple(paged) + tuple(probes)
    # Product-realistic (intake, 2026-10-16): no mention of Contexto's tools or a recall cap; the
    # preamble and page index must lead the model to recall. D27: six turns of nine probes.
    blocks = assign_probes(list(all_probes), seed)
    for block in blocks:
        probe_turn = "\n".join([*PROBE_HEADER, "", *[f"{p.id}: {p.question}" for p in block]])
        texts.append(probe_turn)
        sizes.append(len(probe_turn.encode("utf-8")))
    return Script(seed, tuple(texts), all_probes, tuple(p.id for p in paged), tuple(sizes),
                  tuple(tuple(p.id for p in block) for block in blocks))


def restatements(script: Script) -> list[str]:
    """Any plant subject that appears outside its own statements and question (must be empty)."""
    problems = []
    for p in script.probes:
        for where, text in enumerate(script.turns):
            residue = text
            for st in p.statements:
                residue = residue.replace(st, "")
            if where >= script.n_content:  # a probe turn asks the question itself
                residue = residue.replace(p.question, "")
            for subject in p.subjects:
                if subject in residue:
                    problems.append(f"{p.id}: {subject!r} restated in turn {where + 1}")
    return problems
