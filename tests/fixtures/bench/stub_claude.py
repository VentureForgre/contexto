"""A stub ``claude`` for the benchmark's tests in CI, where Claude Code is not installed.

It speaks enough of ``claude -p --input-format stream-json --output-format stream-json`` to run a
benchmark session directly or under ``contexto run``:

- ``--version`` prints a Claude Code version line.
- It reads ``--settings=<file>`` (``env.ANTHROPIC_BASE_URL`` and ``permissions.allow``) and
  ``$CLAUDE_CONFIG_DIR/settings.json`` (``permissions.allow`` and ``apiKeyHelper``), starts the stdio
  MCP servers of ``--mcp-config=<file>`` and lists their tools as ``mcp__<server>__<tool>``.
- Each stdin line is a user turn. It sends Claude Code-shaped streaming Messages requests to its base
  URL (system, tools, the whole history, ``max_tokens`` 32000, the session id header), runs the tool
  calls of the reply (``Read`` from the working directory when a rule allows it, MCP tools through
  their server) and repeats until a reply has no tool call, then prints a ``result`` event.
- The key comes from the ``apiKeyHelper`` (run with ``/bin/sh -c``, sent as ``X-Api-Key`` and
  ``Authorization: Bearer``) or from ``ANTHROPIC_API_KEY`` (``X-Api-Key`` only), as Claude Code does.
- With ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` set it compacts when the last response's usage reaches
  the window minus 33,000: one summary request, the history replaced by the summary, and a
  ``compact_boundary`` event with ``compact_metadata.pre_tokens``. Three compactions in three
  consecutive requests end the turn with the "Autocompact is thrashing" error.
- With ``CLAUDE_CODE_GATEWAY_HINT_HEADERS`` set it sends ``x-claude-code-request-class`` (``main`` or
  ``compaction``) and ``x-claude-code-compaction: auto`` on the summary request.

What it does not imitate: Claude Code's real system prompt and tool list (its head is a few hundred
bytes, not tens of thousands of tokens), tool search and deferred tools, thinking, permission prompts
beyond the allow list, retries with backoff (it retries a 429, 529 or 5xx three times at once), side
requests such as title generation, subagents, hooks, session files, prompt cache breakpoints, the
estimate Claude Code adds to the last usage before it decides to compact, and every flag not named
above (they are accepted and ignored).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

VERSION = "2.1.322"
RESERVE = 33_000
THRASHING = ("Autocompact is thrashing: the context refilled to the limit within 3 turns of the previous "
             "compact, 3 times in a row.")
SUMMARY_HEAD = ("This session is being continued from a previous conversation that ran out of context. The "
                "summary below covers the earlier portion of the conversation.\n")
READ = {"name": "Read", "description": "Read a file from the working directory.",
        "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def load(path: str | None) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class McpServer:
    """One stdio MCP server: newline-delimited JSON-RPC."""

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.proc = subprocess.Popen([spec["command"], *spec.get("args", [])],
                                     env={**os.environ, **spec.get("env", {})},
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.next_id = 0
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "claude-code", "version": VERSION}})
        self.notify("notifications/initialized")
        self.tools = self.call("tools/list", {}).get("tools") or []

    def notify(self, method: str) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == self.next_id:
                return message.get("result") or {"isError": True, "content": [{"type": "text", "text": "error"}]}
        return {"isError": True, "content": [{"type": "text", "text": "the server exited"}]}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


class Session:
    def __init__(self, args: dict[str, str]) -> None:
        flag = load(args.get("--settings"))
        user = load(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), "settings.json"))
        self.base = ((flag.get("env") or {}).get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
                     or "http://127.0.0.1:9").rstrip("/")
        self.allow = [*((user.get("permissions") or {}).get("allow") or []),
                      *((flag.get("permissions") or {}).get("allow") or [])]
        self.helper = user.get("apiKeyHelper")
        self.key: str | None = None
        self.model = args.get("--model", "stub")
        self.session = str(uuid.uuid4())
        servers = load(args.get("--mcp-config")).get("mcpServers") or {}
        self.servers = [McpServer(name, spec) for name, spec in servers.items()]
        self.tools = [READ] + [{"name": f"mcp__{s.name}__{t['name']}", "description": t.get("description", ""),
                                "input_schema": t.get("inputSchema") or {"type": "object"}}
                               for s in self.servers for t in s.tools]
        self.messages: list[dict[str, Any]] = []
        self.last_usage = 0
        self.rapid = 0  # compactions in consecutive requests
        window = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "")
        self.trigger = int(window) - RESERVE if window.isdecimal() else None
        self.hints = bool(os.environ.get("CLAUDE_CODE_GATEWAY_HINT_HEADERS"))

    def headers(self, request_class: str) -> dict[str, str]:
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
                   "user-agent": f"claude-cli/{VERSION} (external, sdk-cli)", "accept": "text/event-stream",
                   "x-claude-code-session-id": self.session, "x-app": "cli"}
        if self.helper:
            if self.key is None:  # Claude Code keeps a helper's key for five minutes
                self.key = subprocess.run(["/bin/sh", "-c", self.helper], capture_output=True, text=True).stdout.strip()
            headers.update({"x-api-key": self.key, "authorization": f"Bearer {self.key}"})
        elif os.environ.get("ANTHROPIC_API_KEY"):
            headers["x-api-key"] = os.environ["ANTHROPIC_API_KEY"]
        if self.hints:
            headers["x-claude-code-request-class"] = request_class
            if request_class == "compaction":
                headers["x-claude-code-compaction"] = "auto"
        return headers

    def request(self, messages: list[dict[str, Any]],
                request_class: str = "main") -> tuple[list[dict[str, Any]], dict[str, int]]:
        """One streaming Messages request: the reply's content blocks and its usage. Raises
        ``urllib.error.HTTPError`` for a status that is not retried, or still failing after three tries."""
        body = json.dumps({"model": self.model, "max_tokens": 32_000, "stream": True,
                           "system": [{"type": "text", "text": "You are a coding assistant.\n"
                                                               f"Working directory: {os.getcwd()}\n"}],
                           "tools": self.tools, "messages": messages,
                           "metadata": {"user_id": json.dumps({"session_id": self.session})}}).encode("utf-8")
        for attempt in range(4):
            req = urllib.request.Request(self.base + "/v1/messages", data=body, headers=self.headers(request_class),
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=120) as response:
                    return parse_sse(response)
            except urllib.error.HTTPError as exc:
                exc.read()
                if exc.code not in (429, 529) and exc.code < 500 or attempt == 3:
                    raise
        raise AssertionError("unreachable")

    def tool(self, call: dict[str, Any]) -> dict[str, Any]:
        name, args = call["name"], call.get("input") or {}
        allowed = any(rule == name or (rule.endswith("__") and name.startswith(rule)) for rule in self.allow)
        if not allowed:
            text, error = f"Claude requested permissions to use {name}, but you haven't granted it yet.", True
        elif name == "Read":
            try:
                text, error = Path(str(args.get("file_path"))).read_text(encoding="utf-8"), False
            except OSError as exc:
                text, error = f"File does not exist. ({type(exc).__name__})", True
        else:
            server = next((s for s in self.servers if name.startswith(f"mcp__{s.name}__")), None)
            if server is None:
                text, error = f"No such tool available: {name}", True
            else:
                result = server.call("tools/call", {"name": name.split("__", 2)[2], "arguments": args})
                text = "\n".join(b.get("text", "") for b in result.get("content") or [] if isinstance(b, dict))
                error = bool(result.get("isError"))
        result = {"type": "tool_result", "tool_use_id": call["id"], "content": text}
        return {**result, "is_error": True} if error else result

    def compact(self) -> bool:
        """Summarize and replace the history when the last usage reached the trigger. False when
        auto-compaction is thrashing."""
        if self.trigger is None or self.last_usage < self.trigger or not self.messages:
            self.rapid = 0
            return True
        self.rapid += 1
        if self.rapid >= 3:
            return False
        pre_tokens = self.last_usage
        emit({"type": "system", "subtype": "status", "status": "compacting", "session_id": self.session})
        blocks, _ = self.request([*self.messages, {"role": "user", "content": [
            {"type": "text", "text": "Summarize the conversation so far."}]}], "compaction")
        summary = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        # The newest user message stays: it is the turn being served.
        tail = [m for m in self.messages[-1:] if m.get("role") == "user"]
        self.messages = [{"role": "user", "content": [{"type": "text", "text": SUMMARY_HEAD + summary}]},
                         {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]}, *tail]
        self.last_usage = 0
        emit({"type": "system", "subtype": "compact_boundary", "session_id": self.session,
              "compact_metadata": {"trigger": "auto", "pre_tokens": pre_tokens}})
        return True

    def turn(self, message: dict[str, Any]) -> bool:
        """Serve one user turn. False when the session must end (an error result was printed)."""
        self.messages.append({"role": "user", "content": message.get("content")})
        requests = 0
        while True:
            if not self.compact():
                emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": THRASHING,
                      "num_turns": requests, "session_id": self.session})
                return False
            try:
                blocks, usage = self.request(self.messages)
            except urllib.error.HTTPError as exc:
                emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": requests,
                      "result": f"API Error: {exc.code}", "session_id": self.session})
                return exc.code not in (401, 403)
            except (OSError, ValueError) as exc:
                emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": requests,
                      "result": f"API Error: {type(exc).__name__}", "session_id": self.session})
                return True
            requests += 1
            self.last_usage = sum(usage.values())
            self.messages.append({"role": "assistant", "content": blocks})
            emit({"type": "assistant", "session_id": self.session,
                  "message": {"role": "assistant", "model": self.model, "content": blocks, "usage": usage}})
            calls = [b for b in blocks if b.get("type") == "tool_use"]
            if not calls:
                emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": requests, "usage": usage,
                      "result": "".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
                      "session_id": self.session})
                return True
            results = [self.tool(call) for call in calls]
            self.messages.append({"role": "user", "content": results})
            emit({"type": "user", "session_id": self.session, "message": {"role": "user", "content": results}})


def parse_sse(response: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, str] = {}
    usage: dict[str, int] = {}
    ended = False
    for raw in response:
        line = raw.decode("utf-8", "replace")
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        kind = event.get("type")
        if kind == "message_start":
            usage.update({k: v for k, v in (event["message"].get("usage") or {}).items() if isinstance(v, int)})
        elif kind == "content_block_start":
            blocks[event["index"]] = dict(event["content_block"])
        elif kind == "content_block_delta":
            delta = event["delta"]
            if delta.get("type") == "text_delta":
                blocks[event["index"]]["text"] += delta["text"]
            elif delta.get("type") == "input_json_delta":
                partial[event["index"]] = partial.get(event["index"], "") + delta["partial_json"]
        elif kind == "message_delta":
            usage.update({k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, int)})
        elif kind == "message_stop":
            ended = True
    if not ended:
        raise ValueError("the stream ended early")
    for index, text in partial.items():
        blocks[index]["input"] = json.loads(text or "{}")
    return [blocks[i] for i in sorted(blocks)], usage


def main(argv: list[str]) -> int:
    if argv in (["--version"], ["-v"]):
        print(f"{VERSION} (Claude Code)")
        return 0
    args: dict[str, str] = {}
    for index, arg in enumerate(argv):
        if arg.startswith("--") and "=" in arg:
            name, value = arg.split("=", 1)
            args[name] = value
        elif arg.startswith("--") and index + 1 < len(argv) and not argv[index + 1].startswith("-"):
            args[arg] = argv[index + 1]
    record = os.environ.get("STUB_CLAUDE_ARGV_LOG")  # tests read the command lines the stub was started with
    if record:
        with open(record, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\n")
    session = Session(args)
    emit({"type": "system", "subtype": "init", "session_id": session.session, "model": session.model,
          "tools": [t["name"] for t in session.tools], "cwd": os.getcwd()})
    code = 0
    try:
        for line in sys.stdin:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "user" and not session.turn(event.get("message") or {}):
                code = 1
                break
    finally:
        for server in session.servers:
            server.close()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
