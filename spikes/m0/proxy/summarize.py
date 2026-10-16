"""Numbers-only summary of measuring-proxy runs, for the M0 doc and lane 2's cost model.

    python3 summarize.py RUN_DIR [RUN_DIR ...] --csv OUT.csv

One CSV row per model call (count_tokens, health checks, and handshakes are left out) with the
lane 2 fields plus the head split, tool_result bytes, cache reuse flags, and a compaction-shape
flag. Prints a short per-run digest. The output holds sizes, counts, and usage only.

Flags, all derived from the proxy log:
- cache_full_miss: cache_read is 0 on a call after the first one of the run.
- cache_partial: cache_read is below 90% of the previous call's cached prefix
  (cache_read + cache_creation), for example after compaction replaced the history.
- compaction_shape (Claude Code): the call lacks the claude-code-20250219 beta that every normal
  agent call in the run carries. Measured runs show this on the summary call compaction makes.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from extract import SUMMARY_FIELDS, total_tokens

EXTRA_FIELDS = (
    "run",
    "seq",
    "transport",
    "system_bytes",
    "tools_bytes",
    "tool_count",
    "first_message_bytes",
    "messages_bytes",
    "tool_result_bytes",
    "context_tokens",
    "cache_full_miss",
    "cache_partial",
    "compaction_shape",
    "stop_reason",
)
FIELDS = SUMMARY_FIELDS + EXTRA_FIELDS
CLAUDE_CODE_BETA = "claude-code-20250219"


def is_model_call(record: dict[str, Any]) -> bool:
    endpoint = record.get("endpoint", "")
    if record.get("transport") == "websocket-handshake" or record.get("status") not in (200, 101):
        return False
    return endpoint.endswith("/messages") or endpoint.endswith("/responses")


def context_tokens(usage: dict[str, Any]) -> int:
    return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def run_rows(run_dir: Path) -> list[dict[str, Any]]:
    records = []
    for line in (run_dir / "requests.jsonl").read_text().splitlines():
        record = json.loads(line)
        if "seq" in record and is_model_call(record):
            records.append(record)
    records.sort(key=lambda r: r["seq"])
    has_cc_beta = any(CLAUDE_CODE_BETA in (r.get("anthropic_beta") or []) for r in records)
    rows, previous = [], None
    for record in records:
        usage = record.get("usage") or {}
        read = usage.get("cache_read_input_tokens") or 0
        row = {field: record.get(field) for field in SUMMARY_FIELDS}
        row.update({field: usage.get(field) for field in
                    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")})
        row.update(
            run=run_dir.name,
            seq=record["seq"],
            transport=record.get("transport", "http"),
            system_bytes=record.get("system_bytes"),
            tools_bytes=record.get("tools_bytes"),
            tool_count=record.get("tool_count"),
            first_message_bytes=record.get("first_message_bytes"),
            messages_bytes=record.get("messages_bytes"),
            tool_result_bytes=(record.get("class_bytes") or {}).get("tool_result", 0),
            context_tokens=context_tokens(usage),
            cache_full_miss=int(previous is not None and read == 0),
            cache_partial=0,
            compaction_shape=int(has_cc_beta and CLAUDE_CODE_BETA not in (record.get("anthropic_beta") or [])),
            stop_reason=record.get("stop_reason"),
        )
        if previous is not None and read:
            prior = previous.get("usage") or {}
            prefix = (prior.get("cache_read_input_tokens") or 0) + (prior.get("cache_creation_input_tokens") or 0)
            row["cache_partial"] = int(prefix > 0 and read < 0.9 * prefix)
        rows.append(row)
        previous = record
    return rows


def digest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"calls": 0}
    first = rows[0]
    totals = {k: sum(r[k] or 0 for r in rows) for k in
              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
    turns = sorted({r["turn"] for r in rows if r["turn"] is not None})
    return {
        "calls": len(rows),
        "turns": len(turns),
        "head": {"system_bytes": first["system_bytes"], "tools_bytes": first["tools_bytes"],
                 "tool_count": first["tool_count"], "first_message_bytes": first["first_message_bytes"],
                 "first_call_context_tokens": first["context_tokens"]},
        "max_context_tokens": max(r["context_tokens"] for r in rows),
        "tokens": dict(totals, total=total_tokens(totals)),
        "cache_full_miss_seqs": [r["seq"] for r in rows if r["cache_full_miss"]],
        "cache_partial_seqs": [r["seq"] for r in rows if r["cache_partial"]],
        "compaction_shape_seqs": [r["seq"] for r in rows if r["compaction_shape"]],
        "tool_result_bytes_by_turn": {t: max(r["tool_result_bytes"] for r in rows if r["turn"] == t) for t in turns},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--csv", type=Path, help="write the numbers-only per-call CSV here")
    args = parser.parse_args(argv)
    all_rows = []
    for run_dir in args.runs:
        rows = run_rows(run_dir)
        all_rows += rows
        print(json.dumps({"run": run_dir.name, **digest(rows)}, sort_keys=True))
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(all_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
