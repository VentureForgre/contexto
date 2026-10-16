#!/usr/bin/env python3
"""Loopback fake model for zero-token shape probes. Answers POST /v1/messages with a valid
streamed (SSE) or JSON reply, and logs the request STRUCTURE with shape_stub's helpers. If the
newest user turn contains the marker USE_TOOL and has no tool_result yet, the reply is one
tool_use of the Read tool on a synthetic file; otherwise it is the text "ok". HEAD and other
endpoints get 200 with an empty JSON object. No model is called and no text is logged.

Usage: fake_model_stub.py <port> <log.jsonl> <read_path>
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, __import__("os").path.dirname(__file__))
from shape_stub import message_shape  # noqa: E402

LOG, READ_PATH = sys.argv[2], sys.argv[3]
COUNTER = {"n": 0}


def wants_tool(messages):
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        blocks = c if isinstance(c, list) else [{"type": "text", "text": c}]
        if any(b.get("type") == "tool_result" for b in blocks if isinstance(b, dict)):
            return False
        if any("USE_TOOL" in (b.get("text") or "") for b in blocks if isinstance(b, dict)):
            return True
        if any(b.get("type") == "text" for b in blocks if isinstance(b, dict)):
            return False
    return False


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class H(BaseHTTPRequestHandler):
    def _reply(self, code, ctype, body):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        path = self.path.split("?")[0]
        msgs = body.get("messages") or []
        with open(LOG, "a") as f:
            f.write(json.dumps({"method": "POST", "path": path, "bytes": n, "stream": body.get("stream"),
                                "tool_names": [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)],
                                "messages": [message_shape(m) for m in msgs]}) + "\n")
        if path.endswith("/count_tokens"):
            return self._reply(200, "application/json", b'{"input_tokens": 10}')
        COUNTER["n"] += 1
        mid = f"msg_fake{COUNTER['n']:04d}"
        usage = {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if wants_tool(msgs):
            block = {"type": "tool_use", "id": f"toolu_fake{COUNTER['n']:04d}", "name": "Read", "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps({"file_path": READ_PATH})}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": "ok"}
            stop = "end_turn"
        if not body.get("stream"):
            full = dict(block)
            if stop == "tool_use":
                full["input"] = {"file_path": READ_PATH}
            else:
                full["text"] = "ok"
            msg = {"id": mid, "type": "message", "role": "assistant", "model": body.get("model"),
                   "content": [full], "stop_reason": stop, "stop_sequence": None, "usage": usage}
            return self._reply(200, "application/json", json.dumps(msg).encode())
        events = [
            {"type": "message_start", "message": {"id": mid, "type": "message", "role": "assistant",
                                                  "model": body.get("model"), "content": [], "stop_reason": None,
                                                  "stop_sequence": None, "usage": usage}},
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        self._reply(200, "text/event-stream", sse(events))

    def do_GET(self):
        self._reply(200, "application/json", b"{}")
    do_HEAD = do_GET

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
