#!/usr/bin/env python3
"""Step 2: one tiny real Claude Code session against the fake upstream, to learn the session
JSONL format and location. The fake upstream answers one Read tool_use on a temp file, then text.
Prints the session file path and, per record, its type and key names (no values).

Usage: probe.py <work_root>
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402

root = Path(sys.argv[1]).resolve()
(root / "probe").mkdir(parents=True, exist_ok=True)
root = root / "probe"
cwd = root / "cwd"
cwd.mkdir(exist_ok=True)
cwd = cwd.resolve()
read_file = cwd / "notes.txt"
read_file.write_text("line one\nline two\n")
port = harness.free_port()
log = root / "upstream.jsonl"
log.unlink(missing_ok=True)
up = harness.start_upstream(HERE, port, log, "--mode", "contexto", "--tool-loop", "1",
                            "--read-path", str(read_file))
try:
    env = harness.claude_env(root, port)
    res = harness.run_claude(["-p", "Read notes.txt and say ok", "--model", "claude-opus-5-5", "--output-format", "json",
                              "--allowedTools", "Read"],
                             cwd, env, root / "run", timeout=120)
finally:
    up.terminate()
print(json.dumps({k: v for k, v in res.items() if k != "sockets"}, indent=1))
print("sockets:", res["sockets"])
for r in harness.read_log(log):
    print({k: r.get(k) for k in ("method", "path", "body_bytes", "messages", "model", "tools", "reply", "compaction")})
for f in sorted((root / "config").rglob("*.jsonl")):
    print("SESSION", f.relative_to(root), f.stat().st_size)
    for line in f.read_text().splitlines():
        r = json.loads(line)
        m = r.get("message") or {}
        c = m.get("content")
        ctypes = [b.get("type") for b in c] if isinstance(c, list) else type(c).__name__
        print(" ", r.get("type"), r.get("subtype", ""), sorted(r.keys()), ctypes)
