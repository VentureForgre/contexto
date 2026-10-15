#!/usr/bin/env python3
"""Usage summary from a Claude Code session JSONL: one row per API message, deduplicated by
message.id (streamed assistant records repeat the same message). Prints numbers, model,
record-type counts, and hook-attachment count only; never content.

Usage: session_usage.py <session.jsonl>
"""
import collections
import json
import sys


def main() -> None:
    seen = {}
    types = collections.Counter()
    hooks = 0
    with open(sys.argv[1]) as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            types[o.get("type")] += 1
            a = o.get("attachment") or {}
            if "hook" in str(a.get("type", "")):
                hooks += 1
            m = o.get("message") if o.get("type") == "assistant" else None
            if isinstance(m, dict) and m.get("usage") and m.get("id"):
                seen[m["id"]] = (o.get("timestamp"), m.get("model"), m["usage"])
    print(f"hook_attachments={hooks} record_types={dict(types)}")
    print("ts,model,input_tokens,cache_creation_input_tokens,cache_read_input_tokens,output_tokens,context_tokens")
    for ts, model, u in seen.values():
        ctx = sum(u.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        print(f"{ts},{model},{u.get('input_tokens', 0)},{u.get('cache_creation_input_tokens', 0)},"
              f"{u.get('cache_read_input_tokens', 0)},{u.get('output_tokens', 0)},{ctx}")


if __name__ == "__main__":
    main()
