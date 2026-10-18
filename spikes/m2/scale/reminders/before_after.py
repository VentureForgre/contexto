#!/usr/bin/env python3
"""Before and after D32 and D33: carried tokens at chosen turns, from two turns.csv files written by
analyze.py (one run on the code before the change, one after, over the same captured bodies).

Usage: python3 -I spikes/m2/scale/reminders/before_after.py <before/turns.csv> <after/turns.csv>

Columns: ``carried_unpageable_k`` (what is left once the fallback pages everything it may page) and
``carried_after_fallback`` (carried content with the 8,000-token fallback applied at every turn start).
"""
import csv
import sys

before = list(csv.DictReader(open(sys.argv[1])))
after = list(csv.DictReader(open(sys.argv[2])))
COLUMNS = ("carried_unpageable_k", "carried_after_fallback")
TURNS = (2, 12, 24, 36, 48)


def series(rows: list[dict], run: str, col: str) -> dict[int, int]:
    return {int(r["turn"]): int(r[col]) for r in rows if r["run"] == run}


for col in COLUMNS:
    print(f"\n`{col}`, before -> after (Contexto estimator tokens)\n")
    print("| run | " + " | ".join(f"turn {t}" for t in TURNS) + " | max, turn 2 to last | per turn, turn 12 to last |")
    print("|---|" + "---|" * (len(TURNS) + 2))
    for run in dict.fromkeys(r["run"] for r in before):
        b, a = series(before, run, col), series(after, run, col)
        cells = [f"{b[t]:,} -> {a[t]:,}" if t in b else "" for t in TURNS]
        last = max(b)
        peak = f"{max(v for t, v in b.items() if t >= 2):,} -> {max(v for t, v in a.items() if t >= 2):,}"
        if 12 in b and last > 12:
            span = last - 12
            rate = f"{(b[last] - b[12]) / span:.0f} -> {(a[last] - a[12]) / span:.0f}"
        else:
            rate = ""
        print(f"| {run} | " + " | ".join(cells) + f" | {peak} | {rate} |")
