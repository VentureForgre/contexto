"""``contexto bench --chart RESULTS``: draw the charts of a results file as SVG. No calls.

``RESULTS`` is a ``results.json`` written by a live run, by ``--report`` or by ``--replay``. The
charts go into ``charts/`` beside it, or into ``OUT/charts/`` when ``--out`` is given:
``accuracy.svg`` (a live file only: a replay has no answers to score) and ``request-size.svg``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FLAG = "--chart"


def add_arguments(parser) -> None:
    parser.add_argument(FLAG, type=Path, default=None, metavar="RESULTS",
                        help="draw the charts of a results.json as SVG into charts/ beside it, or into OUT/charts/ "
                             "with --out: accuracy by arm with intervals (a live results file only) and request "
                             "size against history length")


def run(args: argparse.Namespace) -> int:
    from contexto.bench import charts

    source = Path(args.chart)
    try:
        results = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"contexto bench: error: {source}: cannot read the file: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"contexto bench: error: {source}: not JSON ({exc})", file=sys.stderr)
        return 1
    if not isinstance(results, dict) or "request_series" not in results:
        print(f"contexto bench: error: {source}: not a bench results file (it has no request_series)",
              file=sys.stderr)
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
