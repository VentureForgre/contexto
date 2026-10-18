#!/usr/bin/env python3
"""Print the README results table (markdown) from results.csv. Usage: make_table.py <csv>"""
import csv
import sys

rows = list(csv.DictReader(open(sys.argv[1])))
print("| Run | Model | Usage mode | Tool loop | History (tokens) | Planted usage | Session file MB | "
      "Sent? | Launch to 1st request s | 1st body MB | Requests | Compaction requests | count_tokens | "
      "CLI peak RSS MB | Exit | Load (1m) |")
print("|" + "---|" * 16)
for r in rows:
    print(f"| {r['label'].split('-')[0]} | {r['model'].replace('claude-', '')} | {r['mode']} | {r['tool_loop']} | "
          f"{int(r['session_tokens']):,} | {int(r['planted_usage']):,} | {r['session_file_mb_before']} -> "
          f"{r['session_file_mb_after']} | {'yes' if r['sent_request'] == 'True' else 'no'} | "
          f"{r['launch_to_first_req_s']} | {r['first_req_body_mb']} | {r['messages_requests']} | "
          f"{r['compaction_requests']} | {r['count_tokens_calls']} | {r['cli_maxrss_mb']} | {r['exit']}"
          f"{' (timeout)' if r['timed_out'] not in ('False', '') else ''} | {r['load1_before']} |")
bad = sum(int(r["non_loopback"]) for r in rows)
ref = sum(int(r["refused_proxy"]) for r in rows)
seen = sum(int(r["sockets_seen"]) for r in rows)
samples = sum(int(r["lsof_samples"]) for r in rows)
print(f"\nRuns: {len(rows)}. Non-loopback sockets: {bad}. Refused proxy attempts: {ref}. "
      f"lsof samples: {samples}; distinct sockets seen: {seen} (all 127.0.0.1 to the fake upstream).")
