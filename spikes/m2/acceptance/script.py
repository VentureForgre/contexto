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
# Intake (2026-10-16): the probe turn's tool loop cannot rotate, so its recalls are capped. A full
# recall adds about 17,800 tokens; 2 keep the probe turn under the bound. Acceptance 3 needs one.
MAX_RECALLS = 2
INSTRUCTIONS = (
    "This is a long working session. Each message pastes the next part of a working log. Read it, keep "
    "what matters, and reply with exactly: OK"
)


@dataclass(frozen=True)
class Script:
    seed: int
    turns: tuple[str, ...]  # user texts, in order; the last one is the probe turn
    probes: tuple[Probe, ...]
    paged_only: tuple[str, ...]  # probe ids answerable only from turn 1
    turn_bytes: tuple[int, ...]


def build(seed: int = DEFAULT_SEED, turns: int = 20, turn_bytes: int = 30_000, mix: Mix = CHAIN_HEAVY) -> Script:
    """``turns`` content turns of about ``turn_bytes`` bytes each, then one probe turn.

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
        for item in items:  # spread plants through the turn's log
            log.insert(rng.randrange(0, len(log) + 1), item)
        head = INSTRUCTIONS if t == 0 else "Next part of the working log:"
        text = "\n".join([head, *log, "Reply with exactly: OK"])
        texts.append(text)
        sizes.append(len(text.encode("utf-8")))
    all_probes = tuple(paged) + tuple(probes)
    probe_turn = "\n".join([
        "Final step. Answer the questions below about this conversation. You may use the contexto tools "
        f"(contexto_recall, contexto_search, contexto_state) to read earlier turns, with at most {MAX_RECALLS} "
        "contexto_recall calls in total. Reply with one line per question, in the form",
        "q_id: answer",
        "with the answer only. For a list, give the ids separated by commas. If you do not know, write unknown.",
        "",
        *[f"{p.id}: {p.question}" for p in all_probes],
    ])
    texts.append(probe_turn)
    sizes.append(len(probe_turn.encode("utf-8")))
    return Script(seed, tuple(texts), all_probes, tuple(p.id for p in paged), tuple(sizes))


def restatements(script: Script) -> list[str]:
    """Any plant subject that appears outside its own statements and question (must be empty)."""
    problems = []
    for p in script.probes:
        for where, text in enumerate(script.turns):
            residue = text
            for st in p.statements:
                residue = residue.replace(st, "")
            if where == len(script.turns) - 1:
                residue = residue.replace(p.question, "")
            for subject in p.subjects:
                if subject in residue:
                    problems.append(f"{p.id}: {subject!r} restated in turn {where + 1}")
    return problems
