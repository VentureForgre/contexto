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
    drawn, left_out = charts.render(results)  # every chart is drawn before one is written
    if not drawn:
        reasons = "; ".join(reason for _, reason in left_out) or "the results file has nothing to draw"
        print(f"contexto bench: error: {source}: {reasons}", file=sys.stderr)
        return 1
    try:
        written = charts.write(None, target, drawn)
    except OSError as exc:
        print(f"contexto bench: error: cannot write the charts into {target}: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"written: {path}")
    for name, reason in left_out:
        print(f"not written: {name}: {reason}")
    return 0
