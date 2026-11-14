"""``contexto bench --dry-run``: the plan of a live run and its token estimate. No calls, no key.

It prints, per arm and in total, the units, turns, estimated requests and estimated tokens by class,
for the expected case and the worst case, the model behind the figures (``contexto.bench.plan``), and
the ``--budget-tokens`` figure the live run needs. With ``--out`` naming a run that was started, the
plan covers only the units that run still has to do.
"""

from __future__ import annotations

import argparse
import sys

from contexto.bench.modes import _live_options

FLAG = "--dry-run"


def add_arguments(parser) -> None:
    parser.add_argument(FLAG, action="store_true",
                        help="print the live run's plan and token estimate, per arm and in total, and the "
                             "--budget-tokens figure to pass; make no calls")
    # The options a plan depends on (--model, --arms, --budget-tokens) are shared with the live run through
    # add_option: the help lists them under whichever of the two modes adds them first. The live run's other
    # options (a key helper, the upstream, the caps) are its OWN_OPTIONS, and are refused here.
    _live_options.add_shared(parser)


def run(args: argparse.Namespace) -> int:
    from contexto.bench import plan as bench_plan
    from contexto.bench import runner
    from contexto.bench.arms import plain

    config = _live_options.config(args, None)
    spent, skipped, todo = 0, 0, bench_plan.units(config.seeds, config.repeats, config.arms)
    try:
        scripts = runner.build_scripts(config)
        if config.out is not None and (old := runner.read_manifest(config.out)) is not None:
            if config.model:
                runner.merge_manifest(old, config, None)  # refuses a different run, as the live run would
            done = {runner.unit_key(u) for u in old["units"] if u.get("status") == "complete"}
            skipped = len(done & set(todo))
            todo = [u for u in todo if u not in done]
            from contexto.bench.meter import read_spent

            spent = read_spent(config.out / "requests.jsonl")
    except (runner.Refused, ValueError) as exc:
        print(f"contexto bench: error: {exc}", file=sys.stderr)
        return runner.EXIT_USAGE
    plan = bench_plan.estimate(scripts, todo, config.bound, model=config.model or None)
    total = plan["total"]
    first = config.seeds[0]
    seed_text = f"seed {first}"
    if len(config.seeds) > 1:
        seed_text = f"{len(config.seeds)} seeds, {first} to {config.seeds[-1]}"
    repeat_text = f", the first seed {config.repeats} times" if config.repeats > 1 else ""
    lines = [
        "contexto bench --dry-run: the plan of a live run. No calls were made and no key was read.",
        f"Run: model {config.model or '(none given)'}, bound {config.bound:,} tokens, protocol {config.protocol}, "
        f"{seed_text}{repeat_text}, arms {', '.join(config.arms)}: {plan['units']} units to run.",
    ]
    if skipped or spent:
        lines.append(f"The output directory already holds {skipped} complete units and {spent:,} recorded tokens; "
                     "the plan below covers the rest.")
    lines += ["", "Expected case (tokens), a rough size:", *bench_plan.table(plan, "expected"), "",
              "Worst case (tokens), a planning figure and not a ceiling: every input token counted as a cache write "
              f"with a 10% margin, and an output reservation of {bench_plan.OUTPUT_RESERVE:,} tokens on every Claude "
              f"Code request and {bench_plan.FULL_MAX_TOKENS:,} on every full-arm request. Claude Code itself asks for "
              "up to 128,000 output tokens a request on claude-haiku-5-5, so a run could pass this figure; the "
              "ceiling is --budget-tokens.", *bench_plan.table(plan, "worst"), ""]
    script = scripts[first]
    for arm in config.arms:
        est = bench_plan.unit_estimate(arm, script, config.bound, model=config.model or None)
        extra = ""
        if arm == "contexto":
            extra = f"; {est.over_bound_expected} planned requests above the bound"
        elif arm == "plain":
            extra = f"; {est.compactions_expected} compactions expected"
        lines.append(f"One {arm} unit (seed {first}, {len(script.turns)} turns): {est.requests_expected} requests and "
                     f"{est.expected_total:,} tokens expected, {est.requests_worst} requests and {est.worst_total:,} "
                     f"tokens at worst{extra}.")
    if "plain" in config.arms:
        setting = plain.compaction_setting(config.bound)
        lines.append(f"Plain arm: {setting['variable']}={setting['value']}. {setting['note']}")
    lines += ["", bench_plan.calibration(config.model or None), *bench_plan.NOTES, ""]
    need = bench_plan.budget_to_pass(spent + total.worst_total)
    lines.append(f"Budget: pass --budget-tokens {need} (the worst case of {total.worst_total:,} tokens"
                 + (f" plus the {spent:,} already recorded" if spent else "") + ", rounded up to 10,000). That figure "
                 "is the ceiling: the live run refuses a smaller one, and its guard reserves each request's own "
                 "max_tokens while the request is in flight and refuses the request that could pass the budget, so a "
                 "run that would pass it stops early instead. The expected case is "
                 f"{total.expected_total:,} tokens.")
    if config.budget is not None:
        verdict = "covers" if config.budget >= spent + total.worst_total else "is below"
        lines.append(f"--budget-tokens {config.budget:,} {verdict} the worst case.")
    print("\n".join(lines))
    return 0
