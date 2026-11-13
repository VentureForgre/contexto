"""The two CLIs the harness plays: each keeps its own complete history and resends all of it,
unmodified, on every request. Nothing the engine forwards is ever fed back into that history.

Shapes follow the ones measured in this repository: Claude Code 2.1.318 (``contexto.rewrite``,
``spikes/m2/subagents``) and codex-cli 0.166.0 (``contexto.codex``, ``docs/research/M2-codex-adapter.md``).
The heads are sized like measured ones (15,101 tokens for Claude Code's main conversation, 13,537 for
Codex, by the fake upstream's tokenizer); their text is filler.
"""

from __future__ import annotations

import json
from typing import Any

from inturn.scripts import AGENT_ID, Call, Conversation, Image, Step, Turn, claude_call_id, codex_call_id, png, prose

CLAUDE_VERSION = "2.1.318"
CODEX_VERSION = "0.166.0"
CLAUDE_MODEL = "claude-opus-5-5"
CODEX_MODEL = "gpt-6-astra"
MESSAGES = "/v1/messages"
RESPONSES = "/v1/responses"
SESSION_ID = "5f0c6a2e-9d41-4b7a-8c3e-1a2b3c4d5e6f"
EPHEMERAL = {"type": "ephemeral"}
CONTEXTO_TOOLS = ("contexto_recall", "contexto_pin", "contexto_close", "contexto_state", "contexto_search")
CONTEXTO_PREFIX = "mcp__contexto__"
SCREENSHOT_TOOL = "mcp__playwright__browser_take_screenshot"


def contexto_arguments(call: Call) -> dict:
    """The arguments of a Contexto tool call: a search's query, or a recall's page id and offset."""
    return {"query": call.arg} if call.tool == "contexto_search" else json.loads(call.arg)


# Claude Code ----------------------------------------------------------------------------------------

def _tool(name: str, properties: dict[str, str], size: int) -> dict:
    return {"name": name, "description": prose(f"tool/{name}", size),
            "input_schema": {"type": "object", "required": list(properties)[:1],
                             "properties": {k: {"type": v, "description": prose(f"tool/{name}/{k}", 120)}
                                            for k, v in properties.items()}}}


def claude_tools(agent: bool) -> list[dict]:
    tools = [
        _tool("Read", {"file_path": "string", "offset": "number", "limit": "number"}, 2_600),
        _tool("Bash", {"command": "string", "description": "string", "timeout": "number"}, 5_200),
        _tool("Grep", {"pattern": "string", "path": "string", "output_mode": "string"}, 2_400),
        _tool("Edit", {"file_path": "string", "old_string": "string", "new_string": "string"}, 2_200),
        _tool("Write", {"file_path": "string", "content": "string"}, 1_500),
        _tool(SCREENSHOT_TOOL, {"filename": "string", "fullPage": "boolean"}, 900),
    ]
    if not agent:  # a subagent cannot launch another
        tools.append(_tool("Agent", {"prompt": "string", "description": "string", "subagent_type": "string",
                                     "run_in_background": "boolean"}, 6_500))
    # A subagent gets the session's MCP tools too, so a stub in its request names a tool it can call.
    tools += [_tool(CONTEXTO_PREFIX + name, {"page_id": "string", "offset": "number"}, 700)
              for name in CONTEXTO_TOOLS]
    return tools


def claude_system(agent: bool) -> list[dict]:
    body = prose("system/agent" if agent else "system/main", 9_000 if agent else 15_500)
    return [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {"type": "text", "text": body, "cache_control": EPHEMERAL}]


def claude_environment(agent: bool) -> dict:
    """The session-start system message, which follows the first user message on 5.x model ids. A
    subagent's starts with the deferred-tool listing (D46)."""
    listing = ("The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded:\n"
               "WebFetch\nWebSearch\n\n") if agent else ""
    text = (f"{listing}# Environment\nYou have been invoked in the following environment:\n"
            " - Primary working directory: /work/app\n - Is a git repository: true\n - Platform: linux\n"
            " - Shell: bash\n" + prose("environment", 900))
    return {"role": "system", "content": [{"type": "text", "text": text}], "output_config": {"effort": "medium"}}


def claude_image(image: Image) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png(image)}}


