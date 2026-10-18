#!/usr/bin/env python3
"""Attribute reminder items outside messages[0] to the feature that produced them, per run and
location, from results/reminders.csv (distinct occurrences over the whole run).

Usage: python3 -I spikes/m2/scale/reminders/features.py [results/reminders.csv]
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

LABELS = (
    ("<total_tokens>", "token countdown (`<total_tokens>N tokens left`)"),
    ("Plan mode is active", "plan mode entry"),
    ("## Exited Plan Mode", "plan mode exit"),
    ("Note: <path> changed on disk", "file changed on disk"),
    ("The task tools haven't", "task tools not used recently"),
    ("Contents of <path>", "nested CLAUDE.md (sub/)"),
    ("# Environment", "session-start environment message"),
)


def label(kind: str) -> str:
    for prefix, name in LABELS:
        if kind.startswith(prefix):
            return name
    return "other: " + kind[:50]


path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "results/reminders.csv"
agg: dict = defaultdict(lambda: [0, 0])
for r in csv.DictReader(open(path)):
    if r["location"] == "m0":
        continue
    a = agg[(r["run"], label(r["kind"]), r["location"])]
    a[0] += 1
    a[1] += int(r["bytes"])
print("| run | feature | location | items | tokens (bytes/1.83) | tokens per item |")
print("|---|---|---|---|---|---|")
for (run, name, loc), (n, b) in sorted(agg.items()):
    print(f"| {run} | {name} | {loc} | {n} | {round(b / 1.83)} | {round(b / 1.83 / n)} |")
