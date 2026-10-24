"""Resume a finished background agent with SendMessage, twice (D40 round 5).

Main launches a background Agent; the subagent runs a 1 s Bash and answers. Main waits with a 6 s Bash
(the stop notice arrives during its loop), then sends the agent a message with SendMessage together
with another 6 s Bash, and repeats that once more, so the capture shows how a resumed run's requests
and stop notices look.
"""

import re

from bg_scenario import first_prompt, steps, tool


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


def last_is_tool_result(body):
    for m in reversed(body["messages"]):
        if m.get("role") == "system":
            continue
        content = m.get("content")
        return isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def reply(body):
    first = first_prompt(body)
    n = steps(body)
    if "SUB-RESUME" in first:
        if last_is_tool_result(body):
            return "sub", [{"type": "text", "text": f"subagent report after {n} steps"}], "end_turn"
        return "sub", [tool(n, "Bash", {"command": "sleep 1", "description": "sub work"})], "tool_use"
    if "MAIN-RUN" in first:
        if n == 0:
            return "main", [tool(n, "Agent", {"description": "the agent", "prompt": "SUB-RESUME task",
                                              "subagent_type": "general-purpose", "run_in_background": True})], "tool_use"
        if n in (1, 3, 5):
            return "main", [tool(n, "Bash", {"command": "sleep 6", "description": f"wait {n}"})], "tool_use"
        if n in (2, 4):
            return "main", [tool(n, "SendMessage", {"to": agent_id(body), "summary": "follow-up",
                                                    "message": f"RESUME-{n} also check Y"}),
                            tool(n + 100, "Bash", {"command": "sleep 6", "description": f"wait {n}b"})], "tool_use"
        return "main", [{"type": "text", "text": "main done"}], "end_turn"
    return "side", [{"type": "text", "text": "side"}], "end_turn"
