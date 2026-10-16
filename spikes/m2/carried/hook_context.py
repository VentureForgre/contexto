#!/usr/bin/env python3
"""UserPromptSubmit hook for the zero-spend probe: injects a short synthetic additionalContext that
differs per call, the way per-turn context hooks do."""
import json
import sys
import time

json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                         "additionalContext": f"Synthetic hook context {time.time_ns() % 100000}"}}))
