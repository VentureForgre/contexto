"""Loopback stub for probe.sh: appends each request's method and path to a file and answers 401.

Usage: stub.py PORT LOGFILE. It keeps no body and no header, calls no model and forwards nothing.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _answer(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with open(LOG, "a") as f:
            f.write(f"{self.command} {self.path.split('?')[0]}\n")
        body = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "stub"}}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET = do_HEAD = _answer


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