def claude_tool_use(call: Call) -> dict:
    if call.tool == "read":
        name, arguments = "Read", {"file_path": call.arg}
    elif call.tool == "grep":
        name, arguments = "Grep", {"pattern": call.arg, "path": "/work/app/src", "output_mode": "content"}
    elif call.tool == "screenshot":
        name, arguments = SCREENSHOT_TOOL, {"filename": call.arg}
    elif call.tool == "agent":
        name, arguments = "Agent", {"description": "audit the store", "prompt": call.arg,
                                    "subagent_type": "general-purpose", "run_in_background": True}
    elif call.tool.startswith("contexto_"):
        name, arguments = CONTEXTO_PREFIX + call.tool, contexto_arguments(call)
    else:  # bash, and exec: Claude Code has no code mode, so the same command goes through Bash
        name, arguments = "Bash", {"command": call.arg, "description": "Run the focused tests"}
    return {"type": "tool_use", "id": claude_call_id(call.key), "name": name, "input": arguments}


def claude_tool_result(call: Call) -> dict:
    content: Any = call.result
    if call.texts:  # an MCP tool's reply arrives as its text blocks
        content = [{"type": "text", "text": text} for text in call.texts]
    elif call.images or call.tool == "agent":  # Claude Code sends these as blocks
        content = [{"type": "text", "text": call.result}, *(claude_image(i) for i in call.images)]
    return {"type": "tool_result", "tool_use_id": claude_call_id(call.key), "content": content}


def _with_breakpoint(message: dict) -> dict:
    """A copy of ``message`` with Claude Code's moving cache breakpoint on its last block."""
    content = message["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    blocks[-1] = {**blocks[-1], "cache_control": EPHEMERAL}
    return {**message, "content": blocks}


class ClaudeCode:
    """Claude Code's side of one conversation (the main one, or a subagent's)."""

    dialect = "claude"
    endpoint = MESSAGES
    call_id = staticmethod(claude_call_id)

    def __init__(self, conversation: Conversation) -> None:
        self.agent = conversation.agent
        self.system = claude_system(self.agent)
        self.tools = claude_tools(self.agent)
        self.history: list[dict] = []
        self.headers = {"User-Agent": f"claude-cli/{CLAUDE_VERSION} (external, cli)",
                        "X-Claude-Code-Session-Id": SESSION_ID, "anthropic-version": "2023-06-01"}
        if self.agent:
            self.headers["X-Claude-Code-Agent-Id"] = AGENT_ID
        self.metadata = {"user_id": json.dumps({"device_id": "0" * 64, "account_uuid": "",
                                                "session_id": SESSION_ID})}

    def prompt(self, turn: Turn) -> None:
        if self.agent:  # measured: a <system-reminder> block, then the Agent call's prompt unchanged
            content: Any = [{"type": "text", "text": "<system-reminder>\nAs you answer, you can use the following "
                                                     "context:\n# currentDate\n2026-11-12\n</system-reminder>"},
                            {"type": "text", "text": turn.prompt}]
        elif turn.images:
            content = [{"type": "text", "text": turn.prompt}, *(claude_image(i) for i in turn.images)]
        else:
            content = turn.prompt  # a typed prompt arrives as a plain string
        first = not self.history
        self.history.append({"role": "user", "content": content})
        if first:
            self.history.append(claude_environment(self.agent))

    def head(self) -> dict:
        """The head fields, the same objects on every request."""
        return {"system": self.system, "tools": self.tools}

    def request(self) -> dict:
        messages = list(self.history)
        last = next(i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "user")  # not a notice
        messages[last] = _with_breakpoint(messages[last])
        return {"model": CLAUDE_MODEL, "max_tokens": 32_000, "stream": True, "thinking": {"type": "adaptive"},
                "metadata": self.metadata, "system": self.system, "tools": self.tools, "messages": messages}

    def take(self, output: list[dict], step: Step) -> list[dict]:
        """The model's reply, then what the CLI adds after running its tool calls. Returns the items
        added."""
        added = [{"role": "assistant", "content": output}]
        if step.calls:
            results: list[dict] = [claude_tool_result(c) for c in step.calls]
            if step.reminder:  # text after the results, in their message
                results.append({"type": "text", "text": step.reminder})
            added.append({"role": "user", "content": results})
            if step.notice:
                added.append({"role": "system", "content": [{"type": "text", "text": step.notice}]})
        self.history += added
        return added

    @property
    def items(self) -> list[dict]:
        return self.history


# Codex ----------------------------------------------------------------------------------------------

def _developer(text: str) -> dict:
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def codex_lead() -> list[dict]:
    """The items before the first prompt, in the measured order (code mode: no top-level tools)."""
    functions = [{"type": "custom", "name": "exec", "description": prose("codex/exec", 3_000)},
                 {"type": "function", "name": "exec_command", "description": prose("codex/exec_command", 6_000),
                  "parameters": {"type": "object", "properties": {"cmd": {"type": "string"},
                                                                  "workdir": {"type": "string"}}}},
                 {"type": "function", "name": "view_image", "description": prose("codex/view_image", 900),
                  "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
                 {"type": "function", "name": "wait"}, {"type": "function", "name": "request_user_input"}]
    namespaces = [{"type": "namespace", "name": "functions", "description": prose("codex/functions", 400),
                   "tools": functions},
                  {"type": "namespace", "name": "collaboration", "description": prose("codex/collaboration", 400),
                   "tools": [{"type": "function", "name": "spawn_agent"}, {"type": "function", "name": "wait_agent"}]}]
    return [
        {"type": "additional_tools", "id": "at_inturn", "role": "developer", "tools": namespaces},
        _developer(prose("codex/base", 9_000)),
        {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "<skills_instructions>" + prose("codex/skills", 2_500)
             + "</skills_instructions>"},
            {"type": "input_text", "text": "<permissions instructions>" + prose("codex/permissions", 1_500)
             + "</permissions instructions>"}]},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>\n  <cwd>/work/app</cwd>\n  <shell>bash</shell>\n"
                                           "  <current_date>2026-11-12</current_date>\n</environment_context>"}]},
    ]


