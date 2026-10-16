"""M2 acceptance harness entry point.

From the repository root:
    python3 spikes/m2/acceptance/run.py dry-run [--turns N --turn-kbytes K --head H]   # budget, no calls
    python3 spikes/m2/acceptance/run.py approval-table   # the approval lines the doc carries and live gates on
    python3 spikes/m2/acceptance/run.py score --h1 ANSWERS --h1-record H1JSON --baseline ANSWERS \
        --baseline-result REPLAYJSON [--partial]
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
import traceback
import urllib.parse
from collections.abc import Callable
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
    """The full-context baseline, D27's symmetric form (intake, 2026-10-16 05:42Z): one request per
    probe turn, sent in order, request k carrying probe turns 1..k-1 with the baseline's own answers.
    Each is built from the H1 run's own JSONL, checked with count_tokens against the context window and
    what is left of the approval, then sent once; the first failure stops the rest."""
    from acceptance import credcheck, replay

    s, _ = plan(args.seed, 0, args.turn_kbytes * 1_000)
    try:
        parts = replay.turn_requests(detectors.read_jsonl(args.cc_jsonl), list(s.probe_texts), args.max_tokens)
        first = replay.baseline_request(parts, [], 1)
    except (replay.ReplayError, OSError, StopIteration) as exc:
        print(f"refusing: {exc or 'the first probe turn is not in the JSONL'}", file=sys.stderr)
        return 2
    out: dict[str, Any] = {"label": "no-replayed-thinking baseline, D27: one request per probe turn",
                           "changes_from_recorded": first.get("_changes") or {},
                           "betas": replay.harness_betas(first.get("_betas")), "first_request": replay.summary(first),
                           "probe_turns": len(s.probe_texts), "turns": []}
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
    if args.h1_record is not None:
        try:
            h1 = json.loads(args.h1_record.read_text(encoding="utf-8"))
            h1_spent = int(h1["spent"])
        except (OSError, ValueError, KeyError, TypeError):
            print("refusing: --h1-record is not a live run's H1 record", file=sys.stderr)
            return 2
        out["approval"] = {"combined": args.approved, "h1_spent": h1_spent, "replay": args.approved - h1_spent}
        out["h1_session"] = h1.get("session")  # score ties this baseline to that H1 run
        args.approved -= h1_spent
    status, spent, answers = 0, 0, []
    window = args.context_window or replay.CONTEXT_WINDOW
    try:
        for k in range(1, len(s.probe_texts) + 1):
            request = replay.baseline_request(parts, answers, k)
            turn: dict[str, Any] = {"turn": k, "messages": len(request["messages"])}
            out["turns"].append(turn)
            tokens = replay.count_tokens(request, upstream, key)
            turn["count_tokens"] = tokens
            reason = replay.check_fits(tokens, args.max_tokens, window, args.approved - spent)
            if reason:
                out["stopped"] = f"turn {k}: {reason}"
                status = 1
                break
            result = replay.send(request, upstream, key)
            usage = result["usage"]
            spent += sum(int(usage.get(f) or 0) for f in ("input_tokens", "cache_creation_input_tokens",
                                                          "cache_read_input_tokens", "output_tokens"))
            answers.append(result["text"])
            turn.update({"status": result["status"], "usage": usage, "stop_reason": result["stop_reason"],
                         "blocks": result["blocks"], "answer_chars": len(result["text"])})
    except (replay.ReplayError, driver.AuthFailure, OSError) as exc:
        out["stopped"] = str(exc)
        status = 1
    finally:
        del key
    out["spent"] = spent
    out["completed"] = len(answers)
    # A stopped replay's answers go to a .partial name, never the full-run name (reviewer-2 on PR #123).
    out["answers_file"] = "baseline-answers.partial.txt" if out.get("stopped") else "baseline-answers.txt"
    (args.out / out["answers_file"]).write_text("\n".join(answers) + "\n", encoding="utf-8")
    (args.out / "replay-result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    return status


def h1_spend(results_spent: int, session_log: list[dict[str, Any]], abnormal: bool, reserve: int) -> dict[str, int]:
    """What H1 cost, for the replay's approval (reviewer-2, PR #112). The driver's figure grows only
    when a turn's result arrives, so a stop inside a turn (the spend watcher in a tool loop, a 401 or
    403, Claude Code exiting) drops that turn's requests. The proxy logs every Messages response's
    usage, which is what was billed: take the larger of the two. On an abnormal stop a request may also
    have been in flight, unlogged: allow one more of the last logged input plus the output reserve."""
    logged, last_input = 0, 0
    for ev in session_log:
        if ev.get("event") == "request" and detectors.request_path(ev) in detectors.BILLED_PATHS and ev.get("usage"):
            usage = ev["usage"]
            inp = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens"))
            logged += inp + int(usage.get("output_tokens") or 0)
            last_input = inp
    in_flight = last_input + reserve if abnormal else 0
    return {"spent": max(results_spent, logged) + in_flight, "spent_results": results_spent,
            "spent_logged": logged, "in_flight_allowance": in_flight}


def plan(seed: int = script.DEFAULT_SEED, turns: int = 0, turn_bytes: int = 40_000,
         params: budget.Params = budget.Params()) -> tuple[script.Script, budget.Budget]:
    """The script and its simulated budget: the one figure dry-run prints, the doc shows, and live gates on."""
    s = build_script(seed, turns, turn_bytes, params)
    return s, budget.simulate(list(s.content_bytes), list(s.probe_bytes), params)


def build_script(seed: int, turns: int, turn_bytes: int, params: budget.Params) -> script.Script:
    """With ``turns`` 0, the fewest content turns whose actual sizes reach 2.5x the bound."""
    if turns:
        return script.build(seed, turns, turn_bytes)
    n = 4
    while True:
        s = script.build(seed, n, turn_bytes)
        tokens = sum(budget.turn_tokens(x) + params.reply_tokens for x in s.content_bytes)
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
    score_p.add_argument("--h1-record", type=Path, required=True,
                         help="the H1 run's record (acceptance-<session>-h1.json); a stopped run is refused")
    score_p.add_argument("--partial", action="store_true",
                         help="score a stopped H1 run or baseline anyway, labeled PARTIAL, with no acceptance 2 verdict")
    score_p.add_argument("--baseline", type=Path, required=True)
    score_p.add_argument("--baseline-result", type=Path, required=True,
                         help="the replay's replay-result.json; a stopped or short baseline is refused")
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
    rep.add_argument("--approved", type=int, default=0,
                     help="tokens the replay may spend; with --h1-record, the combined approval instead")
    rep.add_argument("--h1-record", type=Path, default=None,
                     help="the live run's <home>/acceptance-<session>-h1.json: the replay may then spend "
                          "--approved (the combined approval) minus the H1 spend recorded there")
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
    chk.add_argument("--answers", type=Path, default=None,
                     help="the run's answers file: with --session-dir, credits acceptance 3 from the logged "
                          "returned ranges (contract PR #95)")
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
        result = reach.reach(list(s.probes), reach.load_pages(args.session_dir), turn_texts=list(s.content_turns))
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
            if args.answers is not None:
                from acceptance import reach, recall_credit

                probes = [p for p in s0.probes if p.id in s0.paged_only]
                credit = recall_credit.credit(probes, args.answers.read_text(encoding="utf-8"),
                                              detectors.read_jsonl(args.session_log), reach.load_pages(args.session_dir))
                out["recall_credit"] = credit
                out["verdicts"]["recall_credited"] = credit["acceptance_3"]
        print(json.dumps(out, indent=2, default=str))
        return 0 if all(out["verdicts"].values()) else 1

    params = budget.Params(bound=args.bound, head=args.head)
    s, b = plan(args.seed, args.turns, args.turn_kbytes * 1_000, params)
    if args.command == "approval-table":
        print(budget.approval_table(b))
        return 0
    if args.command == "dry-run":
        print(f"Script: seed {s.seed}, {s.n_content} content turns of about {args.turn_kbytes} KB, "
              f"{len(s.probes)} probes ({len(s.paged_only)} paged-only), restatements {len(script.restatements(s))}.")
        print()
        print(budget.table(b, params))
        print()
        print("Turn-size sweep (smallest turn count reaching 2.5x the bound):")
        for row in budget.sweep(list(s.probe_bytes), params):
            print(json.dumps(row))
        return 0
    if args.command == "score":
        # Reviewer-2 on PR #123: a stopped run's answers are partial, and acceptance 2 compares full
        # runs only, on both sides (D27 is symmetric). The H1 record and the replay result decide, not
        # the answers text.
        try:
            record = json.loads(args.h1_record.read_text(encoding="utf-8"))
            replayed = json.loads(args.baseline_result.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not isinstance(replayed, dict):
                raise ValueError
        except (OSError, ValueError):
            print("refusing: --h1-record or --baseline-result is not readable as a run record", file=sys.stderr)
            return 2
        # Reviewer-2 on PR #123: each answers file must be the one its record names, and the baseline
        # must have replayed this H1 run, so no record can vouch for another run's (or partial) answers.
        session = record.get("session")
        ties = {
            "the H1 record's session matches its file name":
                bool(session) and args.h1_record.name == f"acceptance-{session}-h1.json",
            "--h1 is the answers file the H1 record names": bool(record.get("answers_file"))
                and args.h1.resolve() == (args.h1_record.parent / str(record["answers_file"])).resolve(),
            "--baseline is the answers file the replay result names": bool(replayed.get("answers_file"))
                and args.baseline.resolve() == (args.baseline_result.parent / str(replayed["answers_file"])).resolve(),
            "the baseline replayed this H1 session": bool(session) and replayed.get("h1_session") == session,
        }
        broken = [name for name, ok in ties.items() if not ok]
        if broken:
            print(f"refusing: the answers and records are not one run's (failed: {'; '.join(broken)})", file=sys.stderr)
            return 2
        n = len(s.probe_texts)
        h1_full = record.get("stopped") is None and record.get("probe_turns_completed") == n
        turns = replayed.get("turns") or []
        base_done = sum(1 for t in turns if t.get("status") == 200)
        base_full = not replayed.get("stopped") and len(turns) == n and base_done == n
        if not (h1_full and base_full) and not args.partial:
            print(f"refusing: a partial run (H1 stopped {record.get('stopped')!r}, probe turns "
                  f"{record.get('probe_turns_completed')} of {n}; baseline stopped {replayed.get('stopped')!r}, "
                  f"requests {base_done} of {n}); pass --partial for a PARTIAL score with no acceptance 2 verdict",
                  file=sys.stderr)
            return 2
        h1 = score(list(s.probes), args.h1.read_text(encoding="utf-8"))
        base = score(list(s.probes), args.baseline.read_text(encoding="utf-8"))
        paged = [p for p in s.probes if p.id in s.paged_only]
        out = {"h1": h1, "baseline": base, "paged_only": score(paged, args.h1.read_text(encoding="utf-8")),
               "h1_record": {"stopped": record.get("stopped"), "probe_turns_completed": record.get("probe_turns_completed"),
                             "probe_turns": n},
               "baseline_result": {"stopped": replayed.get("stopped"), "requests_completed": base_done, "requests": n}}
        if args.partial:
            out = {"label": "PARTIAL: not an acceptance 2 verdict", **out}
        else:
            out["compare"] = compare(h1, base)
        print(json.dumps(out, indent=2, default=str))
        return 0
    # live: gates on the same Budget that dry-run and approval-table print
    upstream = args.upstream or os.environ.get("ANTHROPIC_BASE_URL")
    return run_h1(s, b, params, args.approved, args.scratch, args.home, args.session, args.seat_settings, upstream)


# Every live-only branch run_h1 can take. The H1 record lists the ones a run took; a dry run fails
# unless its runs, together, took all of them (live-path fix, intake 2026-10-16 07:11Z).
LOOPBACK = ("127.0.0.1", "localhost", "::1")
LIVE_PATHS = ("gate_passed", "estimates_cover_turns", "watcher_started", "content_turn_sent", "probe_turn_sent",
              "every_turn_sent", "stop_handled", "error_handled", "answers_written", "record_written")


def run_h1(s: script.Script, b: budget.Budget, params: budget.Params, approved: int, scratch: Path, home: Path,
           session: str, seat_settings: Path, upstream: str | None, contexto: str = "contexto",
           inject: Callable[[int], None] | None = None) -> int:
    """H1 through the real launcher. Live calls this with the owner's upstream; the stub dry run calls
    it with a local, non-forwarding one, and nothing else differs. ``inject`` (dry runs only) is
    called after each finished turn with its index and may raise, to drive the stop and error
    branches. The H1 record is written however the run ends; a harness error is re-raised after it."""
    if b.history_tokens < 2.5 * params.bound:
        print(f"refusing: planned history {b.history_tokens:,} is below 2.5x the bound ({int(2.5 * params.bound):,})",
              file=sys.stderr)
        return 2
    # D27 (intake 05:42Z): the approval covers H1 and the six-request baseline, and the gate is their
    # combined worst case. H1's guards stop at the approval minus the baseline's worst, so the
    # baseline always keeps its share.
    combined_worst = b.h1_worst + b.baseline_replay_worst
    if approved < combined_worst:
        print(f"refusing: approved {approved:,} is below the combined worst case {combined_worst:,} "
              f"(H1 {b.h1_worst:,} + baseline {b.baseline_replay_worst:,})", file=sys.stderr)
        return 2
    h1_cap = approved - b.baseline_replay_worst
    if not upstream:
        print("refusing: no upstream (pass --upstream or set ANTHROPIC_BASE_URL)", file=sys.stderr)
        return 2
    if inject is not None and urllib.parse.urlsplit(upstream).hostname not in LOOPBACK:
        print("refusing: the inject hook is for dry runs against a local stub only", file=sys.stderr)
        return 2
    if not seat_settings.is_file():
        print(f"refusing: the seat settings file {seat_settings} does not exist", file=sys.stderr)
        return 2
    scratch.mkdir(parents=True, exist_ok=True)
    if driver.inside_git(scratch):
        print(f"refusing: the scratch directory {scratch} is inside a git work tree", file=sys.stderr)
        return 2
    estimates = budget.turn_estimates(b)
    if len(estimates) != len(s.turns):
        print(f"refusing: {len(estimates)} spend estimates for {len(s.turns)} turns", file=sys.stderr)
        return 2
    taken = {"gate_passed", "estimates_cover_turns"}
    config = driver.isolated_config(scratch, seat_settings)
    env = driver.child_env(home, config)
    env.pop("ANTHROPIC_BASE_URL", None)  # the launcher points Claude Code at its proxy; nothing goes around it
    session_log = home / "sessions" / session / "log.jsonl"
    transport = driver.StreamJsonTransport(driver.launcher_command(session, params.bound, upstream, contexto), scratch,
                                           env, session_log, approved=h1_cap, reserve=params.max_tokens_reserve)
    if transport.watcher is not None:
        taken.add("watcher_started")
    run = driver.Run(transport, h1_cap, params.max_tokens_reserve, estimates)
    status, stopped, error, cli_exit = 0, None, None, None
    answers: list[str] = []
    try:
        for i, text in enumerate(s.turns):
            answers.append(run.turn(i, text))
            taken.add("probe_turn_sent" if i >= s.n_content else "content_turn_sent")
            if inject is not None:
                inject(i)
        taken.add("every_turn_sent")
    except (driver.BudgetExceeded, driver.AuthFailure, RuntimeError) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        status, stopped = 1, f"{type(exc).__name__}: {exc}"
        taken.add("stop_handled")
    except Exception as exc:  # noqa: BLE001  a harness error: record the spend first, then re-raise
        traceback.print_exc()
        status, stopped, error = 1, f"{type(exc).__name__}: {exc}", exc
        taken.add("error_handled")
    finally:
        cli_exit = transport.close()
    # D27: the answers of every finished probe turn, in order (all 54 lines on a full run). A stopped
    # run's go to a .partial.txt file, never the full-run name, so they cannot be scored as a full run.
    probe_answers = answers[s.n_content:]
    home.mkdir(parents=True, exist_ok=True)
    answers_name = f"acceptance-{session}-answers{'.partial' if stopped else ''}.txt"
    (home / answers_name).write_text("\n".join(probe_answers) + "\n", encoding="utf-8")
    taken.add("answers_written")
    spend = h1_spend(run.spent, detectors.read_jsonl(session_log) if session_log.exists() else [],
                     abnormal=status != 0, reserve=params.max_tokens_reserve)
    taken.add("record_written")
    record = {**spend, "session": session, "turns": run.turns, "stream_compactions": transport.compactions, "cli_exit": cli_exit,
              "approved": approved, "h1_cap": h1_cap, "baseline_worst": b.baseline_replay_worst,
              "planned_turns": len(s.turns), "turns_completed": len(answers),
              "probe_turns_completed": len(probe_answers), "stopped": stopped, "answers_file": answers_name,
              "paths": sorted(taken)}
    # The replay's approval is the combined approval minus this spend (run.py replay --h1-record).
    (home / f"acceptance-{session}-h1.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))
    if error is not None:
        raise error
    return status

if __name__ == "__main__":
    sys.exit(main())
