"""The synthetic client of a replay: requests in the shape Claude Code sends, with none of its text.

Shape. Each request has ``model``, the cumulative ``messages``, ``system`` (text blocks with a cache
breakpoint), ``tools``, ``metadata`` (a session id), ``max_tokens`` and ``stream``; the first user
message opens with a ``<system-reminder>`` context block; the newest message's last block carries the
moving cache breakpoint; a tool result goes back as a user message. There is one request per model
round, and the client answers every tool call itself.

Text. The system prompt, the context block and the tool descriptions are filler generated here from
a fixed word list (``filler``), and each says so in its first sentence. They are not Claude Code's
system prompt or tool descriptions, which are not ours to ship. Their sizes are chosen so that the
head is as large as a real one: about 64,000 bytes, which the fake upstream prices at 21,277
tokens. The fixed overhead measured for Claude Code 2.1.314 with tool search on was 18,641 to 27,925
tokens (docs/DECISIONS.md, D55; ``contexto.launcher.ASSUMED_OVERHEAD_DEFAULT``).

Contexto's own five MCP tools are in the tool list with their real names, descriptions and schemas
(``contexto.mcp.TOOLS``), named as Claude Code names an MCP tool, because the proxy checks that the
main conversation has them.

Tools the client runs:

- ``read_file``: answers with the file's content from the script (``Script.files``).
- ``mcp__contexto__contexto_search``: answers with the reply of Contexto's MCP server
  (``contexto.mcp.Server``) for the session's store, called in this process, so the search and the
  reply text are the ones a real session gets.

The client connects to 127.0.0.1 only and sends no key.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import random
from typing import Any

from contexto.bench.model import Script
from contexto.bench.replay.scripted import READ_TOOL, SEARCH_TOOL

LOOPBACK = "127.0.0.1"
MODEL = "contexto-bench-replay"  # the model id the client sends; no model has this name
USER_AGENT = "contexto-bench-replay"
MAX_TOKENS = 32_000
TIMEOUT_S = 120
CACHE = {"type": "ephemeral"}
SYSTEM_BYTES = 24_000
CONTEXT_BYTES = 3_000
TOOL_DESCRIPTION_BYTES = 1_900
MCP_PREFIX = "mcp__contexto__"
SYNTHETIC_TOOLS = (
    (READ_TOOL, "file_path", "Path of the file to read, relative to the working directory"),
    ("write_file", "file_path", "Path of the file to write, relative to the working directory"),
    ("edit_file", "file_path", "Path of the file to change, relative to the working directory"),
    ("list_files", "path", "Directory to list, relative to the working directory"),
    ("find_files", "pattern", "A file name pattern"),
    ("search_text", "pattern", "A regular expression to look for in the files"),
    ("run_command", "command", "The command line to run"),
    ("read_output", "task_id", "Identifier of a command still running"),
    ("stop_command", "task_id", "Identifier of a command still running"),
    ("fetch_page", "url", "Address of the page to fetch"),
    ("search_web", "query", "What to look for"),
    ("write_todos", "items", "The task list, one task per line"),
    ("ask_user", "question", "The question to put to the user"),
    ("start_task", "prompt", "What the task is to do"),
    ("read_notebook", "notebook_path", "Path of the notebook to read"),
    ("edit_notebook", "notebook_path", "Path of the notebook to change"),
)
_WORDS = (
    "anchor", "basket", "candle", "dapple", "eleven", "fabric", "garden", "hammer", "island", "jacket", "kettle",
    "ladder", "magnet", "napkin", "orchid", "pencil", "quiver", "ribbon", "saddle", "tablet", "umbrella", "velvet",
    "walnut", "yellow", "zipper", "bridge", "copper", "damper", "engine", "funnel", "gravel", "hinges", "indigo",
    "jigsaw", "lantern", "mortar", "nozzle", "outlet", "piston", "rudder",
)


class ReplayError(RuntimeError):
    """The replay could not go on: the proxy or the fake upstream answered something unexpected."""


def filler(label: str, nbytes: int) -> str:
    """Exactly ``nbytes`` ASCII characters of generated sentences, the same for the same arguments."""
    rng = random.Random(f"contexto-bench/replay/filler/{label}")
    parts: list[str] = []
    size = 0
    n = 0
    while size < nbytes:
        n += 1
        sentence = f"Filler sentence {n}: " + " ".join(rng.choice(_WORDS) for _ in range(12)) + "."
        parts.append(sentence)
        size += len(sentence) + 1
    return "\n".join(parts)[:nbytes]


def system_blocks() -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": "This is the synthetic system prompt of `contexto bench --replay`. It stands in for "
                                 "a coding assistant's system prompt so that requests have a head of realistic size. "
                                 "It is generated filler and is not the system prompt of any product.",
         "cache_control": CACHE},
        {"type": "text", "text": filler("system", SYSTEM_BYTES), "cache_control": CACHE},
    ]


def synthetic_tools() -> list[dict[str, Any]]:
    """The client's own tool definitions: generated descriptions, one string parameter each."""
    tools = []
    for name, parameter, meaning in SYNTHETIC_TOOLS:
        description = (f"Synthetic tool definition of the Contexto benchmark replay ({name}); the description below "
                       "is generated filler.\n" + filler(f"tool/{name}", TOOL_DESCRIPTION_BYTES))
        tools.append({"name": name, "description": description,
                      "input_schema": {"type": "object",
                                       "properties": {parameter: {"type": "string", "description": meaning}},
                                       "required": [parameter], "additionalProperties": False}})
    return tools


