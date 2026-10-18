#!/usr/bin/env python3
"""Where does an --effort change show up? Per main request: top-level output_config and every
message that carries fields beyond role/content (settings-only messages on 2.1.301).

Usage: python3 -I spikes/m2/scale/reminders/effort_check.py <run_root>
"""
import json
import re
import sys
from pathlib import Path

seen = {}
for f in sorted(Path(sys.argv[1], "bodies").glob("*.json")):
    b = json.loads(f.read_bytes() or b"{}")
    if not b.get("tools"):
        continue
    turn = None
    for m in b["messages"]:
        c = m.get("content")
        t = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
        g = re.search(r"Turn (\d+)\. Synth", t or "")
        if g and m.get("role") == "user":
            turn = int(g.group(1))
    extras = [(i, sorted(k for k in m if k not in ("role", "content"))) for i, m in enumerate(b["messages"])
              if any(k not in ("role", "content") for k in m)]
    key = (json.dumps(b.get("output_config")), json.dumps(extras))
    if seen.get(turn) != key:
        seen[turn] = key
        print(f"turn {turn}: output_config={key[0]} messages with extra fields={key[1]}")
