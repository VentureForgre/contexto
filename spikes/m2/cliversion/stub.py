"""Loopback stub for check.sh: saves each request body under <out>/bodies/ and its path and User-Agent
in <out>/requests.jsonl, then answers 401. No other header is kept (the keys are fake anyway)."""

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OUT = Path(sys.argv[2])
(OUT / "bodies").mkdir(parents=True, exist_ok=True)
LOG = open(OUT / "requests.jsonl", "a", encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        name = None
        if data:
            name = f"{self.path.split('?')[0].strip('/').replace('/', '_')}-{hashlib.sha256(data).hexdigest()[:12]}.json"
            (OUT / "bodies" / name).write_bytes(data)
        LOG.write(json.dumps({"method": self.command, "path": self.path, "user_agent": self.headers.get("User-Agent"),
                              "body": name}) + "\n")
        LOG.flush()
        body = b'{"type":"error","error":{"type":"authentication_error","message":"cliversion stub"}}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET = _answer

    def log_message(self, *args) -> None:
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
