"""A recording ``headersHelper`` for an HTTP MCP server entry, for the D70 spike.

    python3 headers_helper.py <log.jsonl>

Appends its environment, argv, stdin, pid, parent pid and working directory to the log, then prints
the headers Claude Code should send, as one JSON object: a call counter and whatever session id its
environment holds under ``CLAUDE_CODE_SESSION_ID``.
"""

import json
import os
import select
import sys
import time

log = sys.argv[1]
# Claude Code leaves the helper's stdin open, so a plain read() would block until its 10 s timeout.
ready = [] if sys.stdin.isatty() else select.select([sys.stdin], [], [], 0.3)[0]
stdin = os.read(sys.stdin.fileno(), 65536).decode("utf-8", "replace") if ready else ""
try:
    calls = sum(1 for _ in open(log))
except OSError:
    calls = 0
with open(log, "a") as f:
    f.write(json.dumps({"t": time.time(), "pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(),
                        "argv": sys.argv, "stdin": stdin, "env": dict(os.environ)}) + "\n")
print(json.dumps({"X-Probe-Helper-Call": str(calls + 1),
                  "X-Probe-Helper-Sid": os.environ.get("CLAUDE_CODE_SESSION_ID", "absent")}))
