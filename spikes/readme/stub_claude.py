"""A stand-in for Claude Code, for the README recording. It is not Claude Code and calls no model.

``contexto run ... claude`` starts whatever ``claude`` is first on ``PATH``; the recorder puts a
two-line wrapper for this file there. The launcher passes ``--settings=<file>`` (the proxy's address
and the permission rules) and ``--mcp-config=<file>`` (how to start ``contexto mcp`` for the session),
as it does for Claude Code. The stub then does what Claude Code does with them, in a small way:

- starts the MCP server from the config and lists its tools (stdio, one JSON-RPC message a line);
- sends each turn of ``demo_script`` to the proxy as a Messages API request holding the whole
  history, with the MCP tools named ``mcp__contexto__<tool>`` in ``tools``;
- runs a tool call the reply asks for through the MCP server and sends the result back;
- runs one shell command in the session, as a user does with ``!`` in Claude Code.

Every line it prints starts with ``[stub claude]``. The unlabelled lines after a "runs" line (the shell
command, one labelled row per line of it) and after a "which returns:" line (a tool call) are the real
tool's bytes, passed through unchanged. The history size it prints is the bytes of the request it is
about to send at one token per 3.5 bytes, the rule the fake upstream counts with. Turns are counted
from 0, as Contexto counts them.

``README_DEMO_PACE`` (seconds, default 0) is a pause before each step, so a recording is readable. The
stub waits at most ``MCP_TIMEOUT`` seconds for an MCP reply, ``HTTP_TIMEOUT`` for the proxy and
``SHELL_TIMEOUT`` for the shell command, so a hang ends the session with a labelled line.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_script as script  # noqa: E402

MCP_PREFIX = "mcp__contexto__"
MCP_TIMEOUT = 30.0
HTTP_TIMEOUT = 60.0
SHELL_TIMEOUT = 30.0
# `contexto run` sets CONTEXTO_SESSION for the CLI and its shell commands. `sessions show` prints 13
# summary lines and then the opening 200 characters of each paged-out turn. The filter keeps the page
# count, the over-bound count and the page lines; the cut ends each page line after its first line.
SHOW_COLUMNS = 74
SHOW_COMMAND = ("contexto sessions show $CONTEXTO_SESSION \\\n"
                f"    | grep -E 'Pages:|Over-bound|pg_' | cut -c1-{SHOW_COLUMNS}")
SHOW_NOTE = "the turns now on disk"
FIRST_DROP_PAUSE = 1.5  # extra pause, in paces, after the first turn the upstream receives less than was sent
SYSTEM = [{"type": "text", "text": "You are a stub: a scripted stand-in for a coding agent, used to record a demo."}]


def say(text: str, end: str = "\n") -> None:
    sys.stdout.write(f"{script.STUB_LABEL} {text}{end}")
    sys.stdout.flush()


def pause(factor: float = 1.0) -> None:
    try:
        seconds = float(os.environ.get("README_DEMO_PACE") or 0)
    except ValueError:
        seconds = 0.0
    if seconds > 0:
        time.sleep(seconds * factor)


class McpServer:
    """The MCP server of the launcher's ``--mcp-config``, over stdio. A thread reads its output into a
    queue, so a reply that does not arrive within ``timeout`` seconds is an error, not a hang."""

    def __init__(self, config: dict[str, Any], timeout: float = MCP_TIMEOUT) -> None:
        env = {**os.environ, **(config.get("env") or {})}
        self.timeout = timeout
        self.proc = subprocess.Popen([config["command"], *config.get("args", [])], env=env, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8")
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 0
        try:
            self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "stub-claude", "version": "0"}})
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except (RuntimeError, OSError):
            self.close(wait=0)
            raise

    def _read(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in iter(self.proc.stdout.readline, ""):
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        self.lines.put(None)  # the server closed its output

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                line = self.lines.get(timeout=max(deadline - time.monotonic(), 0.001))
            except queue.Empty:
                raise RuntimeError(f"the MCP server did not answer {method} within {self.timeout:g} s") from None
            if line is None:
                raise RuntimeError(f"the MCP server closed its output during {method}")
            try:
                reply = json.loads(line)
            except ValueError:
                raise RuntimeError(f"the MCP server sent a line that is not JSON during {method}") from None
            if isinstance(reply, dict) and reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply["result"]

    def close(self, wait: float = 5.0) -> None:
        """Close the server's input and wait up to ``wait`` seconds for it to exit, then kill it."""
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def tool_definitions(listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The MCP tools as Claude Code names them in a request: ``mcp__<server>__<tool>``."""
    return [{"name": MCP_PREFIX + tool["name"], "description": tool.get("description", ""),
             "input_schema": tool.get("inputSchema") or {"type": "object"}} for tool in listed]


def request_body(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> bytes:
    """The wire bytes of one request: compact JSON in Claude Code's key order."""
    request = {"model": script.MODEL, "messages": messages, "system": SYSTEM, "tools": tools,
               "metadata": {"user_id": "stub-user"}, "max_tokens": 1024, "stream": True}
    return json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class Sent(NamedTuple):
    stop: str | None  # the reply's stop reason
    history: int  # tokens of the request the stub sent
    received: int  # input tokens the reply's usage reports: what reached the upstream


def parse_stream(raw: bytes) -> tuple[list[dict[str, Any]], str | None, int]:
    """(content blocks, stop reason, input tokens of the usage) of a Messages API stream."""
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, str] = {}
    stop = None
    input_tokens = 0
    for event in raw.decode("utf-8").split("\n\n"):
        data = [line[5:].strip() for line in event.splitlines() if line.startswith("data:")]
        if not data:
            continue
        payload = json.loads("\n".join(data))
        kind = payload.get("type")
        if kind == "message_start":
            usage = payload["message"].get("usage") or {}
            input_tokens = sum(int(usage.get(key) or 0) for key in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        elif kind == "content_block_start":
            blocks[payload["index"]] = dict(payload["content_block"])
        elif kind == "content_block_delta":
            delta = payload["delta"]
            if delta.get("type") == "text_delta":
                blocks[payload["index"]]["text"] += delta["text"]
            elif delta.get("type") == "input_json_delta":
                partial[payload["index"]] = partial.get(payload["index"], "") + delta["partial_json"]
        elif kind == "message_delta":
            stop = payload["delta"].get("stop_reason")
        elif kind == "error":
            raise RuntimeError(f"the stream reported an error: {payload.get('error')}")
    for index, text in partial.items():
        blocks[index]["input"] = json.loads(text)
    return [blocks[i] for i in sorted(blocks)], stop, input_tokens


def post(base_url: str, body: bytes) -> tuple[list[dict[str, Any]], str | None, int]:
    url = urlsplit(base_url)
    conn = http.client.HTTPConnection(url.hostname or "127.0.0.1", url.port or 80, timeout=HTTP_TIMEOUT)
    try:
        conn.request("POST", f"{url.path.rstrip('/')}/v1/messages", body=body, headers={
            "Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": "stub-key"})
        response = conn.getresponse()
        raw = response.read()
    finally:
        conn.close()
    if response.status != 200:
        raise RuntimeError(f"the proxy answered {response.status}: {raw[:200].decode('utf-8', 'replace')}")
    return parse_stream(raw)


def send(base_url: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], what: str) -> Sent:
    """Send the whole history and append the reply to it. Prints the history size and leaves the line
    open: the fake upstream prints what it received on the same line."""
    body = request_body(messages, tools)
    history = script.tokens(len(body))
    say(f"{what}: history {history:,} tokens -> ", end="")
    try:
        content, stop, received = post(base_url, body)
    except Exception:
        sys.stdout.write("\n")
        raise
    messages.append({"role": "assistant", "content": content})
    return Sent(stop, history, received)


