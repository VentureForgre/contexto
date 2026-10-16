#!/usr/bin/env python3
"""Loopback stub that answers every request with 401 and logs the STRUCTURE of Messages API
requests only: per message the role, block types, text lengths, a leading tag name (for example
system-reminder) when a text block starts with one, and where cache_control sits. No text.

Usage: shape_stub.py <port> <log.jsonl>
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TAG = re.compile(r"\s*<([A-Za-z][A-Za-z0-9_-]{0,40})[\s>]")


def block_shape(b):
    if not isinstance(b, dict):
        return {"type": type(b).__name__}
    s = {"type": b.get("type")}
    if isinstance(b.get("text"), str):
        s["len"] = len(b["text"])
        m = TAG.match(b["text"])
        if m:
            s["tag"] = m.group(1)
    if "cache_control" in b:
        s["cache_control"] = True
    return s


def message_shape(m):
    c = m.get("content")
    s = {"role": m.get("role")}
    if isinstance(c, str):
        s["content"] = "str"
        s["len"] = len(c)
        t = TAG.match(c)
        if t:
            s["tag"] = t.group(1)
    else:
        s["blocks"] = [block_shape(b) for b in c or []]
    if "cache_control" in m:
        s["cache_control"] = True
    extra = sorted(set(m) - {"role", "content"})
    if extra:
        s["extra_keys"] = extra
        # Nested key names and short enum-like values of extra fields (for example output_config).
        s["extra_shape"] = {k: ({kk: (vv if isinstance(vv, (int, float, bool)) or (isinstance(vv, str) and len(vv) <= 24 and vv.replace("_", "").replace("-", "").isalnum()) else type(vv).__name__)
                                 for kk, vv in m[k].items()} if isinstance(m[k], dict) else type(m[k]).__name__)
                            for k in extra}
    s["json_len"] = len(__import__("json").dumps(m, separators=(",", ":"))) if not s.get("blocks") and s.get("content") != "str" else None
    return s


class H(BaseHTTPRequestHandler):
    def _handle(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        rec = {"method": self.command, "path": self.path.split("?")[0], "bytes": n}
        try:
            body = json.loads(raw)
            rec["messages"] = [message_shape(m) for m in body.get("messages") or []]
            rec["top_keys"] = sorted(body)
        except Exception:
            pass
        with open(sys.argv[2], "a") as f:
            f.write(json.dumps(rec) + "\n")
        out = b'{"type":"error","error":{"type":"authentication_error","message":"stub"}}'
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_POST = do_GET = do_HEAD = _handle

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
