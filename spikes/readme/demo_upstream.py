"""A stand-in for the model API on 127.0.0.1, for the README recording. It is not a model.

It answers ``POST /v1/messages`` with a Messages API stream and forwards nothing. For each request it
prints, labelled ``[fake upstream]``, the size it received: the request body's bytes at one token per
3.5 bytes (``demo_script.tokens``). The same number goes back as ``usage.input_tokens``, so Contexto's
engine measures against it as it would against real usage.

The reply follows three rules (``scripted_reply``):

1. The newest message holds a tool result: answer with the name that result gives after "codename
   is", or say that the result does not name it.
2. The newest message asks the scripted question: call ``mcp__contexto__contexto_search``.
3. Anything else: answer ``OK``.

Usage: ``python demo_upstream.py --port 8399``. Stops on SIGTERM or Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_script as script  # noqa: E402

DEFAULT_PORT = 8399


def say(text: str, end: str = "\n") -> None:
    sys.stdout.write(f"{script.UPSTREAM_LABEL} {text}{end}")
    sys.stdout.flush()


def block_text(block: Any) -> str:
    """The text of one content block: a text block's text, or a tool result's content, flattened."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    if block.get("type") == "text":
        return str(block.get("text") or "")
    if block.get("type") == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(block_text(b) for b in content or [])
    return ""


def newest(request: dict[str, Any]) -> tuple[str, str]:
    """(text, tool result text) of the newest message."""
    messages = request.get("messages") or []
    content = messages[-1].get("content") if messages else ""
    blocks = [content] if isinstance(content, str) else list(content or [])
    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    texts = [b for b in blocks if b not in results]
    return "\n".join(block_text(b) for b in texts), "\n".join(block_text(b) for b in results)


def scripted_reply(request: dict[str, Any]) -> dict[str, Any]:
    """The reply the three rules give: ``{"text": ...}`` or ``{"tool": name, "input": {...}}``."""
    text, result = newest(request)
    if result:
        # The name is read out of the result that arrived, not taken from the script.
        named = re.search(r"codename is ([\w-]+)", result)
        if named:
            return {"text": f"The search result names it: {named.group(1)}."}
        return {"text": "The search result does not name it."}
    if script.QUESTION in text:
        return {"tool": script.SEARCH_TOOL, "input": {"query": script.QUERY}}
    return {"text": "OK"}


def sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def reply_events(reply: dict[str, Any], input_tokens: int, n: int) -> list[bytes]:
    """The stream events of one reply, in the order the Messages API sends them."""
    usage = {"input_tokens": input_tokens, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
             "output_tokens": 1}
    message = {"id": f"msg_fake_{n:04d}", "type": "message", "role": "assistant", "model": script.MODEL,
               "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
    events = [sse("message_start", {"type": "message_start", "message": message})]
    if "tool" in reply:
        block = {"type": "tool_use", "id": f"toolu_fake_{n:04d}", "name": reply["tool"], "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(reply["input"])}
        stop, output = "tool_use", 40
    else:
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": reply["text"]}
        stop, output = "end_turn", script.tokens(len(reply["text"]))
    events += [sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block}),
               sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
               sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
               sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": output}}),
               sse("message_stop", {"type": "message_stop"})]
    return events


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    count = 0
    lock = threading.Lock()

    def log_message(self, *args: Any) -> None:
        pass

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self._empty(200)

    def do_GET(self) -> None:  # noqa: N802
        self._empty(404)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.split("?", 1)[0] != "/v1/messages":
            self._empty(404)
            return
        try:
            request = json.loads(body)
            reply = scripted_reply(request)
        except (ValueError, AttributeError, IndexError):
            self._empty(400)
            return
        received = script.tokens(len(body))
        with Handler.lock:
            Handler.count += 1
            n = Handler.count
            say(f"received {received:,} tokens")
            if "tool" in reply:
                say(f"scripted reply: call {reply['tool'].rpartition('__')[2]} {json.dumps(reply['input'])}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for raw in reply_events(reply, received, n):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    """``HTTPServer.server_bind`` names the server with ``socket.getfqdn(host)``, a reverse lookup that
    took longer than the recorder's wait for the listening line on macOS CI runners. Nothing here reads
    the name."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"demo_upstream: cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    # The recorder waits for this line before it starts the session.
    say(f"listening on 127.0.0.1:{server.server_address[1]}, "
        f"counting 1 token per {script.BYTES_PER_TOKEN} bytes received")
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
