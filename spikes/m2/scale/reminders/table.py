#!/usr/bin/env python3
"""The README's per-run table from results/turns.csv: reminder placement at the last turn, and
Contexto's carried content from turn 2 (the first turn with something paged) to the last.

Usage: python3 -I spikes/m2/scale/reminders/table.py [results/turns.csv]
"""
import csv
import sys
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "results/turns.csv"
rows = list(csv.DictReader(open(path)))


def tok(b: str) -> int:
    return round(int(b) / 1.83)


print("| run | model | turns | later turn-start reminders n / tok | in tool_result n / tok | role-system msgs n / tok "
      "| carried at k after fallback, turn 2 -> last | context blocks at k | session-start class at k n / tok "
      "| not pageable at k, turn 2 -> last | not pageable growth per turn |")
print("|" + "---|" * 11)
for run in dict.fromkeys(r["run"] for r in rows):
    rr = [r for r in rows if r["run"] == run]
    f = next(r for r in rr if r["turn"] == "2")
    last = rr[-1]
    per = (int(last["carried_unpageable_k"]) - int(f["carried_unpageable_k"])) / (int(last["turn"]) - 2)
    print(f"| {run} | {last['model']} | {last['turn']} | {last['ts_later_n']} / {tok(last['ts_later_bytes'])} "
          f"| {last['tool_result_n']} / {tok(last['tool_result_bytes'])} | {last['system_msgs']} / {tok(last['system_msgs_bytes'])} "
          f"| {f['carried_after_fallback']} -> {last['carried_after_fallback']} | {f['carried_blocks_k']} -> {last['carried_blocks_k']} "
          f"| {last['session_start_msgs_k']} / {last['carried_session_start_k']} "
          f"| {f['carried_unpageable_k']} -> {last['carried_unpageable_k']} | {per:.0f} |")
