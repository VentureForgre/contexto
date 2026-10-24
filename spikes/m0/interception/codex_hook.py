#!/usr/bin/env python3
"""Codex H2 probe: log event and input field names; inject a synthetic marker."""
import json
import sys

data = json.load(sys.stdin)
event = data.get("hook_event_name", "?")
with open(sys.argv[1], "a") as f:  # log path passed by the generated hook config
    f.write(json.dumps({"event": event, "keys": sorted(data), "source": data.get("source"), "trigger": data.get("trigger")}) + "\n")
tags = {"SessionStart": "CX-SS", "UserPromptSubmit": "CX-UPS"}
if event in tags:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": f"Context marker: MARKER-{tags[event]}"}}))
