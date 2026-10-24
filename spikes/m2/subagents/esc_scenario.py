"""Scenarios for the D40 capture.

MODE=esc:    main calls Agent with run_in_background false; the subagent runs a 30 s Bash, during
             which the interactive driver presses Esc and then types a new prompt.
MODE=fg:     main calls Agent with run_in_background false; the subagent finishes; main ends.
MODE=bgidle: main launches a background Agent (20 s Bash) and ends its turn; the driver types a new
             prompt while it runs.
MODE=stop:   main launches a background Agent (30 s Bash), then stops it with TaskStop, then ends;
             the stop's notification arrives while main is idle.
"""

import os
import re

from bg_scenario import first_prompt, steps, tool

MODE = os.environ.get("SCEN_MODE", "esc")


def agent_id(body):
    for m in body["messages"]:
        for b in m.get("content") if isinstance(m.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                text = b["content"] if isinstance(b["content"], str) else " ".join(
                    x.get("text", "") for x in b["content"])
                found = re.search(r"agentId: (\w+)", text)
                if found:
                    return found.group(1)
    return "unknown"


def reply(body):
    first = first_prompt(body)
    n = steps(body)
    if "SUB-" in first:
        if n == 0:
            secs = {"esc": "30", "fg": "1", "bgidle": "20", "stop": "30"}[MODE]
            return "sub", [tool(n, "Bash", {"command": f"sleep {secs}", "description": "sub work"})], "tool_use"
        return "sub", [{"type": "text", "text": "subagent result"}], "end_turn"
    if "MAIN-RUN" in first:
        if n == 0:
            inp = {"description": "the agent", "prompt": f"SUB-{MODE.upper()} task", "subagent_type": "general-purpose",
                   "run_in_background": MODE in ("bgidle", "stop")}
            return "main", [tool(n, "Agent", inp)], "tool_use"
        if MODE == "stop" and n == 1:
            return "main", [tool(n, "TaskStop", {"task_id": agent_id(body)})], "tool_use"
        return "main", [{"type": "text", "text": f"main reply {n}"}], "end_turn"
    return "side", [{"type": "text", "text": "side"}], "end_turn"
