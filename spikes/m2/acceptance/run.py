"""M2 acceptance harness entry point.

From the repository root:
    python3 spikes/m2/acceptance/run.py dry-run [--turns N --turn-kbytes K --head H]   # budget, no calls
    python3 spikes/m2/acceptance/run.py approval-table   # the approval lines the doc carries and live gates on
    python3 spikes/m2/acceptance/run.py score --h1 ANSWERS --baseline ANSWERS [--seed N --turns N --turn-kbytes K]
    python3 spikes/m2/acceptance/run.py check --session-log PATH --cc-jsonl PATH [--bound 100000]
    python3 spikes/m2/acceptance/run.py check-credential --seat-settings PATH        # zero spend
    python3 spikes/m2/acceptance/run.py live --approved TOKENS --scratch DIR --home DIR --session ID \
        --seat-settings PATH [--upstream URL]
The live mode refuses to start without an approved token figure that covers the plan, or with a
plan below 2.5 times the bound; the guard stops before passing the approval, and the first 401 or
403 stops the run. Nothing here has been run live.
"""

from __future__ import annotations

import argparse
import json
import os
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


def run_replay(args: argparse.Namespace) -> int:
    """Phase 2: the full-context baseline. Builds the request from the H1 run's own JSONL, checks the
    exact token count against the context window and the approval, then sends it once."""
    from acceptance import credcheck, replay

    s, _ = plan(args.seed, 0, args.turn_kbytes * 1_000)
    try:
        request = replay.build_request(detectors.read_jsonl(args.cc_jsonl), s.turns[-1], args.max_tokens)
    except (replay.ReplayError, OSError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    out: dict[str, Any] = {"request": replay.summary(request)}
    if args.dry:
        print(json.dumps(out, indent=2))
        return 0
    upstream = args.upstream or os.environ.get("ANTHROPIC_BASE_URL")
    if not upstream:
        print("refusing: no upstream (pass --upstream or set ANTHROPIC_BASE_URL)", file=sys.stderr)
        return 2
    if args.seat_settings is None or not args.seat_settings.is_file():
        print("refusing: --seat-settings must name the seat's settings file", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        key = credcheck._helper_output(args.seat_settings)  # in memory; header only
    except (RuntimeError, OSError):
        print("refusing: the seat's apiKeyHelper did not produce a key; output not shown", file=sys.stderr)
        return 2
    status = 0
    try:
        tokens = replay.count_tokens(request, upstream, key)
        out["count_tokens"] = tokens
        window = args.context_window or replay.CONTEXT_WINDOW
        reason = replay.check_fits(tokens, args.max_tokens, window, args.approved)
        if reason:
            out["stopped"] = reason
            status = 1
        else:
            result = replay.send(request, upstream, key)
            (args.out / "baseline-answers.txt").write_text(result["text"] + "\n", encoding="utf-8")
            out.update({"status": result["status"], "usage": result["usage"], "stop_reason": result["stop_reason"],
                        "blocks": result["blocks"], "answer_chars": len(result["text"])})
    except (replay.ReplayError, driver.AuthFailure, OSError) as exc:
        out["stopped"] = str(exc)
        status = 1
    finally:
        del key
    (args.out / "replay-result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    return status


def plan(seed: int = script.DEFAULT_SEED, turns: int = 0, turn_bytes: int = 40_000,
         params: budget.Params = budget.Params()) -> tuple[script.Script, budget.Budget]:
    """The script and its simulated budget: the one figure dry-run prints, the doc shows, and live gates on."""
    s = build_script(seed, turns, turn_bytes, params)
    return s, budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], params)


def build_script(seed: int, turns: int, turn_bytes: int, params: budget.Params) -> script.Script:
    """With ``turns`` 0, the fewest content turns whose actual sizes reach 2.5x the bound."""
    if turns:
        return script.build(seed, turns, turn_bytes)
    n = 4
    while True:
        s = script.build(seed, n, turn_bytes)
        tokens = sum(budget.turn_tokens(x) + params.reply_tokens for x in s.turn_bytes[:-1])
        if tokens >= 2.5 * params.bound:
            return s
        n += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M2 acceptance harness")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "approval-table", "score", "live"):
        sp = sub.add_parser(name)
        sp.add_argument("--seed", type=int, default=script.DEFAULT_SEED)
        sp.add_argument("--turns", type=int, default=0, help="content turns; 0 = the fewest reaching 2.5x the bound")
        sp.add_argument("--turn-kbytes", type=int, default=40)
        sp.add_argument("--bound", type=int, default=150_000)
        sp.add_argument("--head", type=int, default=budget.Params.head)
    score_p = sub.choices["score"]
    score_p.add_argument("--h1", type=Path, required=True)
    score_p.add_argument("--baseline", type=Path, required=True)
    live = sub.choices["live"]
    live.add_argument("--approved", type=int, required=True, help="tokens intake approved for the H1 run")
    live.add_argument("--scratch", type=Path, required=True)
    live.add_argument("--home", type=Path, required=True)
    live.add_argument("--session", required=True)
    live.add_argument("--seat-settings", type=Path, required=True,
                      help="the seat's settings file whose apiKeyHelper the run uses, read-only")
    live.add_argument("--upstream", default=None, help="upstream base URL (default: $ANTHROPIC_BASE_URL)")
    rep = sub.add_parser("replay", help="full-context baseline: one direct request after the H1 run")
    rep.add_argument("--cc-jsonl", type=Path, required=True, help="the H1 run's Claude Code session JSONL")
    rep.add_argument("--out", type=Path, required=True, help="private directory for the answers and counts")
    rep.add_argument("--dry", action="store_true", help="build the request and print its sizes; no calls")
    rep.add_argument("--approved", type=int, default=0, help="tokens intake approved for the replay")
    rep.add_argument("--seat-settings", type=Path, default=None)
    rep.add_argument("--upstream", default=None, help="upstream base URL (default: $ANTHROPIC_BASE_URL)")
    rep.add_argument("--context-window", type=int, default=None)
    rep.add_argument("--max-tokens", type=int, default=budget.Params.max_tokens_reserve)
    rep.add_argument("--seed", type=int, default=script.DEFAULT_SEED)
    rep.add_argument("--turn-kbytes", type=int, default=40)
    cred = sub.add_parser("check-credential")
    cred.add_argument("--seat-settings", type=Path, required=True)
    rc = sub.add_parser("reach", help="offline: can contexto_search reach every probe's answer page")
    rc.add_argument("--session-dir", type=Path, required=True, help="a run's sessions/<id> directory (pages/)")
    rc.add_argument("--seed", type=int, default=script.DEFAULT_SEED)
    rc.add_argument("--turn-kbytes", type=int, default=40)
    chk = sub.add_parser("check")
    chk.add_argument("--session-log", type=Path, required=True)
    chk.add_argument("--cc-jsonl", type=Path, required=True)
    chk.add_argument("--bound", type=int, default=150_000)
    chk.add_argument("--session-dir", type=Path, default=None,
                     help="session directory, to confirm the paged-only plants were really paged")
    args = parser.parse_args(argv)

    if args.command == "check-credential":
        from acceptance import credcheck

        result = credcheck.check(args.seat_settings)
        print(json.dumps(result, indent=2))
        return 0 if result.get("pass") else 1

    if args.command == "replay":
        return run_replay(args)

    if args.command == "reach":
        from acceptance import reach

        s, _ = plan(args.seed, 0, args.turn_kbytes * 1_000)
        result = reach.reach(list(s.probes), reach.load_pages(args.session_dir), turn_texts=list(s.turns[:-1]))
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
        return 0 if not result["unreachable"] and result.get("mapping_ok", True) else 1  # fails closed

    if args.command == "check":
        report = detectors.analyze(detectors.read_jsonl(args.session_log), detectors.read_jsonl(args.cc_jsonl), args.bound)
        out = {k: v for k, v in report.__dict__.items()}
        out["cache_hit_rate"] = report.cache_hit_rate
        out["verdicts"] = report.verdicts()
        if args.session_dir is not None:
            s0 = script.build()
            paged = detectors.paged_statements(args.session_dir, [p.statements[0] for p in s0.probes
                                                                  if p.id in s0.paged_only])
            out["paged_only_plants_paged"] = f"{sum(paged.values())}/{len(paged)}"
            out["verdicts"]["paged_only_plants_paged"] = all(paged.values())
        print(json.dumps(out, indent=2, default=str))
        return 0 if all(out["verdicts"].values()) else 1

    params = budget.Params(bound=args.bound, head=args.head)
    s, b = plan(args.seed, args.turns, args.turn_kbytes * 1_000, params)
    if args.command == "approval-table":
        print(budget.approval_table(b))
        return 0
    if args.command == "dry-run":
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
    # live: gates on the same Budget that dry-run and approval-table print
    if b.history_tokens < 2.5 * args.bound:
        print(f"refusing: planned history {b.history_tokens:,} is below 2.5x the bound ({int(2.5 * args.bound):,})",
              file=sys.stderr)
        return 2
    if args.approved < b.h1_worst:
        print(f"refusing: approved {args.approved:,} is below the worst-case H1 cost {b.h1_worst:,}", file=sys.stderr)
        return 2
    upstream = args.upstream or os.environ.get("ANTHROPIC_BASE_URL")
    if not upstream:
        print("refusing: no upstream (pass --upstream or set ANTHROPIC_BASE_URL)", file=sys.stderr)
        return 2
    if not args.seat_settings.is_file():
        print(f"refusing: the seat settings file {args.seat_settings} does not exist", file=sys.stderr)
        return 2
    args.scratch.mkdir(parents=True, exist_ok=True)
    if driver.inside_git(args.scratch):
        print(f"refusing: the scratch directory {args.scratch} is inside a git work tree", file=sys.stderr)
        return 2
    config = driver.isolated_config(args.scratch, args.seat_settings)
    estimates = [r.input_tokens for r in b.rows if r.kind == "turn"] + [max(r.input_tokens for r in b.rows if r.kind != "turn")]
    session_log = args.home / "sessions" / args.session / "log.jsonl"
    transport = driver.StreamJsonTransport(driver.launcher_command(args.session, args.bound, upstream), args.scratch,
                                           driver.child_env(args.home, config), session_log,
                                           approved=args.approved, reserve=params.max_tokens_reserve)
    run = driver.Run(transport, args.approved, params.max_tokens_reserve, estimates)
    status = 0
    try:
        answer = driver.run_session(list(s.turns), run)
        (args.home / f"acceptance-{args.session}-answers.txt").write_text(answer + "\n", encoding="utf-8")
    except (driver.BudgetExceeded, driver.AuthFailure, RuntimeError) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        status = 1
    finally:
        transport.close()
    print(json.dumps({"spent": run.spent, "turns": run.turns, "stream_compactions": transport.compactions}, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
