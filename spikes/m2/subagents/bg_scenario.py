"""Main launches a foreground subagent, a background subagent, and a background Bash, then keeps
working with foreground Bash calls so background completions arrive while it runs."""



def texts(message):
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def first_prompt(body):
    for m in body.get("messages", []):
        if m.get("role") == "user":
            return "\n".join(texts(m))
    return ""


def steps(body):
    return sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")


def tool(i, name, inp):
    return {"type": "tool_use", "id": f"toolu_{name}_{i}", "name": name, "input": inp}


def reply(body):
    first = first_prompt(body)
    n = steps(body)
    if "SUB-FG" in first:
        if n == 0:
            return "sub-fg", [tool(n, "Bash", {"command": "sleep 1", "description": "fg sub work"})], "tool_use"
        return "sub-fg", [{"type": "text", "text": "fg subagent result"}], "end_turn"
    if "SUB-BG" in first:
        if n == 0:
            return "sub-bg", [tool(n, "Bash", {"command": "sleep 6", "description": "bg sub work"})], "tool_use"
        return "sub-bg", [{"type": "text", "text": "bg subagent result"}], "end_turn"
    if "MAIN-RUN" in first:
        if n == 0:
            return "main", [
                tool(n, "Agent", {"description": "fg agent", "prompt": "SUB-FG do the foreground thing",
                                  "subagent_type": "general-purpose"}),
                tool(n + 100, "Agent", {"description": "bg agent", "prompt": "SUB-BG do the background thing",
                                        "subagent_type": "general-purpose", "run_in_background": True}),
                tool(n + 200, "Bash", {"command": "sleep 4", "description": "bg bash", "run_in_background": True}),
            ], "tool_use"
        if n <= 4:
            return "main", [tool(n, "Bash", {"command": "sleep 5", "description": f"main wait {n}"})], "tool_use"
        return "main", [{"type": "text", "text": "main done"}], "end_turn"
    return "side", [{"type": "text", "text": "side"}], "end_turn"
