"""Offline simulation of the four conditions. Simulated, not measured against any model.

Usage, from spikes/m0:
    python3 -m fidelity.simulate [--seed N] [--turns N] [--json PATH]

For each condition it replays the session turn by turn, building the request the policy would send
after every turn, and reports: estimated input tokens of the final request, cumulative estimated
input tokens over the session, how many times the window start moved (each move changes the
prompt prefix and so costs a prompt-cache miss), and the simulated reader's probe score.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from fidelity import oracle
from fidelity.policy import CONDITIONS, Settings, Store, WindowState, build_request, check_protocol
from fidelity.scoring import Score, older_values, score
from fidelity.session import DEFAULT_SEED, DEFAULT_TURNS, Session, generate


@dataclass(frozen=True)
class ConditionResult:
    condition: str
    final_tokens: int
    cumulative_tokens: int
    window_moves: int
    recalls: int
    correct: int
    total: int
    by_category: dict[str, tuple[int, int]]
    stale_answers: int


def run_condition(session: Session, condition: str, settings: Settings) -> ConditionResult:
    state = WindowState()
    store = Store()
    cumulative = 0
    moves = 0
    last_first = 0
    request = None
    for upto in range(1, session.turns + 1):
        request = build_request(session, condition, upto, settings, state, store)
        problems = check_protocol(request.messages)
        if problems:
            raise AssertionError(f"{condition} turn {upto}: {problems[:3]}")
        cumulative += request.tokens
        if request.window_first_turn != last_first:
            moves += 1
            last_first = request.window_first_turn
    assert request is not None
    answers = {}
    recalls = 0
    for probe in session.probes:
        got = oracle.answer(session.planted[probe.key], request, store)
        answers[probe.id] = got.text
        recalls += got.recalls
    result: Score = score(session.probes, answers, older_values(session.planted))
    return ConditionResult(
        condition=condition,
        final_tokens=request.tokens,
        cumulative_tokens=cumulative,
        window_moves=moves,
        recalls=recalls,
        correct=result.correct,
        total=result.total,
        by_category=dict(result.by_category),
        stale_answers=len(result.stale),
    )


def table(results: list[ConditionResult]) -> str:
    categories = sorted({c for r in results for c in r.by_category})
    head = ["condition", "final req tokens (est)", "cumulative tokens (est)", "window moves", "recalls", "score"]
    head += categories
    rows = [head, ["---"] * len(head)]
    for r in results:
        row = [r.condition, str(r.final_tokens), str(r.cumulative_tokens), str(r.window_moves), str(r.recalls)]
        row.append(f"{r.correct}/{r.total}")
        row += [f"{r.by_category[c][0]}/{r.by_category[c][1]}" if c in r.by_category else "-" for c in categories]
        rows.append(row)
    return "\n".join("| " + " | ".join(row) + " |" for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    parser.add_argument("--json", type=Path, help="also write results as JSON (sizes and scores only)")
    args = parser.parse_args(argv)
    session = generate(args.seed, args.turns)
    settings = Settings()
    results = [run_condition(session, c, settings) for c in CONDITIONS]
    print(f"seed={args.seed} turns={session.turns} messages={len(session.messages)} probes={len(session.probes)}")
    print(f"settings={asdict(settings)}")
    print(table(results))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seed": args.seed,
            "turns": session.turns,
            "settings": asdict(settings),
            "results": [asdict(r) for r in results],
        }
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
