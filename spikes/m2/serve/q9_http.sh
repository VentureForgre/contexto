#!/bin/bash
# D70 spike, question 9: an MCP server of "type": "http" at a local recording server.
# usage: q9_http.sh <run> <headers|helper|user-helper>
#   headers:     --mcp-config entry with a static header and ${VAR} headers
#   helper:      --mcp-config entry with headersHelper as well
#   user-helper: the same entry at user scope ($CLAUDE_CONFIG_DIR/.claude.json), no --mcp-config
# Each run is one claude -p in stream-json mode: a probe call, /clear, a probe call. Zero spend.
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=/tmp/d70-mcp
run=$1; mode=$2
py=$(command -v python3)
rm -rf "$root/$run"; mkdir -p "$root/$run/config" "$root/$run/work" "$root/$run/home"
port=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
python3 "$here/recording_http_mcp.py" "$port" "$root/$run/http.jsonl" > "$root/$run/http.log" 2>&1 &
http=$!
trap 'kill $http 2>/dev/null; wait $http 2>/dev/null' EXIT
for _ in $(seq 50); do grep -qx listening "$root/$run/http.log" && break; sleep 0.1; done
grep -qx listening "$root/$run/http.log" || { echo "http MCP server not listening"; exit 1; }
headers='"headers":{"X-Probe-Static":"static-value","X-Probe-Start-Env":"${D70_START_MARK}","X-Probe-Settings-Env":"${D70_SETTINGS_ENV}","X-Probe-Sid":"${CLAUDE_CODE_SESSION_ID}","X-Probe-Default":"${D70_UNSET:-fallback-value}"}'
helper="\"headersHelper\":\"$py $here/headers_helper.py $root/$run/helper.jsonl\""
entry="{\"type\":\"http\",\"url\":\"http://127.0.0.1:$port/mcp\",$headers}"
[ "$mode" != headers ] && entry="{\"type\":\"http\",\"url\":\"http://127.0.0.1:$port/mcp\",$headers,$helper}"
settings='{"env":{"D70_SETTINGS_ENV":"from-settings-env-block"},"permissions":{"allow":["mcp__probe__probe_echo"]}}'
printf '%s\n' '{"type":"user","message":{"role":"user","content":"D70CALL1 before clear"}}' \
  '{"type":"user","message":{"role":"user","content":"/clear"}}' \
  '{"type":"user","message":{"role":"user","content":"D70CALL1 after clear"}}' > "$root/$run/stdin.jsonl"
common=(-p --model claude-haiku-5-5 --input-format stream-json --output-format stream-json --verbose --debug-file "$root/$run/debug.log")
if [ "$mode" = user-helper ]; then
  echo "{\"mcpServers\":{\"probe\":$entry}}" > "$root/$run/config/.claude.json"
  echo "$settings" > "$root/$run/config/settings.json"
  python3 "$here/mcp_probe.py" "$run" --stdin-file "{run}/stdin.jsonl" --stdin-delay 7 -- "${common[@]}" > "$root/$run.summary" 2>&1
else
  echo "{\"mcpServers\":{\"probe\":$entry}}" > "$root/$run/mcp.json"
  echo "$settings" > "$root/$run/settings.json"
  python3 "$here/mcp_probe.py" "$run" --stdin-file "{run}/stdin.jsonl" --stdin-delay 7 -- "${common[@]}" \
    --settings "{run}/settings.json" --mcp-config "{run}/mcp.json" > "$root/$run.summary" 2>&1
fi
python3 - "$root/$run" <<'PY'
import json, sys
from pathlib import Path
run = Path(sys.argv[1])
sessions = []
for f in sorted((run / "stub").glob("*.json")):
    h = {k.lower(): v for k, v in json.loads(f.read_text())["headers"]}
    if h.get("x-claude-code-session-id") and h["x-claude-code-session-id"] not in sessions:
        sessions.append(h["x-claude-code-session-id"])
print("stub session ids in order:", sessions)
log = run / "http.jsonl"
for line in log.read_text().splitlines() if log.exists() else []:
    r = json.loads(line)
    body = r["body"] if isinstance(r["body"], dict) else {}
    text = json.dumps(r)
    print(f"{r['method']} {r['path']} rpc={body.get('method')} id={body.get('id')} holds_session_id={[s for s in sessions if s in text]}")
    print("   headers:", json.dumps(r["headers"]))
    if body.get("method") in ("initialize", "tools/call", "server/discover"):
        print("   body:", json.dumps(body))
helper = run / "helper.jsonl"
for line in helper.read_text().splitlines() if helper.exists() else []:
    r = json.loads(line)
    start = json.loads((run / "start-env.json").read_text())
    print(f"helper call: cwd={r['cwd']} ppid={r['ppid']} stdin={r['stdin']!r}")
    print("   env added:", json.dumps({k: v for k, v in sorted(r["env"].items()) if k not in start}))
PY
