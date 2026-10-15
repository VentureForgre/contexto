"""Offline simulation of the four conditions. Simulated, not measured against any model.

Usage, from spikes/m0:
    python3 -m fidelity.simulate [--seed N] [--turns N] [--json PATH]

For each condition it replays the session turn by turn, building the request the policy would send
after every turn, and reports: estimated input tokens of the final request, cumulative estimated
input tokens over the session, how many times the window start moved (each move changes the
prompt prefix and so costs a prompt-cache miss), how many requests were not a pure append to the
previous one (edit points; these must equal the window moves), and the simulated reader's probe
score at four points in the session (a quarter, half, three quarters, and the end), each scored
against the values current at that point. Every request is also checked for protocol validity and
for the truth of the pinned obligations header.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from fidelity import oracle
from fidelity.policy import (
    CONDITIONS,
    Settings,
    Store,
    WindowState,
    append_only,
    build_request,
    check_protocol,
    over_bound,
    pinned_claim_errors,
)
from fidelity.scoring import Score, score
from fidelity.session import DEFAULT_SEED, DEFAULT_TURNS, Probe, Session, generate


@dataclass(frozen=True)
class ConditionResult:
    condition: str
    final_tokens: int
    cumulative_tokens: int
    window_moves: int
    edit_points: int
    over_bound: int
    recalls: int
    correct: int
    total: int
    by_category: dict[str, tuple[int, int]]
    stale_answers: int
    all_points_correct: int
    all_points_total: int


def probe_points(session: Session) -> list[int]:
    return sorted({session.turns // 4, session.turns // 2, 3 * session.turns // 4, session.turns})


def probes_at(session: Session, upto: int) -> tuple[list[Probe], dict[str, list[str]]]:
    """Probes for items stated before turn ``upto``, with the answers current at that point."""
    probes: list[Probe] = []
    older: dict[str, list[str]] = {}
    for probe in session.probes:
        history = [(turn, value) for turn, value in session.planted[probe.key].history if turn < upto]
        if not history:
            continue
        probes.append(Probe(probe.id, probe.category, probe.key, probe.question, history[-1][1]))
        older[probe.key] = [value for _, value in history[:-1]]
    return probes, older


def run_condition(session: Session, condition: str, settings: Settings) -> ConditionResult:
    state = WindowState()
    store = Store()
    cumulative = 0
    moves = 0
    edits = 0
    over = 0
    last_first = 0
    previous = None
    request = None
    points = set(probe_points(session))
    point_scores: list[Score] = []
    final_recalls = 0
    for upto in range(1, session.turns + 1):
        request = build_request(session, condition, upto, settings, state, store)
        problems = check_protocol(request.messages) + pinned_claim_errors(session, request)
        if problems:
            raise AssertionError(f"{condition} turn {upto}: {problems[:3]}")
        cumulative += request.tokens
        if request.window_first_turn != last_first:
            moves += 1
            last_first = request.window_first_turn
        if previous is not None and not append_only(previous, request):
            edits += 1
        if condition != "full" and over_bound(session, request, upto, settings):
            over += 1
        previous = request
        if upto in points:
            probes, older = probes_at(session, upto)
            answers = {}
            recalls = 0
            for probe in probes:
                got = oracle.answer(session.planted[probe.key], request, store)
                answers[probe.id] = got.text
                recalls += got.recalls
            point_scores.append(score(probes, answers, older))
            final_recalls = recalls
    assert request is not None
    result = point_scores[-1]
    return ConditionResult(
        condition=condition,
        final_tokens=request.tokens,
        cumulative_tokens=cumulative,
        window_moves=moves,
        edit_points=edits,
        over_bound=over,
        recalls=final_recalls,
        correct=result.correct,
        total=result.total,
        by_category=dict(result.by_category),
        stale_answers=len(result.stale),
        all_points_correct=sum(s.correct for s in point_scores),
        all_points_total=sum(s.total for s in point_scores),
    )


def table(results: list[ConditionResult]) -> str:
    categories = sorted({c for r in results for c in r.by_category})
    head = ["condition", "final req tokens (est)", "cumulative tokens (est)", "window moves", "edit points", "over bound",
            "recalls", "score (final)", "score (4 points)"]
    head += categories
    rows = [head, ["---"] * len(head)]
    for r in results:
        row = [r.condition, str(r.final_tokens), str(r.cumulative_tokens), str(r.window_moves), str(r.edit_points), str(r.over_bound),
               str(r.recalls), f"{r.correct}/{r.total}", f"{r.all_points_correct}/{r.all_points_total}"]
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
