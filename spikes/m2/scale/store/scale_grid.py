"""The D41 grid table from ``scale_length.py --grid`` and ``scale_dumps.py`` outputs (numbers only).

    .venv/bin/python spikes/m2/scale/store/scale_grid.py results/d41/grid          # stub repeats dropped
    .venv/bin/python spikes/m2/scale/store/scale_grid.py results/d41/grid --full   # stub repeats kept

Reads ``probes/summary.md`` and ``dumps_{loud,quiet,many_calls,split_output,edits}.md`` in the given
directory (``probes_full/`` and ``dumps_*_full.md`` with ``--full``) and prints one row per variant: the probes at 6,000 pages on both page-size models (top 3 /
latest first / stale only), whether it loses against D41 at 0ff1bfe on any probe row, the 13-turn cases
passed, main's two D34 scenarios, and stale only in each dump set and in all of them.
"""

from __future__ import annotations

import sys
from pathlib import Path

DUMPS = ("loud", "quiet", "many_calls", "split_output", "edits")
BASE = "text cap 1, page cap none (0ff1bfe)"
DUMP_NAMES = {"D41 at 0ff1bfe (once per text)": BASE, "text cap 1, page cap none, summary kept": BASE}


def rows(text: str) -> list[list[str]]:
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in text.splitlines()
            if line.startswith("|") and not line.startswith("|---")]


def main(argv: list[str]) -> int:
    full = "--full" in argv
    argv = [a for a in argv if a != "--full"]
    if len(argv) != 1:
        raise SystemExit(__doc__)
    root = Path(argv[0])
    probes = (root / ("probes_full" if full else "probes") / "summary.md").read_text(encoding="utf-8")
    quality: dict[str, dict[tuple[str, str], tuple[int, int, int]]] = {}
    turns: dict[str, int] = {}
    scenarios: dict[str, tuple[str, str]] = {}
    for r in rows(probes):
        if len(r) == 8 and r[0] in ("spike", "calibration") and r[2] != "variant":
            quality.setdefault(r[2], {})[(r[0], r[1])] = (int(r[3]), int(r[4]), int(r[5]))
        elif len(r) == 6 and r[0] != "variant":
            turns[r[0]] = int(r[5])
        elif len(r) == 3 and r[0] != "variant":
            scenarios[r[0]] = (r[1], r[2])
    stale: dict[str, dict[str, int]] = {}
    for name in DUMPS:
        for r in rows((root / f"dumps_{name}{'_full' if full else ''}.md").read_text(encoding="utf-8")):
            if len(r) == 7 and r[1] == "all":
                stale.setdefault(DUMP_NAMES.get(r[0], r[0]), {})[name] = int(r[5])
    base = quality[BASE]
    print("| variant | spike 6,000 | real 6,000 | probe rows below D41 | 13-turn (of 4) | page about the query "
          "| newest of equal statements | stale only: " + " / ".join(DUMPS) + " | sum |")
    print("|---|---|---|---:|---:|---|---|---|---:|")
    for variant, q in quality.items():
        label = variant if full or not variant.startswith("text cap") or "(0ff1bfe)" in variant \
            else f"{variant}, repeats dropped"
        worse = sum(1 for k, (top, latest, st) in q.items()
                    if top < base[k][0] or latest < base[k][1] or st > base[k][2])
        s = stale.get(variant, {})
        cells = " / ".join(str(s.get(d, "-")) for d in DUMPS)
        total = sum(s.values()) if len(s) == len(DUMPS) else "-"
        fmt = lambda k, q=q: "{} / {} / {}".format(*q.get(k, ("-", "-", "-")))  # noqa: E731
        print(f"| {label} | {fmt(('spike', '6,000'))} | {fmt(('calibration', '6,000'))} | {worse} | "
              f"{turns.get(variant, '-')} | {scenarios.get(variant, ('-', '-'))[0]} | "
              f"{scenarios.get(variant, ('-', '-'))[1]} | {cells} | {total} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
