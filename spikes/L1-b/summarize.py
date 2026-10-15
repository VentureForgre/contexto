#!/usr/bin/env python3
"""Summarize the stub request log and a hook log for one experiment.

Usage: summarize.py <stub requests.log> [hooks.log]
Prints hook events (name, source, trigger) and, for each POST, body size, message or
input-item structure, and synthetic MARKER-* tokens. The logs hold no other content.
"""
import json
import sys


def main() -> None:
    if len(sys.argv) > 2:
        try:
            with open(sys.argv[2]) as f:
                for line in f:
                    o = json.loads(line)
                    print(f"  hook {o['event']} source={o.get('source')} trigger={o.get('trigger')}")
        except FileNotFoundError:
            print("  hook log missing (no hook ran)")
    gets = 0
    with open(sys.argv[1]) as f:
        for line in f:
            o = json.loads(line)
            if o["method"] != "POST":
                gets += 1
                continue
            s = o.get("shape") or {}
            structure = s.get("input_items") if "input_items" in s else s.get("roles")
            print(f"  POST {o['path']} bytes={o['body_bytes']} model={s.get('model')} "
                  f"structure={structure} markers={o['markers']}")
    print(f"  non-POST requests: {gets}")


if __name__ == "__main__":
    main()
