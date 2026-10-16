"""Zero-spend timing measurement: when does Claude Code start an MCP tool call during a stream?

    python3 measure.py [--hold 6] [--count-tokens-403]

A loopback fake upstream streams one ``tool_use`` for ``mcp__contexto__contexto_state``, sends its
``content_block_stop``, then HOLDS the final ``message_delta`` and ``message_stop`` (and the HTTP
terminator) for ``--hold`` seconds. A stdio relay in front of lane B's real MCP server
(``python -m contexto mcp``) timestamps the ``tools/call`` it receives. Comparing the timestamps
shows whether the CLI starts the tool call on ``content_block_stop`` or only after the stream ends.

No model, no tokens, no real credential, and no user configuration: a temporary
``CLAUDE_CONFIG_DIR``, a fake API key, ``--bare``, a flag-level ``--settings`` with the stub's base
URL, and ``--strict-mcp-config`` (the pattern of tests/test_mcp_real_client.py). Prints numbers only.

``--through-proxy`` puts a real ``ContextoProxy`` (packet A9's stream hold, contract PR #74) between
the CLI and the stub, records when the proxy publishes usage, and reports whether the tool call
starts only after that publish.

``--count-tokens-403`` makes the stub answer 403 on /v1/messages/count_tokens and reports whether
the CLI called it and what it did next.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RELAY = r'''
import json, subprocess, sys, threading, time
log, child_cmd = sys.argv[1], sys.argv[2:]
child = subprocess.Popen(child_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
def pump_out():
    for line in child.stdout:
        sys.stdout.write(line); sys.stdout.flush()
threading.Thread(target=pump_out, daemon=True).start()
for line in sys.stdin:
    try:
        method = json.loads(line).get("method")
    except ValueError:
        method = None
    if method == "tools/call":
        with open(log, "a") as f:
            f.write(json.dumps({"event": "tools_call", "t": time.time()}) + "\n")
    child.stdin.write(line); child.stdin.flush()
child.stdin.close(); child.wait()
'''


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    events: list[dict] = []
    hold = 6.0
    count_tokens_403 = False

    def log_message(self, *args):
        pass

    def note(self, event: str, **extra) -> None:
        Stub.events.append({"event": event, "t": time.time(), "path": self.path.split("?")[0], **extra})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            self.note("count_tokens")
            status, payload = (403, {"type": "error", "error": {"type": "permission_error", "message": "stub 403"}}) \
                if Stub.count_tokens_403 else (200, {"input_tokens": 100})
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        messages = body.get("messages") or []
        has_result = any(isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"])
                         for m in messages)
        self.note("messages_request", has_tool_result=has_result)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if has_result:
            self.events_out([
                {"type": "message_start", "message": {"id": "msg_2", "type": "message", "role": "assistant",
                                                      "content": [], "model": "stub", "usage": {"input_tokens": 10, "output_tokens": 1}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
                {"type": "message_stop"}])
            self.end()
            return
        self.events_out([
            {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "content": [],
                                                  "model": "stub", "usage": {"input_tokens": 10, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_stub_1", "name": "mcp__contexto__contexto_state", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 0}])
        self.note("tool_use_block_stop_sent")
        time.sleep(Stub.hold)
        self.events_out([{"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
                         {"type": "message_stop"}])
        self.end()
        self.note("stream_end_sent")

    def events_out(self, events: list[dict]) -> None:
        for e in events:
            chunk = f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()

    def end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    do_GET = do_HEAD = lambda self: (self.send_response(404), self.send_header("Content-Length", "0"), self.end_headers())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hold", type=float, default=6.0)
    parser.add_argument("--count-tokens-403", action="store_true")
    parser.add_argument("--through-proxy", action="store_true")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    claude = shutil.which("claude")
    if claude is None:
        raise SystemExit("claude CLI not found")
    Stub.events, Stub.hold, Stub.count_tokens_403 = [], args.hold, args.count_tokens_403
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="toolcall-"))
    proxy = None
    published: list[float] = []
    try:
        relay_log = tmp / "relay.jsonl"
        relay = tmp / "relay.py"
        relay.write_text(RELAY)
        mcp = tmp / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"contexto": {"command": sys.executable, "args": [
            str(relay), str(relay_log), sys.executable, "-m", "contexto", "mcp", "--session", "timing",
            "--home", str(tmp / "home")]}}}))
        base = f"http://127.0.0.1:{server.server_port}"
        if args.through_proxy:
            from contexto.proxy import ContextoProxy
            from contexto.store.memory import MemoryStore

            def observe(endpoint, usage, request):
                published.append(time.time())
                return []

            proxy = ContextoProxy(base, MemoryStore(), lambda r, h, e: (r, h, [{"epoch": 0}]), observe_usage=observe)
            proxy.start()
            base = proxy.url
        settings = tmp / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": base}}))
        scratch = tmp / "scratch"
        scratch.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENRIG_", "CLAUDE_CODE_", "CLAUDECODE", "ANTHROPIC_"))}
        env.update({"CLAUDE_CONFIG_DIR": str(tmp / "config"), "ANTHROPIC_API_KEY": "fake-key-for-stub",
                    "ANTHROPIC_BASE_URL": base})
        started = time.time()
        proc = subprocess.run(
            [claude, "-p", "--bare", "--settings", str(settings), "--mcp-config", str(mcp), "--strict-mcp-config",
             "--model", "claude-haiku-5-5", "--allowedTools", "mcp__contexto__contexto_state",
             "--output-format", "json", "use the contexto_state tool"],
            cwd=scratch, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=args.timeout)
        calls = [json.loads(line) for line in relay_log.read_text().splitlines()] if relay_log.exists() else []
    finally:
        if proxy is not None:
            proxy.stop()
        shutil.rmtree(tmp, ignore_errors=True)
        server.shutdown()
        server.server_close()
    def first(name):
        return next((e["t"] for e in Stub.events if e["event"] == name), None)
    block_stop, stream_end = first("tool_use_block_stop_sent"), first("stream_end_sent")
    call = calls[0]["t"] if calls else None
    report = {
        "claude_exit": proc.returncode,
        "hold_seconds": args.hold,
        "requests": [e["event"] + (":tool_result" if e.get("has_tool_result") else "") for e in Stub.events
                     if e["event"] in ("messages_request", "count_tokens")],
        "tools_call_received": call is not None,
        "tools_call_after_block_stop_s": round(call - block_stop, 3) if call and block_stop else None,
        "tools_call_after_stream_end_s": round(call - stream_end, 3) if call and stream_end else None,
        "started_before_stream_end": bool(call and stream_end and call < stream_end),
        "count_tokens_calls": sum(1 for e in Stub.events if e["event"] == "count_tokens"),
        "through_proxy": args.through_proxy,
        "tools_call_after_first_publish_s": round(call - published[0], 3) if call and published else None,
        "tools_call_before_publish": bool(call and published and call < published[0]),
        "elapsed_s": round(time.time() - started, 1),
    }
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
