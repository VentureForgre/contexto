"""Scripted stand-in for the Anthropic Messages API for the D70 spike. Zero spend: it never forwards.

    python3 stub_upstream.py <port> <capture dir>

Saves every request (method, path, all headers, body) to <capture dir>/NNN-<kind>.json and prints
``listening`` once it holds the port. The reply is chosen from the LAST user message of the request:

- it holds a ``tool_result``                     -> text, end_turn
- its text holds ``D70CALL2`` (and tools sent)   -> two ``mcp__probe__probe_echo`` tool_use blocks
- its text holds ``D70CALL1`` (and tools sent)   -> one ``mcp__probe__probe_echo`` tool_use block
- its text holds ``D70BASH`` (and tools sent)    -> a ``Bash`` tool_use appending its D70*/CLAUDE* environment to bash-env.txt
- its text holds ``D70AGENT`` (and tools sent)   -> an ``Agent`` tool_use whose prompt is ``D70CALL1 from the subagent``
- anything else                                  -> text, end_turn

Tool use ids are ``toolu_01D70<request number>...`` so each can be matched to what the MCP server saw.
"""

import itertools
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, CAPTURE = 0, ""  # set from the command line by main()
TOOL = os.environ.get("D70_TOOL", "mcp__probe__probe_echo")
counter = itertools.count(1)
lock = threading.Lock()


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def last_user(body):
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content, False
            blocks = [b for b in content or [] if isinstance(b, dict)]
            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return text, any(b.get("type") == "tool_result" for b in blocks)
    return "", False


def tool_use(n, k, name, inp):
    return {"type": "tool_use", "id": f"toolu_01D70{n:03d}{k}xQ7mZp4LkTr9VbYs", "name": name, "input": inp}


def reply(n, body):
    text, has_result = last_user(body)
    has_tools = bool(body.get("tools"))
    if has_result or not has_tools:
        return "plain", [{"type": "text", "text": f"stub reply {n}"}], "end_turn"
    if "D70AGENT" in text:
        inp = {"description": "probe subagent", "prompt": "D70CALL1 from the subagent",
               "subagent_type": "general-purpose", "run_in_background": False}
        return "agent", [tool_use(n, "a", "Agent", inp)], "tool_use"
    if "D70BASH" in text:
        command = "env | grep -E '^(D70|CLAUDE)' >> bash-env.txt; echo --- >> bash-env.txt"
        return "bash", [tool_use(n, "a", "Bash", {"command": command, "description": "record the environment"})], "tool_use"
    if "D70CALL2" in text:
        return "call2", [tool_use(n, "a", TOOL, {"note": "first of two"}),
                         tool_use(n, "b", TOOL, {"note": "second of two"})], "tool_use"
    if "D70CALL1" in text:
        return "call1", [tool_use(n, "a", TOOL, {"note": "single"})], "tool_use"
    return "plain", [{"type": "text", "text": f"stub reply {n}"}], "end_turn"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _save(self, n, kind, body, extra=None):
        with open(os.path.join(CAPTURE, f"{n:03d}-{kind}.json"), "w") as f:
            json.dump({"method": self.command, "path": self.path, "headers": list(self.headers.items()),
                       "body": body, **(extra or {})}, f, indent=1)

    def _json(self, status, payload):
        out = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        with lock:
            n = next(counter)
        self._save(n, "get", None)
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub"}})

    do_HEAD = do_GET

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"_unparsed": raw.decode("utf-8", "replace")[:2000]}
        with lock:
            n = next(counter)
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            self._save(n, "count", body)
            return self._json(200, {"input_tokens": 100})
        if not path.endswith("/v1/messages"):
            self._save(n, "other", body)
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub"}})
        kind, blocks, stop = reply(n, body)
        self._save(n, kind, body, {"reply": blocks})
        usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        message = {"id": f"msg_01D70{n:03d}", "type": "message", "role": "assistant", "model": body.get("model", "m"),
                   "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        if not body.get("stream"):
            return self._json(200, {**message, "content": blocks, "stop_reason": stop})
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
        self.wfile.flush()


def main() -> None:
    global PORT, CAPTURE
    PORT, CAPTURE = int(sys.argv[1]), sys.argv[2]
    os.makedirs(CAPTURE, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("listening", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
