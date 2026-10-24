#!/usr/bin/env python3
"""Append a synthesized assistant message and a token_count event to a throwaway Codex rollout.
Usage: codex_append.py <rollout.jsonl> <marker> <total_tokens>"""
import json
import sys
from datetime import UTC, datetime

path, marker, total = sys.argv[1], sys.argv[2], int(sys.argv[3])
lines = [json.loads(line) for line in open(path)]
ordinal = max(rec.get("ordinal", 0) for rec in lines)
ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
usage = {"input_tokens": total - 10, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
         "output_tokens": 10, "reasoning_output_tokens": 0, "total_tokens": total}
new = [
    {"timestamp": ts, "ordinal": ordinal + 1, "type": "response_item",
     "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"Noted: {marker}."}]}},
    {"timestamp": ts, "ordinal": ordinal + 2, "type": "event_msg",
     "payload": {"type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage,
                                                 "model_context_window": 272000}, "rate_limits": None}},
]
with open(path, "a") as f:
    for r in new:
        f.write(json.dumps(r) + "\n")
