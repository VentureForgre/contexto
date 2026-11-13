"""``contexto bench --report DIR [DIR ...]``: recompute the results of existing output directories.

It reads ``manifest.json``, ``probes.jsonl`` and ``requests.jsonl`` from each directory and writes
``results.json``, ``results.md`` and the charts: all of them or none, so a report that fails
leaves the files of an earlier report as they were. No calls are made. With one directory it
writes into that directory unless ``--out`` is given; with several (one per model, or one run
split across shells) it needs ``--out``. ``--prices FILE`` replaces the shipped price table.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from contexto.bench.modes import add_option

FLAG = "--report"


def add_arguments(parser) -> None:
    parser.add_argument(FLAG, nargs="+", type=Path, default=None, metavar="DIR",
                        help="recompute results.json, results.md and the charts from the records in one or more "
                             "existing output directories, making no calls; several directories are reported per "
                             "model and need --out")
    add_option(parser, "--prices", type=Path, default=None, metavar="FILE",
               help="a price table to use in place of the published prices shipped with contexto (the same JSON "
                    "shape as src/contexto/bench/data/prices.json)")


def run(args: argparse.Namespace) -> int:
    from contexto.bench import charts, prices, records, report

    sources = [Path(d) for d in args.report]
    if args.out is None and len(sources) > 1:
        print("contexto bench: error: --report with more than one directory needs --out DIR to write the merged "
              "results into", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out is not None else sources[0]
    try:
        table = prices.load(args.prices) if getattr(args, "prices", None) is not None else None
        results = report.write(out, sources=sources, prices=table)
    except (records.RecordError, report.ReportError, prices.PriceError, charts.ChartError) as exc:
        print(f"contexto bench: error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"contexto bench: error: cannot write the results into {out}: {exc}", file=sys.stderr)
        return 1
    for name in ("results.json", "results.md", *(chart["path"] for chart in results["charts"])):
        print(f"written: {out / name}")
    for chart in results["charts_skipped"]:
        print(f"not written: {chart['path'].removeprefix('charts/')}: {chart['reason']}")
    text = (out / "results.md").read_text(encoding="utf-8")
    print()
    print(text[text.index("## What this run shows"):].rstrip())
    return 0
