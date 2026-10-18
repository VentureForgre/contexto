"""A local fake Messages API upstream that streams realistic SSE. Zero spend: it binds 127.0.0.1 and
never forwards anything.

Usage it reports is computed from the body the proxy forwarded: ``ceil(bytes / 1.83)`` input tokens
(split into 2 uncached, the rest cache read; the engine sums all three), and a fixed number of output
tokens per reply kind. The reply kind comes from the ``x-scale-reply`` request header the driver sets
(``tool_use`` or ``end_turn``), so tool_use replies exercise the proxy's stream hold (contract PR #74).
Per request it records sizes and monotonic timestamps under the ``x-scale-id`` header; nothing else.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from scale.proxy.synth import tokens_of

OUTPUT_TOKENS = {"tool_use": 180, "end_turn": 420}


def sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def reply_events(reply: str, input_tokens: int, request_id: str) -> list[bytes]:
    """The SSE events of one streamed reply, in the order the Messages API sends them."""
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": max(0, input_tokens - 2), "output_tokens": 1}
    message = {"id": f"msg_{request_id}", "type": "message", "role": "assistant", "model": "fake-scale",
               "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
    events = [sse("message_start", {"type": "message_start", "message": message}), sse("ping", {"type": "ping"}),
              sse("content_block_start", {"type": "content_block_start", "index": 0,
                                          "content_block": {"type": "text", "text": ""}}),
              sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": "Working on it."}}),
              sse("content_block_stop", {"type": "content_block_stop", "index": 0})]
    if reply == "tool_use":
        events += [sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                       "type": "tool_use", "id": f"toolu_{request_id}", "name": "Read", "input": {}}}),
                   sse("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {
                       "type": "input_json_delta", "partial_json": '{"file_path": "/repo/src/x.py"}'}}),
                   sse("content_block_stop", {"type": "content_block_stop", "index": 1})]
    events += [sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": reply, "stop_sequence": None},
                                     "usage": {"output_tokens": OUTPUT_TOKENS.get(reply, 100)}}),
               sse("message_stop", {"type": "message_stop"})]
    return events


class FakeUpstream:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> int:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_HEAD(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                t_recv = time.monotonic()
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                t_body = time.monotonic()
                request_id = self.headers.get("x-scale-id") or f"anon{len(owner.records)}"
                reply = self.headers.get("x-scale-reply") or "end_turn"
                try:
                    messages = len(json.loads(body).get("messages") or [])
                except (ValueError, AttributeError):
                    messages = -1
                tokens = tokens_of(len(body))
                with owner.lock:  # recorded before replying, so the driver always finds it
                    owner.records[request_id] = {"bytes": len(body), "tokens": tokens, "messages": messages,
                                                 "t_recv": t_recv, "t_body": t_body}
                if self.path.split("?", 1)[0] != "/v1/messages":
                    payload = json.dumps({"input_tokens": tokens}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for raw in reply_events(reply, tokens, request_id):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="fake-upstream", daemon=True).start()
        return self._server.server_address[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def take(self, request_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.records.pop(request_id, None)
