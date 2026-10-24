"""Scripted stub of the Anthropic Messages API for driving Claude Code at zero cost.

Every request body is saved to CAPTURE/NNN-<kind>.json. The scenario module decides each reply:
``reply(body) -> list of content blocks`` (text and tool_use dicts) and a stop reason.
"""

import importlib.util
import itertools
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURE = sys.argv[2]
os.makedirs(CAPTURE, exist_ok=True)
spec = importlib.util.spec_from_file_location("scenario", sys.argv[3])
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)
counter = itertools.count(1)
lock = threading.Lock()


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        with lock:
            n = next(counter)
        if self.path.split("?")[0].endswith("/count_tokens"):
            kind = "count"
            out = json.dumps({"input_tokens": 100}).encode()
            self._save(n, kind, body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        kind, blocks, stop = scenario.reply(body)
        self._save(n, kind, body)
        usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        message = {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": body.get("model", "m"),
                   "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        if not body.get("stream"):
            out = json.dumps({**message, "content": blocks, "stop_reason": stop}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        chunks = [sse("message_start", {"type": "message_start", "message": message})]
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                chunks += [sse("content_block_start", {"type": "content_block_start", "index": i,
                                                       "content_block": {"type": "text", "text": ""}}),
                           sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                       "delta": {"type": "text_delta", "text": block["text"]}})]
            else:
                chunks += [sse("content_block_start", {"type": "content_block_start", "index": i,
                                                       "content_block": {**block, "input": {}}}),
                           sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                       "delta": {"type": "input_json_delta",
                                                                 "partial_json": json.dumps(block["input"])}})]
            chunks.append(sse("content_block_stop", {"type": "content_block_stop", "index": i}))
        chunks += [sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                         "usage": {"output_tokens": 10}}),
                   sse("message_stop", {"type": "message_stop"})]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for c in chunks:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c))
        self.wfile.write(b"0\r\n\r\n")

    def _save(self, n, kind, body):
        with open(os.path.join(CAPTURE, f"{n:03d}-{kind}.json"), "w") as f:
            json.dump({"path": self.path, "body": body}, f, indent=1)


server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
server.serve_forever()
