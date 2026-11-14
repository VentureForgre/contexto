"""``contexto bench --script-only``: write the generated session scripts and check them. No calls.

For each seed it writes ``<out>/scripts/seed-<n>.json`` (``contexto.bench.model.Script.to_json``) and
prints the script's turn count, bytes and token estimate, its probes by class with their mean
lexical overlap, and the result of the generator's self-checks. It exits 1 if a self-check fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from contexto.bench.modes import seeds

if TYPE_CHECKING:
    from contexto.bench.model import Script

FLAG = "--script-only"


def add_arguments(parser) -> None:
    parser.add_argument(FLAG, action="store_true",
                        help="write the generated session script of each seed to OUT/scripts/seed-<n>.json, print "
                             "its sizes and self-checks, and make no calls")


def script_path(out: Path, seed: int) -> Path:
    """Where a seed's script is written inside an output directory."""
    return out / "scripts" / f"seed-{seed}.json"


def describe(script: Script) -> tuple[list[str], bool]:
    """The lines ``--script-only`` prints for one script, and whether its self-checks all passed."""
    from contexto.bench import script as bench_script  # imported here, not at every contexto start
    sizes = bench_script.estimate(script)
    lines = [
        f"seed {script.seed}: protocol {script.protocol}, bound {script.bound:,} tokens",
        f"  turns: {sizes['turns']} ({sizes['content_turns']} content, {sizes['probe_turns']} probe)",
        f"  bytes: {sizes['bytes']:,} (of which files the tool-loop turn reads: {sizes['file_bytes']:,})",
        f"  token estimate: {sizes['tokens']:,} (content turns {sizes['history_tokens']:,} at 1.8 bytes per token "
        f"plus {bench_script.REPLY_TOKENS} per reply, {sizes['history_tokens'] / script.bound:.2f} times the bound; "
        f"probe turns {sizes['probe_tokens']:,} at 3.5 bytes per token)",
        f"  probes: {len(script.probes)}",
    ]
    for cls, cell in bench_script.overlap_by_class(script).items():
        lines.append(f"    {cls}: {cell['probes']} probes, mean lexical overlap {cell['mean_overlap']:.2f} words")
    results = bench_script.checks(script)
    for name, problems in results.items():
        lines.append(f"  check {name}: {'pass' if not problems else f'FAIL ({len(problems)})'}")
        lines += [f"    {problem}" for problem in problems[:10]]
        if len(problems) > 10:
            lines.append(f"    and {len(problems) - 10} more")
    return lines, not any(results.values())


def run(args: argparse.Namespace) -> int:
    if args.out is None:
        print("contexto bench: error: --script-only needs --out DIR to write the scripts into", file=sys.stderr)
        return 2
    from contexto.bench import script as bench_script

    status = 0
    for seed in seeds(args):
        try:
            script = bench_script.build(seed, args.protocol, args.bound, args.turn_kbytes * 1_000)
        except ValueError as exc:
            print(f"contexto bench: error: seed {seed}: {exc}", file=sys.stderr)
            return 2
        path = script_path(args.out, seed)
        try:
            script.write(path)
        except OSError as exc:
            print(f"contexto bench: error: cannot write {path}: {exc}", file=sys.stderr)
            return 1
        lines, sound = describe(script)
        print("\n".join(lines))
        print(f"  written: {path} (sha256 {script.digest()})")
        if not sound:
            status = 1
    return status
