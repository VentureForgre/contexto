#!/usr/bin/env python3
"""Print the reminder timeline of one run: for the last main request (the whole history), every
reminder item with the turn it belongs to, message index, location and size.

Usage: .venv/bin/python spikes/m2/scale/reminders/timeline.py <run_root> [--csv out.csv]
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import TURN_RE, blocks, kind, locate  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("root")
ap.add_argument("--csv")
a = ap.parse_args()
bodies = sorted(Path(a.root, "bodies").glob("*.json"))
body = next(b for b in (json.loads(f.read_bytes() or b"{}") for f in reversed(bodies)) if b.get("tools"))
msgs = body["messages"]
turn_of, t = {}, 0
for i, m in enumerate(msgs):
    if m.get("role") == "user":
        for b in blocks(m.get("content")):
            g = TURN_RE.search(b.get("text", "") if b.get("type") == "text" else "")
            if g:
                t = int(g.group(1))
    turn_of[i] = t
rows = []
for i, loc, s in locate(msgs):
    path = re.search(r"Note: (\S+) changed on disk", s)
    rows.append({"turn": turn_of[i], "msg_index": i, "location": loc, "kind": kind(s)[:60],
                 "file": path.group(1).split("/project/")[-1] if path else "", "tokens": round(len(s.encode()) / 1.83)})
for r in rows:
    print(f"{r['turn']:>3} {r['msg_index']:>4} {r['location']:<16} {r['tokens']:>6} {r['file']:<14} {r['kind']}")
if a.csv:
    with open(a.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
