#!/usr/bin/env python3
"""Hook for the zero-spend probe (UserPromptSubmit or PostToolUse): injects a short synthetic
additionalContext that differs per call, the way per-turn context hooks do."""
import json
import sys
import time

event = json.load(sys.stdin).get("hook_event_name", "UserPromptSubmit")
print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                         "additionalContext": f"Synthetic {event} context {time.time_ns() % 100000}"}}))
