"""M2 acceptance harness entry point.

From the repository root:
    python3 spikes/m2/acceptance/run.py dry-run [--turns N --turn-kbytes K --head H]   # budget, no calls
    python3 spikes/m2/acceptance/run.py score --h1 ANSWERS --baseline ANSWERS [--seed N --turns N --turn-kbytes K]
    python3 spikes/m2/acceptance/run.py check --session-log PATH --cc-jsonl PATH [--bound 100000]
    python3 spikes/m2/acceptance/run.py live --approved TOKENS --scratch DIR --home DIR --session ID
The live mode refuses to start without an approved token figure, and the guard stops before
passing it. Nothing here has been run live.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acceptance import budget, detectors, driver, script  # noqa: E402
from fidelity.l2f import score  # noqa: E402  (script.py puts spikes/m0 on the path)


def compare(h1: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Acceptance 2 pass rule: within 2 probes of baseline overall and no category more than 1 below."""
    categories = sorted({k for k in baseline["cells"] if "/" not in k} | {k for k in h1["cells"] if "/" not in k})
    gaps = {c: baseline["cells"].get(c, {}).get("correct", 0) - h1["cells"].get(c, {}).get("correct", 0)
            for c in categories}
    overall_gap = baseline["correct"] - h1["correct"]
    return {"overall_gap": overall_gap, "category_gaps": gaps,
            "pass": overall_gap <= 2 and all(g <= 1 for g in gaps.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M2 acceptance harness")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "score", "live"):
        sp = sub.add_parser(name)
        sp.add_argument("--seed", type=int, default=script.DEFAULT_SEED)
        sp.add_argument("--turns", type=int, default=12)
        sp.add_argument("--turn-kbytes", type=int, default=44)
        sp.add_argument("--bound", type=int, default=100_000)
        sp.add_argument("--head", type=int, default=budget.Params.head)
    score_p = sub.choices["score"]
    score_p.add_argument("--h1", type=Path, required=True)
    score_p.add_argument("--baseline", type=Path, required=True)
    live = sub.choices["live"]
    live.add_argument("--approved", type=int, required=True, help="tokens intake approved for the H1 run")
    live.add_argument("--scratch", type=Path, required=True)
    live.add_argument("--home", type=Path, required=True)
    live.add_argument("--session", required=True)
    chk = sub.add_parser("check")
    chk.add_argument("--session-log", type=Path, required=True)
    chk.add_argument("--cc-jsonl", type=Path, required=True)
    chk.add_argument("--bound", type=int, default=100_000)
    args = parser.parse_args(argv)

    if args.command == "check":
        report = detectors.analyze(detectors.read_jsonl(args.session_log), detectors.read_jsonl(args.cc_jsonl), args.bound)
        out = {k: v for k, v in report.__dict__.items()}
        out["cache_hit_rate"] = report.cache_hit_rate
        out["verdicts"] = report.verdicts()
        print(json.dumps(out, indent=2, default=str))
        return 0 if all(report.verdicts().values()) else 1

    s = script.build(args.seed, args.turns, args.turn_kbytes * 1_000)
    params = budget.Params(bound=args.bound, head=args.head)
    if args.command == "dry-run":
        b = budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], params)
        print(f"Script: seed {s.seed}, {len(s.turns) - 1} content turns of about {args.turn_kbytes} KB, "
              f"{len(s.probes)} probes ({len(s.paged_only)} paged-only), restatements {len(script.restatements(s))}.")
        print()
        print(budget.table(b, params))
        print()
        print("Turn-size sweep (smallest turn count reaching 2.5x the bound):")
        for row in budget.sweep(s.turn_bytes[-1], params):
            print(json.dumps(row))
        return 0
    if args.command == "score":
        h1 = score(list(s.probes), args.h1.read_text(encoding="utf-8"))
        base = score(list(s.probes), args.baseline.read_text(encoding="utf-8"))
        paged = [p for p in s.probes if p.id in s.paged_only]
        print(json.dumps({"h1": h1, "baseline": base, "compare": compare(h1, base),
                          "paged_only": score(paged, args.h1.read_text(encoding="utf-8"))}, indent=2, default=str))
        return 0
    # live
    b = budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], params)
    if args.approved < b.h1_expected:
        print(f"refusing: approved {args.approved:,} is below the expected H1 cost {b.h1_expected:,}", file=sys.stderr)
        return 2
    args.scratch.mkdir(parents=True, exist_ok=True)
    estimates = [r.input_tokens for r in b.rows if r.kind == "turn"] + [max(r.input_tokens for r in b.rows if r.kind != "turn")]
    transport = driver.StreamJsonTransport(driver.launcher_command(args.session, args.bound), args.scratch,
                                           driver.child_env(args.home))
    run = driver.Run(transport, args.approved, params.max_tokens_reserve, estimates)
    status = 0
    try:
        answer = driver.run_session(list(s.turns), run)
        (args.home / f"acceptance-{args.session}-answers.txt").write_text(answer + "\n", encoding="utf-8")
    except (driver.BudgetExceeded, RuntimeError) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        status = 1
    finally:
        transport.close()
    print(json.dumps({"spent": run.spent, "turns": run.turns}, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
