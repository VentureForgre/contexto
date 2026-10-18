"""Before and after table for two ``scale_bench.py`` result directories (D34; numbers only, zero spend).

    .venv/bin/python spikes/m2/scale/store/scale_compare.py results/d34_before results/d34_after

Per store size and dialect: the planted page in the 3 detailed hits (of 86 probe queries), changing
values whose latest page comes first (of 24), facts with a query in the detailed hits (of 30),
search latency over stdio (median of the per-query, per-pass medians, as ``summary.md``), the cold
first search of each MCP child (the first call of each pass, which loads the page cache after D34),
and ``index_text`` warm (median) and cold (the first call of each pass on a fresh store).
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from scale_probes import FACTS  # noqa: E402

N_QUERIES = sum(len(f.queries) for f in FACTS)
N_CHANGING = sum(len(f.queries) for f in FACTS if f.kind == "changing")


def med(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def quality(rows: list[dict[str, str]]) -> tuple[int, int, int, int]:
    detailed = sum(r["outcome"] == "detailed" for r in rows)
    latest = sum(r["latest_first"] == "True" for r in rows if r["kind"] == "changing")
    facts = len({r["fact"] for r in rows if r["outcome"] == "detailed"})
    shown = sum(r["value_shown"] == "True" for r in rows)
    return detailed, latest, facts, shown


def latency(rows: list[dict[str, str]], mode: str, kinds: tuple[str, ...]) -> float:
    per: list[float] = []
    for p in sorted({r["pass"] for r in rows}):
        qs: dict[str, list[float]] = {}
        for r in rows:
            if r["pass"] == p and r["mode"] == mode and r["kind"] in kinds and r["seconds"]:
                qs.setdefault(r["query"], []).append(float(r["seconds"]))
        per += [med(v) for v in qs.values()]
    return med(per)


def cold(rows: list[dict[str, str]], mode: str, kinds: tuple[str, ...]) -> float:
    """The first call of each pass of the given kinds (a fresh process or store), median over passes."""
    firsts = []
    for p in sorted({r["pass"] for r in rows}):
        calls = [r for r in rows if r["pass"] == p and r["mode"] == mode and r["kind"] in kinds and r["seconds"]]
        if calls:
            firsts.append(float(calls[0]["seconds"]))
    return med(firsts)


def table(before: Path, after: Path) -> str:
    out = [f"| N | dialect | run | top 3 (of {N_QUERIES}) | latest first (of {N_CHANGING}) | facts (of 30) | "
           "value shown | search rare / common / multi, s | first search, s | index_text warm / cold, s | "
           "MCP peak RSS MB |",
           "|---:|---|---|---:|---:|---:|---:|---|---:|---|---:|"]
    data = {}
    for label, d in (("before", before), ("after", after)):
        data[label] = (list(csv.DictReader((d / "quality.csv").open())),
                       list(csv.DictReader((d / "latency.csv").open())))
    keys = sorted({(r["n"], r["dialect"]) for r in data["before"][0]} | {(r["n"], r["dialect"]) for r in data["after"][0]},
                  key=lambda k: (k[1], int(k[0])))
    search = ("rare", "common", "multi")
    for n, dialect in keys:
        for label in ("before", "after"):
            qual, lat = data[label]
            q = [r for r in qual if (r["n"], r["dialect"]) == (n, dialect)]
            lt = [r for r in lat if (r["n"], r["dialect"]) == (n, dialect)]
            if not q:
                continue
            detailed, latest, facts, shown = quality(q)
            rss = [int(r["chars"]) for r in lt if r["kind"] == "peak_rss"]
            speed = " / ".join(f"{latency(lt, 'stdio', (k,)):.3f}" for k in search)
            out.append(f"| {int(n):,} | {dialect} | {label} | {detailed} | {latest} | {facts} | {shown} | {speed} | "
                       f"{cold(lt, 'stdio', search):.3f} | {latency(lt, 'inproc', ('index_text',)):.3f} / "
                       f"{cold(lt, 'inproc', ('index_text',)):.3f} | {med(rss) / 1e6:.0f} |")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    print(table(Path(argv[0]), Path(argv[1])), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
