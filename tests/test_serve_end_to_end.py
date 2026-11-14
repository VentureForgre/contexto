"""``contexto serve`` and ``contexto mcp --serve`` end to end with the real Claude Code client (D70).

The whole path a user sets up from ``docs/serve.md``: ``contexto serve --port 0`` runs as a child
process on a temporary ``CONTEXTO_HOME``; ``claude -p`` gets exactly what ``--print-settings`` and
``--print-mcp-config`` print (``--settings`` and ``--mcp-config``, with ``--strict-mcp-config`` so
nothing else is loaded). A fake upstream on 127.0.0.1 answers every model request as a model
would: a conversation whose prompt says ``PIN:<word>`` gets a ``contexto_pin`` tool call for that
word, after a ``ToolSearch`` call that loads it when Claude Code offers the tool deferred (tool search
is on by default in 2.1.322), and text once the pin's result is back. The pin is written by the MCP
server, which finds the session by asking the service about the call's tool-use id.

What it shows:

- (a) every model request that offers tools offers the five Contexto tools, in ``tools`` or in Claude
  Code's list of deferred tools;
- (b) two conversations through one service: each pin lands in its own conversation's store only;
- (c) one headless process (stream-json input: a turn, ``/clear``, a turn) keeps one MCP server
  process, and the pin issued after ``/clear`` lands in the new conversation's store, not the old
  one. The server's environment still names the first conversation then
  (docs/research/D70-serve-probes.md section 6), which is why a call is resolved by its tool-use id.

Zero spend: a temporary ``HOME`` and ``CLAUDE_CONFIG_DIR``, a stub API key, ``DISABLE_TELEMETRY`` and
``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC``, and on macOS a sandbox profile that denies every
outbound connection except to localhost. Skipped when the ``claude`` CLI is not installed (as in CI).
Run with ``-s`` to see the measurements it prints.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import serve
from contexto.store import SessionStore
from test_mcp_real_client import EXPECTED
from test_serve import Running, run_cli, served, sessions
from test_serve_real_client import CLAUDE, LOCAL_ONLY, SANDBOX

pytestmark = pytest.mark.skipif(CLAUDE is None, reason="claude CLI not installed")
PIN_TOOL = "mcp__contexto__contexto_pin"
SEARCH_TOOL = "ToolSearch"
FIRST = "0d700000-0000-4000-8000-0000000e2e01"
SECOND = "0d700000-0000-4000-8000-0000000e2e02"
MARK = re.compile(r"PIN:([a-z0-9]+)")
TURN_TIMEOUT_S = 180


def _sse(data: dict) -> bytes:
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode()


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return " ".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


def _blocks(message: dict, kind: str) -> list[dict]:
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict) and block.get("type") == kind] \
        if isinstance(content, list) else []


def _tool_results(message: dict) -> list[dict]:
    return _blocks(message, "tool_result")


def _offered(request: dict, tools: list[str]) -> set[str]:
    """Which of Contexto's tools the request offers: by name in ``tools``, or in Claude Code's list of
    deferred tools, which is system text (a ``system`` block or a ``<system-reminder>`` in a message)."""
    text = json.dumps(request.get("system")) + json.dumps(request.get("messages"))
    return {name for name in EXPECTED if name in tools or name in text}


def _next_block(request: dict, tools: list[str]) -> tuple[dict, str | None]:
    """What a model asked to pin would answer: (the content block, the word pinned or None)."""
    messages = [m for m in request.get("messages") or [] if isinstance(m, dict)]
    words = [found.group(1) for m in messages if m.get("role") == "user" and not _tool_results(m)
             for found in [MARK.search(_text_of(m))] if found]
    called = [block.get("name") for m in messages if m.get("role") == "assistant" for block in _blocks(m, "tool_use")]
    if words and PIN_TOOL not in called:
        if PIN_TOOL in tools:
            pin = {"kind": "decision", "text": f"decision {words[-1]}"}
            return {"type": "tool_use", "name": PIN_TOOL, "input": pin}, words[-1]
        if SEARCH_TOOL in tools and SEARCH_TOOL not in called:
            search = {"query": f"select:{PIN_TOOL}", "max_results": 1}
            return {"type": "tool_use", "name": SEARCH_TOOL, "input": search}, None
    return {"type": "text", "text": "stub reply"}, None


class Upstream(BaseHTTPRequestHandler):
    """The Messages API as far as these runs need it, streamed as the Anthropic API streams it
    (chunked, identity, with usage). ``seen`` holds one record per ``/v1/messages``: the Claude Code
    session id of its header, the tools it offered, the word it was asked to pin, the tool-use id it
    was given and the tool results it carried."""

    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    other: list[tuple[str, str]] = []
    issued = 0
    lock = threading.Lock()

    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self) -> None:  # noqa: N802
        Upstream.other.append((self.command, self.path))
        self._json(200, {})

    def do_GET(self) -> None:  # noqa: N802
        Upstream.other.append((self.command, self.path))
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub"}})

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            request = json.loads(raw or b"{}")
        except ValueError:
            request = {}
        path = self.path.split("?")[0]
        if path != "/v1/messages":
            Upstream.other.append((self.command, path))
            if path.endswith("/count_tokens"):
                return self._json(200, {"input_tokens": 100})
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub"}})
        # The last user message; 2.1.322 on a 5.x model id puts a role "system" message after it.
        users = [m for m in request.get("messages") or [] if isinstance(m, dict) and m.get("role") == "user"]
        last = users[-1] if users else {}
        tools = sorted(t.get("name") for t in request.get("tools") or [] if isinstance(t, dict))
        block, word = _next_block(request, tools)
        record = {"session": self.headers.get("X-Claude-Code-Session-Id"), "tools": tools,
                  "offered": _offered(request, tools), "pin": word, "tool_use_id": None, "call": None,
                  "results": _tool_results(last)}
        if block["type"] == "tool_use":
            with Upstream.lock:
                Upstream.issued += 1
                block = {"type": "tool_use", "id": f"toolu_01E2E{Upstream.issued:04d}serveQ7mZp4LkTr", **block}
            record.update(tool_use_id=block["id"], call=block["name"])
        Upstream.seen.append(record)
        stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
        message = {"id": f"msg_01E2E{len(Upstream.seen):04d}", "type": "message", "role": "assistant", "content": [],
                   "model": request.get("model", "m"), "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}}
        if not request.get("stream"):
            return self._json(200, {**message, "content": [block], "stop_reason": stop})
        if block["type"] == "text":
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        else:
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": start},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                   "usage": {"output_tokens": 10}},
                  {"type": "message_stop"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for event in events:
            chunk = _sse(event)
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def claude(tmp_path: Path, settings: Path, mcp_config: Path, *args: str, stdin=subprocess.DEVNULL) -> subprocess.Popen:
    """``claude -p`` with the printed files and nothing of the developer's Claude Code setup."""
    user_home, config, work = tmp_path / "claude-home", tmp_path / "claude-config", tmp_path / "work"
    for directory in (user_home, config, work):
        directory.mkdir(exist_ok=True)
    env = {"HOME": str(user_home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color",
           "CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_API_KEY": "stub-key", "DISABLE_AUTOUPDATER": "1",
           "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    argv = [CLAUDE, "-p", "--settings", str(settings), "--mcp-config", str(mcp_config), "--strict-mcp-config",
            "--model", "claude-haiku-5-5", *args]
    if sys.platform == "darwin" and os.path.exists(SANDBOX):
        profile = tmp_path / "localonly.sb"
        profile.write_text(LOCAL_ONLY)
        argv = [SANDBOX, "-f", str(profile), *argv]
    return subprocess.Popen(argv, cwd=work, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


def finish(proc: subprocess.Popen, timeout: float = TURN_TIMEOUT_S) -> tuple[int, str, str]:
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return proc.returncode, out, err


def mcp_servers_of(root: int) -> set[int]:
    """The pids of ``contexto mcp --serve`` processes descended from ``root``, from one ``ps`` listing."""
    listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True, timeout=30)
    parent, command = {}, {}
    for line in listing.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            parent[int(fields[0])], command[int(fields[0])] = int(fields[1]), fields[2]
    found = set()
    for pid, cmd in command.items():
        if "contexto mcp --serve" not in cmd:
            continue
        ancestor = parent.get(pid)
        while ancestor and ancestor != root and ancestor in parent:
            ancestor = parent[ancestor]
        if ancestor == root:
            found.add(pid)
    return found


class Watch:
    """Reads a stream-json ``claude -p`` stdout line by line and its stderr, and lists the process's
    MCP servers while it runs. ``wait_for`` waits on what was printed, never for a length of time.
    ``finish`` ends it without ``communicate``, which would read stdout beside the reader thread and,
    on Python 3.12, raises ``ValueError`` once stdin is closed."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.lines: list[dict] = []
        self.err: list[str] = []
        self.servers: set[int] = set()
        self.changed = threading.Condition()
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._read_err, daemon=True)]
        for reader in self.readers:
            reader.start()
        threading.Thread(target=self._servers, daemon=True).start()

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            self.err.append(line)

    def finish(self, timeout: float = TURN_TIMEOUT_S) -> tuple[int, str]:
        """Close stdin, wait for the process to exit (killed after ``timeout``) and for both readers
        to reach the end of their pipes. The exit status and stderr."""
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for reader in self.readers:
            reader.join(timeout=10)
        return self.proc.returncode, "".join(self.err)

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with self.changed:
                self.lines.append(event if isinstance(event, dict) else {})
                self.changed.notify_all()
        with self.changed:
            self.changed.notify_all()

    def _servers(self) -> None:
        while self.proc.poll() is None:
            self.servers |= mcp_servers_of(self.proc.pid)
            time.sleep(0.1)  # sampling a process listing; nothing is asserted from its timing

    def count(self, kind: str, subtype: str | None = None) -> int:
        return sum(1 for e in self.lines if e.get("type") == kind and (subtype is None or e.get("subtype") == subtype))

    def wait_for(self, condition, what: str, timeout: float = TURN_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout
        with self.changed:
            while not condition():
                left = deadline - time.monotonic()
                assert left > 0 and self.proc.poll() is None, (f"{what}: exit {self.proc.poll()}, "
                                                               f"printed {[e.get('type') for e in self.lines]}")
                self.changed.wait(min(left, 1.0))


def user_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def pins(home: Path, name: str) -> set[str]:
    return set(re.findall(r"decision ([a-z0-9]+)", SessionStore.open_existing(home, name).state_text()))


def test_claude_with_the_printed_settings_and_mcp_config_pins_into_each_conversations_store(tmp_path):
    Upstream.seen, Upstream.other, Upstream.issued = [], [], 0
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    home = tmp_path / "home"
    service = Running(home, "--upstream", f"http://127.0.0.1:{upstream.server_address[1]}")
    try:
        printed = run_cli(home, "--print-settings")  # no --port: the running service's, found by its lock
        assert printed.returncode == 0, printed.stderr
        assert json.loads(printed.stdout)["env"]["ANTHROPIC_BASE_URL"] == serve.base_url(service.port, service.token)
        settings = tmp_path / "serve-settings.json"
        settings.touch(mode=0o600)  # it holds the token: as private as the token file (docs/serve.md)
        settings.write_text(printed.stdout)
        printed_mcp = run_cli(home, "--print-mcp-config")
        assert printed_mcp.returncode == 0, printed_mcp.stderr
        assert service.token not in printed_mcp.stdout
        mcp_config = tmp_path / "serve-mcp.json"
        mcp_config.write_text(printed_mcp.stdout)

        # (b) two conversations, one after the other, through the one service
        outcomes = {}
        for session_id, word in ((FIRST, "alpha"), (SECOND, "beta")):
            outcomes[session_id] = finish(claude(tmp_path, settings, mcp_config, "--session-id", session_id,
                                                 "--output-format", "json", f"PIN:{word} please"))
            assert outcomes[session_id][0] == 0, outcomes[session_id][2][-600:]

        # (c) one process: a turn, /clear, a turn
        proc = claude(tmp_path, settings, mcp_config, "--input-format", "stream-json", "--output-format",
                      "stream-json", "--verbose", stdin=subprocess.PIPE)
        watch = Watch(proc)
        try:
            proc.stdin.write(user_line("PIN:beforeclear please"))
            proc.stdin.flush()
            watch.wait_for(lambda: watch.count("result") >= 1, "the first turn's result")
            inits = watch.count("system", "init")
            proc.stdin.write(user_line("/clear"))
            proc.stdin.flush()
            watch.wait_for(lambda: watch.count("system", "init") > inits, "a new conversation after /clear")
            results = watch.count("result")
            proc.stdin.write(user_line("PIN:afterclear please"))
            proc.stdin.flush()
            watch.wait_for(lambda: watch.count("result") > results, "the second turn's result")
            status, stream_err = watch.finish()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        assert status == 0, stream_err[-600:]
        health = serve.ask_health(service.port, service.token)
    finally:
        stopped = service.stop()
        upstream.shutdown()
        upstream.server_close()
    assert stopped == 0, service.stderr_lines

    seen = Upstream.seen
    asked = [r for r in seen if r["pin"]]
    # (a) the five Contexto tools are offered in every request that offers tools
    offering = [r for r in seen if r["tools"]]
    assert offering and all(r["offered"] == EXPECTED for r in offering), [sorted(EXPECTED - r["offered"])
                                                                          for r in offering]
    assert len(EXPECTED) == 5
    deferred = sum(1 for r in offering if PIN_TOOL not in r["tools"])
    # Every pin the upstream asked for came back as a successful tool result.
    answered = {block["tool_use_id"]: block for r in seen for block in r["results"]}
    assert {r["tool_use_id"] for r in asked} <= set(answered), (asked, list(answered))
    assert not any(answered[r["tool_use_id"]].get("is_error") for r in asked), answered
    mapping = served(home)
    by_word = {r["pin"]: r["session"] for r in asked}
    assert set(by_word) == {"alpha", "beta", "beforeclear", "afterclear"}, by_word
    # (b) each pin is in its own conversation's store and in no other store
    assert by_word["alpha"] == FIRST and by_word["beta"] == SECOND
    # (c) /clear made a new Claude Code session id inside the one process, and one MCP server served both
    before, after = by_word["beforeclear"], by_word["afterclear"]
    assert before != after and {before, after}.isdisjoint({FIRST, SECOND})
    assert len(watch.servers) == 1, watch.servers
    everywhere = {name: pins(home, name) for name in sessions(home)}
    for word, session_id in by_word.items():
        assert word in pins(home, mapping[session_id]), (word, everywhere)
        assert [name for name, found in everywhere.items() if word in found] == [mapping[session_id]], everywhere
    assert len({mapping[s] for s in by_word.values()}) == 4
    # The token reached nothing the upstream saw, and no stderr.
    assert not any(service.token in path for _, path in Upstream.other)
    assert service.token not in "\n".join(service.stderr_lines) + stream_err
    assert health is not None and health["resolve"]["found"] == len(asked)
    searches = sum(1 for r in seen if r["call"] == SEARCH_TOOL)
    print(f"\nserve end to end with {CLAUDE}: {len(seen)} Messages requests and {len(Upstream.other)} others at the "
          f"upstream; {len(offering)} offered tools ({deferred} with contexto_pin deferred), {searches} ToolSearch "
          f"calls; {len(asked)} pins asked, {len(answered)} tool results; {len(sessions(home))} stores; "
          f"resolve found {health['resolve']['found']}, unknown {health['resolve']['unknown']} (each unknown is "
          f"one retry by the MCP client); MCP server processes in the /clear run: {len(watch.servers)}")