def codex_image(image: Image) -> dict:
    return {"type": "input_image", "image_url": "data:image/png;base64," + png(image)}


def codex_call(call: Call) -> dict:
    call_id = codex_call_id(call.key)
    if call.tool == "exec" or call.tool.startswith("contexto_"):
        # Code mode: a custom tool whose input is JavaScript. MCP tools are reached through ``tools``;
        # a call of one of Contexto's own is recognised by that literal form (contexto.store.render).
        if call.tool == "exec":
            code = f"const out = await tools.exec_command({json.dumps({'cmd': call.arg})});\ntext(out);"
        else:
            arguments = json.dumps(contexto_arguments(call), separators=(",", ":"))
            code = f"const out = await tools.{CONTEXTO_PREFIX}{call.tool}({arguments});\ntext(out);"
        return {"type": "custom_tool_call", "call_id": call_id, "name": "exec", "input": code}
    if call.tool == "read":
        name, arguments = "exec_command", {"cmd": f"sed -n '1,400p' {call.arg}", "workdir": "/work/app"}
    elif call.tool == "grep":
        name, arguments = "exec_command", {"cmd": f"rg -n {call.arg} src", "workdir": "/work/app"}
    elif call.tool == "screenshot":
        name, arguments = "view_image", {"path": call.arg}
    else:
        name, arguments = "exec_command", {"cmd": call.arg, "workdir": "/work/app"}
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": json.dumps(arguments, separators=(",", ":"))}


def codex_output(call: Call) -> dict:
    output: Any = "\n".join(call.texts) if call.texts else call.result
    if call.images:
        output = [{"type": "input_text", "text": call.result}, *(codex_image(i) for i in call.images)]
    custom = call.tool == "exec" or call.tool.startswith("contexto_")
    return {"type": "custom_tool_call_output" if custom else "function_call_output",
            "call_id": codex_call_id(call.key), "output": output}


class Codex:
    """Codex's side of one conversation: the lead, then every item, in ``input``."""

    dialect = "codex"
    endpoint = RESPONSES
    call_id = staticmethod(codex_call_id)

    def __init__(self, conversation: Conversation) -> None:
        if conversation.agent:
            raise ValueError("the subagent scenario is Claude Code only")
        self.instructions = prose("codex/instructions", 17_000)
        self.tools: list[dict] = []  # code mode: no top-level tools
        self.history: list[dict] = codex_lead()
        self.headers = {"User-Agent": f"codex_cli_rs/{CODEX_VERSION} (Linux 6.8; x86_64) xterm-256color",
                        "originator": "codex_cli_rs"}

    def prompt(self, turn: Turn) -> None:
        content = [{"type": "input_text", "text": turn.prompt}, *(codex_image(i) for i in turn.images)]
        self.history.append({"type": "message", "role": "user", "content": content})

    def head(self) -> dict:
        return {"instructions": self.instructions, "tools": self.tools}

    def request(self) -> dict:
        return {"model": CODEX_MODEL, "instructions": self.instructions, "input": list(self.history),
                "tools": self.tools,
                "tool_choice": "auto", "parallel_tool_calls": True,
                "reasoning": {"effort": "medium", "summary": "auto"}, "store": False, "stream": True,
                "include": ["reasoning.encrypted_content"], "prompt_cache_key": SESSION_ID}

    def take(self, output: list[dict], step: Step) -> list[dict]:
        added = [*output, *(codex_output(c) for c in step.calls)]
        self.history += added
        return added

    @property
    def items(self) -> list[dict]:
        return self.history


CLIS = {"claude": ClaudeCode, "codex": Codex}
