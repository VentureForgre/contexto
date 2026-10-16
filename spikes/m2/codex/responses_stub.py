#!/usr/bin/env python3
"""Loopback Responses API stub for zero-token Codex probes. Answers 401 to POSTs (and 200 with an
empty JSON object to GETs, refusing WebSocket upgrades with 401), and logs structure only: method,
path, header names, whether the request asked for a WebSocket upgrade, top-level body keys,
store / previous_response_id / prompt_cache_key presence, input item (type, role) pairs, a leading
tag name of text parts, tool names, and (M2 A6) the input indexes and top-level keys whose JSON
mentions each Contexto MCP tool name. No text, no header values.

Usage: responses_stub.py <port> <log.jsonl>
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MCP_TOOLS = ("contexto_recall", "contexto_pin", "contexto_close", "contexto_state", "contexto_search")
MENTION_WORDS = ("contexto", "mcp", "tool_search", "deferred")

TAG = re.compile(r"\s*<([A-Za-z][A-Za-z0-9_-]{0,40})[\s>]")


def item_shape(i):
    s = {"type": i.get("type"), "role": i.get("role")}
    content = i.get("content")
    if isinstance(content, list):
        tags = []
        for part in content:
            text = part.get("text") if isinstance(part, dict) else None
            m = TAG.match(text) if isinstance(text, str) else None
            tags.append((part.get("type") if isinstance(part, dict) else None, m.group(1) if m else None,
                         len(text) if isinstance(text, str) else None))
        s["parts"] = tags
    if i.get("type") == "additional_tools":
        tools = i.get("tools") or i.get("content") or []
        s["tool_names"] = [t.get("name") or t.get("type") for t in tools if isinstance(t, dict)] if isinstance(tools, list) else type(tools).__name__
        s["keys"] = sorted(i)
        def names(t, depth=0):
            if not isinstance(t, dict) or depth > 3:
                return []
            own = [t["name"]] if isinstance(t.get("name"), str) else []
            for v in t.values():
                for x in (v if isinstance(v, list) else [v]):
                    own += names(x, depth + 1)
            return own
        s["tool_tree"] = [{"type": t.get("type"), "keys": sorted(t), "names": names(t)} for t in tools if isinstance(t, dict)] if isinstance(tools, list) else None
        # M2 A6: sizes and where MCP-related words occur, by namespace and tool name (no text).
        s["tool_sizes"] = [{"namespace": t.get("name"), "description_len": len(str(t.get("description") or "")),
                            "tools": [(u.get("name"), len(json.dumps(u)), [w for w in MENTION_WORDS if w in json.dumps(u).lower()])
                                      for u in t.get("tools") or [] if isinstance(u, dict)],
                            "description_mentions": [w for w in MENTION_WORDS if w in str(t.get("description") or "").lower()]}
                           for t in tools if isinstance(t, dict)] if isinstance(tools, list) else None
    if i.get("type") == "reasoning":
        s["encrypted"] = bool(i.get("encrypted_content"))
    return s


class H(BaseHTTPRequestHandler):
    def _log(self, rec):
        with open(sys.argv[2], "a") as f:
            f.write(json.dumps(rec) + "\n")

    def _reply(self, code, body=b'{"error":{"message":"stub","type":"invalid_request_error"}}'):
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        upgrade = (self.headers.get("upgrade") or "").lower() == "websocket"
        self._log({"method": "GET", "path": self.path.split("?")[0], "websocket": upgrade,
                   "header_names": sorted(k.lower() for k in self.headers.keys())})
        self._reply(401 if upgrade else 200, b"{}" if not upgrade else b'{"error":"stub"}')

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        rec = {"method": "POST", "path": self.path.split("?")[0], "bytes": n,
               "header_names": sorted(k.lower() for k in self.headers.keys())}
        try:
            body = json.loads(raw)
            rec.update({"top_keys": sorted(body), "store": body.get("store"),
                        "previous_response_id_set": body.get("previous_response_id") is not None,
                        "prompt_cache_key_set": bool(body.get("prompt_cache_key")),
                        "include": body.get("include"), "stream": body.get("stream"),
                        "instructions_len": len(body.get("instructions") or ""),
                        "tool_names": [t.get("name") or t.get("type") for t in body.get("tools") or []],
                        "input": [item_shape(i) for i in body.get("input") or [] if isinstance(i, dict)]})
            items = body.get("input") or []
            rec["mcp_mentions"] = {
                name: {"input_indexes": [n for n, i in enumerate(items) if name in json.dumps(i)],
                       "top_keys": sorted(k for k, v in body.items() if k != "input" and name in json.dumps(v))}
                for name in MCP_TOOLS}
        except Exception:
            rec["unparsed"] = True
        self._log(rec)
        self._reply(401)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
