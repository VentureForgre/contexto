#!/usr/bin/env python3
"""Spend meter for the L1-d run: sums total_tokens of requests from <first_turn> on.
Usage: meter.py <usage.csv> <first_turn> <cap>  -> prints spend, last context, next-turn estimate, GO/STOP"""
import csv, sys
path, first, cap = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rs = [r for r in csv.DictReader(open(path)) if int(r["turn"]) >= first]
spend = sum(int(r["total_tokens"]) for r in rs)
last = int(rs[-1]["total_tokens"]) if rs else 0
est = 2 * (last + 3500)  # two requests per tool turn, plus one 300-line tool output
print(f"spend={spend} last_context={last} next_turn_est={est} projected={spend + est} "
      f"{'GO' if spend + est <= cap else 'STOP'}")
