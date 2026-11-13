"""A recording Claude Code hook for the D70 spike.

    python3 hook_record.py <log.jsonl>

Appends one JSON line: the hook's stdin (the event JSON, verbatim), its whole environment, argv, pid,
parent pid and working directory. When ``CLAUDE_ENV_FILE`` is set it also appends
``export D70_FROM_ENV_FILE=<session_id>`` to that file, to see which later processes pick it up.
Prints nothing and exits 0, so it never changes what Claude Code does.
"""

import json
import os
import sys
import time

raw = sys.stdin.read()
try:
    event = json.loads(raw)
except ValueError:
    event = {"_unparsed": raw}
env_file = os.environ.get("CLAUDE_ENV_FILE")
wrote = False
if env_file and isinstance(event, dict) and event.get("session_id"):
    try:
        with open(env_file, "a") as f:
            f.write(f"export D70_FROM_ENV_FILE={event['session_id']}\n")
        wrote = True
    except OSError:
        pass
with open(sys.argv[1], "a") as f:
    f.write(json.dumps({"t": time.time(), "pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(),
                        "argv": sys.argv, "stdin": event, "wrote_env_file": wrote, "env": dict(os.environ)}) + "\n")
