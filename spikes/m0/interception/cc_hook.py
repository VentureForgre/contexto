#!/usr/bin/env python3
"""H2 probe hooks. Logs event name, input field names, source/trigger. Injects synthetic
markers: SessionStart/UserPromptSubmit via additionalContext, PreCompact via plain stdout.
PRECOMPACT_BLOCK=1 makes PreCompact exit 2 (block)."""
import json
import os
import sys

data = json.load(sys.stdin)
event = data.get("hook_event_name", "?")
with open(sys.argv[1], "a") as f:  # log path passed by the generated hook config
    f.write(json.dumps({"event": event, "keys": sorted(data), "source": data.get("source"),
                        "trigger": data.get("trigger")}) + "\n")
if event == "PreCompact":
    if os.environ.get("PRECOMPACT_BLOCK") == "1":
        print("blocked by probe", file=sys.stderr)
        sys.exit(2)
    print("Keep the token MARKER-PRECOMPACT-STEER in the summary.")
elif event in ("SessionStart", "UserPromptSubmit"):
    tag = {"SessionStart": "SS", "UserPromptSubmit": "UPS"}[event]
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                             "additionalContext": f"Context marker: MARKER-{tag}-{(data.get('source') or 'x').upper()}"}}))