def reply_text(message: dict[str, Any]) -> str:
    return " ".join(b["text"] for b in message["content"] if b.get("type") == "text")


def run_tools(mcp: McpServer, message: dict[str, Any]) -> dict[str, Any]:
    """Run the reply's tool calls through the MCP server; the user message that carries the results."""
    results = []
    for block in message["content"]:
        if block.get("type") != "tool_use":
            continue
        name = block["name"]
        if not name.startswith(MCP_PREFIX):
            raise RuntimeError(f"the stub has no tool named {name}")
        result = mcp.call("tools/call", {"name": name[len(MCP_PREFIX):], "arguments": block["input"]})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        say(f"ran {name[len(MCP_PREFIX):]} on the contexto MCP server, which returns:")
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        sys.stdout.flush()
        results.append({"type": "tool_result", "tool_use_id": block["id"],
                        "content": [{"type": "text", "text": text}], "is_error": bool(result.get("isError"))})
    return {"role": "user", "content": results}


def shell(command: str, note: str) -> None:
    """Run a shell command in the session, as ``!`` does in Claude Code. The command is printed as it is
    run, one labelled row per line of it. Its output goes straight to the terminal."""
    first, *rest = command.split("\n")
    say(f"runs ({note}): {first}")
    for line in rest:
        say(line)
    try:
        subprocess.run(command, shell=True, check=False, timeout=SHELL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the shell command did not finish within {SHELL_TIMEOUT:g} s") from None


def session(base_url: str, mcp: McpServer) -> None:
    tools = tool_definitions(mcp.call("tools/list", {})["tools"])
    messages: list[dict[str, Any]] = []
    dropped = False
    for turn in range(script.GROWTH_TURNS):
        pause()
        if turn == script.PLANT_TURN:
            say(f'turn {turn} ends with: "{script.PLANT}"')
        messages.append({"role": "user", "content": [{"type": "text", "text": script.prompt(turn)}]})
        sent = send(base_url, messages, tools, f"turn {turn}")
        if not dropped and sent.received < sent.history:
            # The line the recording is for: the history kept growing and the request did not.
            dropped = True
            pause(FIRST_DROP_PAUSE)
    pause(2)
    shell(SHOW_COMMAND, SHOW_NOTE)
    pause(5)
    turn = script.GROWTH_TURNS
    say(f'turn {turn} asks: "{script.QUESTION}"')
    messages.append({"role": "user", "content": [{"type": "text", "text": script.QUESTION}]})
    pause(1.5)
    stop = send(base_url, messages, tools, f"turn {turn}").stop
    while stop == "tool_use":
        pause(1.5)
        messages.append(run_tools(mcp, messages[-1]))
        pause(4)
        stop = send(base_url, messages, tools, f"turn {turn}").stop
    say(f'scripted reply: "{reply_text(messages[-1])}"')


def main(argv: list[str]) -> int:
    if argv in (["--version"], ["-v"]):
        print("0.0.0 (stub claude, not Claude Code)")
        return 0
    paths = {name: arg.split("=", 1)[1] for arg in argv for name in ("--settings", "--mcp-config")
             if arg.startswith(name + "=")}
    if len(paths) != 2:
        print("stub claude: run it through `contexto run ... claude`, which passes --settings and --mcp-config",
              file=sys.stderr)
        return 2
    try:
        settings = json.loads(Path(paths["--settings"]).read_text(encoding="utf-8"))
        config = json.loads(Path(paths["--mcp-config"]).read_text(encoding="utf-8"))["mcpServers"]["contexto"]
        base_url = settings["env"]["ANTHROPIC_BASE_URL"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"stub claude: cannot read the launcher's files: {exc!r}", file=sys.stderr)
        return 2
    mcp = None
    try:
        mcp = McpServer(config)
        session(base_url, mcp)
    except (RuntimeError, OSError) as exc:
        say(f"stopped: {exc}")
        return 1
    finally:
        if mcp is not None:
            mcp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