def contexto_tools() -> list[dict[str, Any]]:
    """Contexto's MCP tools as Claude Code lists them: ``mcp__contexto__<name>``, the server's own
    description and schema."""
    from contexto.mcp import TOOLS

    return [{"name": MCP_PREFIX + tool["name"], "description": tool["description"],
             "input_schema": tool["inputSchema"]} for tool in TOOLS]


def context_block() -> dict[str, Any]:
    text = ("<system-reminder>\nSynthetic context block of the Contexto benchmark replay. It stands in for the "
            "context a coding assistant's client attaches to the first message. It is generated filler.\n"
            + filler("context", CONTEXT_BYTES) + "\n</system-reminder>")
    return {"type": "text", "text": text}


def synthetic_digest() -> str:
    """SHA-256 of everything the client generates itself (system prompt, context block, its own tool
    definitions), which the trace pins. Contexto's MCP tool definitions are not in it: they come from
    the installed ``contexto.mcp``, as a real client would get them."""
    text = json.dumps({"system": system_blocks(), "context": context_block(), "tools": synthetic_tools()},
                      sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def head(session_id: str) -> dict[str, Any]:
    """The request fields other than ``model`` and ``messages``."""
    return {"system": system_blocks(), "tools": [*synthetic_tools(), *contexto_tools()],
            "metadata": {"user_id": json.dumps({"session_id": session_id})},
            "max_tokens": MAX_TOKENS, "stream": True}


def request_body(fields: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """One request: the head and the messages, the newest message's last block with the cache
    breakpoint. ``messages`` is not changed."""
    last = messages[-1]
    blocks = [dict(block) for block in last["content"]]
    blocks[-1]["cache_control"] = CACHE
    return {"model": MODEL, "messages": [*messages[:-1], {**last, "content": blocks}], **fields}


def user_message(text: str, first: bool) -> dict[str, Any]:
    content = [context_block()] if first else []
    return {"role": "user", "content": [*content, {"type": "text", "text": text}]}


def error_text(raw: bytes) -> str:
    """An error reply in a few words: ``(type: message)`` for the API's error shape, else its start."""
    try:
        error = json.loads(raw).get("error")
        return f"({error['type']}: {error['message']})"
    except (ValueError, AttributeError, TypeError, KeyError):
        return f"({raw[:300].decode('utf-8', 'replace')})"


def parse_stream(raw: bytes) -> tuple[list[dict[str, Any]], str | None]:
    """The content blocks and the stop reason of a streamed Messages reply."""
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, str] = {}
    stop = None
    for event in raw.decode("utf-8").split("\n\n"):
        data = "".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if not data:
            continue
        payload = json.loads(data)
        kind = payload.get("type")
        if kind == "content_block_start":
            blocks[payload["index"]] = dict(payload["content_block"])
            partial[payload["index"]] = ""
        elif kind == "content_block_delta":
            delta = payload["delta"]
            if delta.get("type") == "text_delta":
                blocks[payload["index"]]["text"] += delta["text"]
            elif delta.get("type") == "input_json_delta":
                partial[payload["index"]] += delta["partial_json"]
        elif kind == "content_block_stop":
            block = blocks[payload["index"]]
            if block.get("type") == "tool_use":
                block["input"] = json.loads(partial[payload["index"]] or "{}")
        elif kind == "message_delta":
            stop = payload["delta"].get("stop_reason")
    return [blocks[i] for i in sorted(blocks)], stop


class Client:
    """Sends the session's requests to the proxy and runs the tool calls."""

    def __init__(self, port: int, session_id: str, script: Script, mcp_store: Any, host: str = LOOPBACK) -> None:
        if host != LOOPBACK:
            raise ValueError(f"the replay's client connects to {LOOPBACK} only, never {host!r}")
        from contexto.mcp import Server

        self.host = host
        self.port = port
        self.session_id = session_id
        self.script = script
        self.fields = head(session_id)
        self.server = Server(mcp_store, io.StringIO(), io.StringIO())
        self._rpc = 0
        self.reads = 0
        self.searches = 0
        # What a client does when it starts an MCP server: the handshake and the tool list.
        self._call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": USER_AGENT, "version": "1"}})
        self._call("tools/list", {})

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._rpc += 1
        response = self.server.handle_line(json.dumps({"jsonrpc": "2.0", "id": self._rpc, "method": method,
                                                       "params": params}))
        if not isinstance(response, dict) or "result" not in response:
            raise ReplayError(f"Contexto's MCP server did not answer {method}")
        return response["result"]

    def send(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One model round: POST the request, return the reply's content blocks."""
        body = json.dumps(request_body(self.fields, messages), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT_S)
        try:
            connection.request("POST", "/v1/messages", body=body, headers={
                "Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": USER_AGENT,
                "anthropic-version": "2023-06-01", "X-Claude-Code-Session-Id": self.session_id})
            response = connection.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            # The errno names the cause (no free local port, under load) and no path or host.
            detail = f" [Errno {exc.errno}] {exc.strerror}" if isinstance(exc, OSError) and exc.errno else ""
            raise ReplayError(f"the request to the proxy failed: {type(exc).__name__}{detail}") from exc
        finally:
            connection.close()
        if response.status != 200:
            raise ReplayError(f"the proxy answered {response.status} {error_text(raw)}")
        content, _ = parse_stream(raw)
        if not content:
            raise ReplayError("the proxy's reply had no content")
        return content

    def tool_result(self, call: dict[str, Any]) -> dict[str, Any]:
        """The ``tool_result`` block for one tool call."""
        name, arguments = call.get("name"), call.get("input") or {}
        if name == READ_TOOL:
            path = arguments.get("file_path")
            if path in self.script.files:
                self.reads += 1
                return {"type": "tool_result", "tool_use_id": call["id"], "content": self.script.files[path]}
            return {"type": "tool_result", "tool_use_id": call["id"], "content": f"no such file: {path}",
                    "is_error": True}
        if name == SEARCH_TOOL:
            self.searches += 1
            result = self._call("tools/call", {"name": name.removeprefix(MCP_PREFIX), "arguments": arguments})
            block = {"type": "tool_result", "tool_use_id": call["id"], "content": result.get("content") or []}
            if result.get("isError"):
                block["is_error"] = True
            return block
        return {"type": "tool_result", "tool_use_id": call["id"], "content": f"the replay has no tool {name}",
                "is_error": True}
