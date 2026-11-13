"""Print what a D70 run captured, in short form.

    python3 mcp_show.py <run> [--log NAME] [--full]

Reads /tmp/d70-mcp/<run>/: the recording MCP server's log (default ``mcp.jsonl``), ``start-env.json``
and ``stub/``. Prints, per server process, the environment diff against claude's start environment,
the working directory, every variable or message that holds a session id the stub saw, the
``initialize`` params and each ``tools/call`` request; then the stub's requests with their session
and agent id headers and the tool_use ids the stub sent.
"""

import json
import sys
from pathlib import Path

run = Path("/tmp/d70-mcp") / sys.argv[1]
log_name = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else "mcp.jsonl"
full = "--full" in sys.argv

start_env = json.loads((run / "start-env.json").read_text()) if (run / "start-env.json").exists() else {}
sessions, sent_ids = set(), []
print("== stub requests")
for f in sorted((run / "stub").glob("*.json")):
    captured = json.loads(f.read_text())
    headers = {k.lower(): v for k, v in captured["headers"]}
    sid, aid = headers.get("x-claude-code-session-id"), headers.get("x-claude-code-agent-id")
    if sid:
        sessions.add(sid)
    ids = [b["id"] for b in captured.get("reply") or [] if b.get("type") == "tool_use"]
    sent_ids += ids
    body = captured.get("body") or {}
    tools = [t.get("name") for t in body.get("tools") or []]
    print(f"{f.stem}: {captured['method']} {captured['path']} session={sid} agent={aid} tools={len(tools)}"
          f" probe_tools={[t for t in tools if 'probe' in str(t)]} sent_tool_use={ids}")
print("session ids seen by the stub:", sorted(sessions))

log = run / log_name
records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
print(f"== MCP server log {log} ({len(records)} records)")
for r in records:
    if r["kind"] == "start":
        env = r["env"]
        print(f"-- server pid={r['pid']} ppid={r['ppid']} cwd={r['cwd']} argv={r['argv']}")
        print("   env added:", json.dumps({k: v for k, v in sorted(env.items()) if k not in start_env}))
        print("   env changed:", json.dumps({k: [start_env[k], v] for k, v in sorted(env.items())
                                              if k in start_env and start_env[k] != v}))
        print("   env removed:", sorted(k for k in start_env if k not in env))
        print("   env values holding a stub session id:", {k: v for k, v in env.items() if any(s in v for s in sessions)})
    elif r["kind"] == "message":
        m = r["message"]
        method = m.get("method")
        text = json.dumps(m)
        holds = [s for s in sessions if s in text]
        if method in ("initialize", "tools/call") or full:
            print(f"-- pid={r['pid']} {method}: {text}")
            print(f"   holds a stub session id: {holds}")
            if method == "tools/call":
                meta = (m.get("params") or {}).get("_meta") or {}
                tid = meta.get("claudecode/toolUseId")
                print(f"   _meta keys: {sorted(meta)}; toolUseId sent by the stub: {tid in sent_ids}")
        else:
            print(f"-- pid={r['pid']} {method or 'response'} id={m.get('id')}")
    else:
        print(f"-- pid={r['pid']} {r['kind']}")
