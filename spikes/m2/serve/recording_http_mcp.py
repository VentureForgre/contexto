"""A recording MCP server over HTTP (Streamable HTTP, JSON replies only) for the D70 spike.

    python3 recording_http_mcp.py <port> <log.jsonl>

Appends one JSON line per HTTP request: method, path, every request header in arrival order, and the
body. POST answers JSON-RPC with ``application/json`` (``initialize`` also sets ``Mcp-Session-Id``),
a notification with 202, GET with 405 (no server-to-client stream), DELETE with 200. One tool,
``probe_echo``. Prints ``listening`` once it holds the port.
"""

import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG = 0, ""  # set from the command line by main()
lock = threading.Lock()
TOOL = {"name": "probe_echo", "description": "Records the call and echoes its arguments.",
        "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}, "additionalProperties": True}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _record(self, body):
        with lock, open(LOG, "a") as f:
            f.write(json.dumps({"t": time.time(), "method": self.command, "path": self.path,
                                "headers": list(self.headers.items()), "body": body}) + "\n")

    def _send(self, status, payload=None, extra=None):
        out = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._record(None)
        self._send(405)

    def do_DELETE(self):
        self._record(None)
        self._send(200)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            message = json.loads(raw or b"{}")
        except ValueError:
            message = {"_unparsed": raw.decode("utf-8", "replace")}
        self._record(message)
        if not isinstance(message, dict) or "id" not in message or "method" not in message:
            return self._send(202)
        method, params, extra = message["method"], message.get("params") or {}, {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "probe-http", "version": "0.0.1"}}
            extra = {"Mcp-Session-Id": "d70-http-" + uuid.uuid4().hex}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "probe_echo over http"}], "isError": False}
        else:
            return self._send(200, {"jsonrpc": "2.0", "id": message["id"],
                                    "error": {"code": -32601, "message": f"Method not found: {method}"}})
        self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": result}, extra)


def main() -> None:
    global PORT, LOG
    PORT, LOG = int(sys.argv[1]), sys.argv[2]
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("listening", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
