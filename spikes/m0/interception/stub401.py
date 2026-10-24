#!/usr/bin/env python3
"""Local stub upstream on 127.0.0.1: answers every request with 401 and logs
method, path, header NAMES, body size, message structure (roles and block types),
and synthetic MARKER-* test tokens. No other content, no header values."""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = sys.argv[2]

class H(BaseHTTPRequestHandler):
    def _handle(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        shape = None
        try:
            body = json.loads(raw)
            msgs = body.get("messages") or []
            shape = {"model": body.get("model"), "n_messages": len(msgs),
                     "roles": [m.get("role") for m in msgs],
                     "block_types": [[b.get("type") for b in m["content"]] if isinstance(m.get("content"), list) else "str" for m in msgs],
                     "n_tools": len(body.get("tools") or []), "system_blocks": len(body.get("system") or [])}
            if isinstance(body.get("input"), list):  # OpenAI Responses API
                shape["input_items"] = [(i.get("type"), i.get("role")) for i in body["input"]]
                shape["store"] = body.get("store")
                shape["previous_response_id_set"] = body.get("previous_response_id") is not None
                shape["include"] = body.get("include")
        except Exception:
            pass
        markers = sorted(set(re.findall(rb"MARKER-[A-Z0-9-]+", raw)))
        with open(LOG, "a") as f:
            f.write(json.dumps({"t": time.time(), "method": self.command, "path": self.path.split("?")[0],
                                "header_names": sorted(k.lower() for k in self.headers.keys()),
                                "body_bytes": n, "shape": shape,
                                "markers": [m.decode() for m in markers]}) + "\n")
        body = b'{"type":"error","error":{"type":"authentication_error","message":"stub upstream"}}'
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_POST = do_GET = do_HEAD = _handle
    def log_message(self, *a): pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
