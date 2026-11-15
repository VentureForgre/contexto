"""A recording stdio MCP server for the D70 spike (what does Claude Code tell a server it starts?).

    python3 recording_mcp.py <log.jsonl>

Newline-delimited JSON-RPC over stdin and stdout, as in src/contexto/mcp.py. One tool, ``probe_echo``.
Appends one JSON object per line to the log: a ``start`` record (the whole ``os.environ``, argv, pid,
parent pid, working directory), then every JSON-RPC message it receives, verbatim, and an ``eof``
record when stdin closes. Each record carries the pid so several servers can share one log.
"""

import json
import os
import sys
import time

LOG = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PROBE_LOG", "/tmp/d70-mcp/recording_mcp.jsonl")
TOOL = {
    "name": "probe_echo",
    "description": "Records the call and echoes its arguments.",
    "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}, "additionalProperties": True},
}


def record(kind, **fields):
    try:
        with open(LOG, "a") as f:
            f.write(json.dumps({"kind": kind, "t": time.time(), "pid": os.getpid(), "ppid": os.getppid(), **fields}) + "\n")
    except OSError as exc:
        sys.stderr.write(f"recording_mcp: cannot write {LOG}: {exc}\n")


def answer(message):
    method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if "id" not in message or not isinstance(method, str):
        return None  # a notification or a response from the client
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "probe", "version": "0.0.1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": f"probe_echo pid={os.getpid()} args={json.dumps(params.get('arguments'))}"}],
                  "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    record("start", argv=sys.argv, cwd=os.getcwd(), env=dict(os.environ))
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            record("unparsed", raw=line)
            continue
        record("message", message=message)
        response = answer(message) if isinstance(message, dict) else None
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    record("eof")
    return 0


if __name__ == "__main__":
    sys.exit(main())
