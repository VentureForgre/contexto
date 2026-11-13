"""``contexto bench --chart RESULTS``: draw the charts of a results file as SVG. No calls.

``RESULTS`` is a ``results.json`` written by a live run, by ``--report`` or by ``--replay``. The
charts go into ``charts/`` beside it, or into ``OUT/charts/`` when ``--out`` is given:
``accuracy.svg`` (a live file only: a replay has no answers to score) and ``request-size.svg``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

FLAG = "--chart"


def add_arguments(parser) -> None:
    parser.add_argument(FLAG, type=Path, default=None, metavar="RESULTS",
                        help="draw the charts of a results.json as SVG into charts/ beside it, or into OUT/charts/ "
                             "with --out: accuracy by arm with intervals (a live results file only) and request "
                             "size against history length")


def run(args: argparse.Namespace) -> int:
    from contexto.bench import charts, records

    source = Path(args.chart)
    try:
        results = records.read_results(source)
    except records.RecordError as exc:
        print(f"contexto bench: error: {exc}", file=sys.stderr)
        return 1
    target = (Path(args.out) if args.out is not None else source.parent) / "charts"
    try:
        written = charts.write(results, target)
    except charts.ChartError as exc:
        print(f"contexto bench: error: {source}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"contexto bench: error: cannot write the charts into {target}: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"written: {path}")
    for name, reason in charts.skipped(results):
        print(f"not written: {name}: {reason}")
    return 0
