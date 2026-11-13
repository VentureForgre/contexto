"""Replay the D63 scenarios through the engine and print what was measured.

    uv run python spikes/m2/inturn/replay.py --scenario all --json out.json
    uv run python spikes/m2/inturn/replay.py --scenario small --full      # 1,500 rounds, minutes of CPU

No model calls: the upstream is ``upstream.FakeUpstream``, in this process. The tables are Markdown.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inturn import harness, scripts  # noqa: E402

DIALECTS = ("claude", "codex")


def _n(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _share(value: float) -> str:
    return f"{value * 100:.1f}%"


def _kinds(counts: dict[str, int]) -> str:
    return ", ".join(f"{kind} {count:,}" for kind, count in counts.items() if count) or "0"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join(lines + ["| " + " | ".join(row) + " |" for row in rows])


def tables(runs: list[dict[str, Any]]) -> str:
    """The three tables for a list of ``Run.metrics()`` dicts."""
    bound, cost, rooms = [], [], []
    for m in runs:
        name = [m["scenario"], m["dialect"]]
        bound.append([*name, _n(m["requests"]), _n(m["over_bound"]["estimate"]), _n(m["over_bound"]["real"]),
                      _n(m["largest"]["estimate"]), _n(m["largest"]["tokens"]), _n(m["largest"]["bytes"]),
                      _n(m["pages"]), _n(m["rotations"]), _kinds(m["violations"])])
        lost = {k: v for k, v in m["info"].items() if k in ("inflight_thinking_missing", "inflight_reasoning_dropped")}
        cost.append([*name, _n(m["input_tokens"]), _share(m["cache_read_share"]), _n(m["rewrites"]["count"]),
                     _n(m["rewrites"]["tokens"]), _n(m["rewrites"]["mean"]), _share(m["rewrites"]["share"]),
                     _n(sum(lost.values()))])
        for conversation, room in m["room"].items():
            rooms.append([*name, conversation, _n(m["requests_by_conversation"][conversation]),
                          _n(room["published"]), _n(room["min"]), _n(room["median"]), _n(room["zero"]),
                          _n(room["zero_past_50k"])])
    return "\n\n".join([
        _table(["scenario", "dialect", "requests", "over_bound (estimate)", "over_bound (real)", "largest estimate",
                "largest tokens", "largest bytes", "pages", "rotations", "violations"], bound),
        _table(["scenario", "dialect", "conversation", "requests", "rooms published", "room min", "room median",
                "requests at 0", "at 0 past 50,000"], rooms),
        _table(["scenario", "dialect", "input tokens sent", "cache read", "rewrites", "re-sent tokens",
                "mean per rewrite", "re-sent share", "in-flight thinking missing"], cost),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", default="all", help="all, or names separated by commas: "
                        + ", ".join(scripts.BUILDERS))
    parser.add_argument("--dialect", default="all", choices=("all", *DIALECTS))
    parser.add_argument("--full", action="store_true",
                        help="the full sizes (small: 1,500 rounds instead of the tests' reduced size)")
    parser.add_argument("--json", type=Path, help="also write the numbers to this file")
    args = parser.parse_args(argv)
    names = list(scripts.BUILDERS) if args.scenario == "all" else args.scenario.split(",")
    unknown = [n for n in names if n not in scripts.BUILDERS]
    if unknown:
        parser.error(f"unknown scenario: {', '.join(unknown)}")
    runs = []
    for name in names:
        scenario = scripts.build(name, full=args.full)
        for dialect in scenario.dialects:
            if args.dialect in ("all", dialect):
                print(f"running {name} on {dialect} ...", file=sys.stderr, flush=True)
                runs.append(harness.run(scenario, dialect).metrics())
    if not runs:
        parser.error("nothing to run: that scenario does not run on that dialect")
    print(tables(runs))
    if args.json:
        try:
            args.json.write_text(json.dumps({"runs": runs}, indent=2) + "\n")
        except OSError as error:
            print(f"cannot write {args.json}: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
