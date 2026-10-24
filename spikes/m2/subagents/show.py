"""Print the shapes of captured requests: per message, role and a short view of each block."""

import json
import sys
from pathlib import Path


def short(text, n=300):
    text = text.replace("\n", "\\n")
    return text if len(text) <= n else text[:n] + f"...(+{len(text) - n})"


def block_view(b):
    if isinstance(b, str):
        return f"str:{short(b)}"
    t = b.get("type")
    if t == "text":
        return f"text:{short(b['text'])}"
    if t == "tool_use":
        return f"tool_use[{b['id']}] {b['name']} {short(json.dumps(b['input']), 200)}"
    if t == "tool_result":
        c = b.get("content")
        inner = c if isinstance(c, str) else " | ".join(block_view(x) for x in c or [])
        return f"tool_result[{b['tool_use_id']}] {short(inner, 600)}"
    return t


cap = Path(sys.argv[1])
start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
for f in sorted(cap.glob("*.json")):
    n = int(f.name[:3])
    if n < start:
        continue
    body = json.loads(f.read_text())["body"]
    msgs = body.get("messages", [])
    print(f"=== {f.name}: {len(msgs)} messages")
    skip = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    for i, m in enumerate(msgs):
        if i < skip:
            continue
        content = m.get("content")
        blocks = [content] if isinstance(content, str) else content
        if m.get("role") == "system":
            print(f"  [{i}] system ({len(json.dumps(m))} chars)")
            continue
        print(f"  [{i}] {m.get('role')}: " + " || ".join(block_view(b) for b in blocks or []))
