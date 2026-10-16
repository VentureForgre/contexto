"""Zero-token routing stub: answers every request with 401 and logs only its shape.

    python3 stub401.py --port 18451 --log .contexto/spikes/L1-a/route/stub.jsonl

Listens on 127.0.0.1. Each line records method, path without query, header names, and body size.
No header values or bodies are written. A CLI pointed here fails fast without reaching a model.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from proxy import utc_now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args(argv)
    args.log.parent.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *a):  # noqa: A002 - stdlib signature
            pass

        def do_GET(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with args.log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": utc_now(), "method": self.command, "path": self.path.split("?", 1)[0],
                                    "header_names": sorted({k.lower() for k in self.headers}),
                                    "body_bytes": len(body)}) + "\n")
            reply = json.dumps({"type": "error", "error": {"type": "authentication_error",
                                                           "message": "routing stub: 401 by design"}}).encode()
            self.send_response_only(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        do_POST = do_PUT = do_HEAD = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"stub listening on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
