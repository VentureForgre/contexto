#!/usr/bin/env python3
"""Minimal MCP stdio server (newline-delimited JSON-RPC) exposing one tool, contexto_probe, so a
zero-token Codex run can show whether an MCP server registered with -c reaches the request."""
import json
import sys

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "contexto-probe", "version": "0.0.1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "contexto_probe", "description": "synthetic probe tool",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
