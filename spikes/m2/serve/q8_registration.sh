#!/bin/bash
# D70 spike, question 8: which registrations make a plain `claude -p` (no --mcp-config, not --bare,
# not --strict-mcp-config) load a stdio MCP server. Zero spend: every run goes through mcp_probe.py.
# Prints one verdict line per variant; raw captures in /tmp/d70-mcp/q8-*/.
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=/tmp/d70-mcp
py=$(command -v python3)
probe() { python3 "$here/mcp_probe.py" "$@"; }
allow='{"permissions":{"allow":["mcp__probe__probe_echo"]}}'
server() { echo "{\"type\":\"stdio\",\"command\":\"$py\",\"args\":[\"$here/recording_mcp.py\",\"$1\"]}"; }
seed() { rm -rf "$root/$1"; mkdir -p "$root/$1/config" "$root/$1/work" "$root/$1/home"; }
verdict() {  # <run> <log>: did the server start, is the tool named in the first model request, was it called
  python3 - "$root/$1" "$2" <<'PY'
import json, sys
from pathlib import Path
run, log = Path(sys.argv[1]), Path(sys.argv[2])
recs = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
first = sorted((run / "stub").glob("*.json"))
body = json.loads(first[0].read_text())["body"] if first else {}
names = [t.get("name") for t in body.get("tools") or []]
print(f"{run.name}: server_started={any(r['kind'] == 'start' for r in recs)}"
      f" in_tools={'mcp__probe__probe_echo' in names} named_in_request={'mcp__probe__probe_echo' in json.dumps(body)}"
      f" tools_call={sum(1 for r in recs if r['kind'] == 'message' and r['message'].get('method') == 'tools/call')}"
      f" stub_requests={len(first)}")
PY
}
run_p() {  # <run> <reuse> [extra mcp_probe args...]: plain claude -p asking for one probe call
  local run=$1 reuse=$2; shift 2
  probe "$run" --reuse "$reuse" "$@" -- -p --model claude-haiku-5-5 --output-format json "D70CALL1 $run" > "$root/$run.summary" 2>&1
}

# 1. claude mcp add-json --scope user
seed q8-addjson
probe q8-addjson --no-stub -- mcp add-json --scope user probe "$(server $root/q8-user/mcp.jsonl)" > "$root/q8-addjson.summary" 2>&1
echo "add-json wrote: $(cd $root/q8-addjson/config && find . -type f | tr '\n' ' ')"
cp "$root/q8-addjson/config/.claude.json" "$root/q8-addjson/claude.json.after-add-json"
echo "$allow" > "$root/q8-addjson/config/settings.json"
mkdir -p "$root/q8-user"; run_p q8-user q8-addjson; verdict q8-user "$root/q8-user/mcp.jsonl"
mkdir -p "$root/q8-user-nosearch"
python3 - "$root/q8-addjson/config/.claude.json" "$root/q8-user-nosearch/mcp.jsonl" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p)); c["mcpServers"]["probe"]["args"][-1] = sys.argv[2]; json.dump(c, open(p, "w"), indent=2)
PY
run_p q8-user-nosearch q8-addjson --env ENABLE_TOOL_SEARCH=false; verdict q8-user-nosearch "$root/q8-user-nosearch/mcp.jsonl"

# 2. claude mcp add with each scope: where does it write
seed q8-add
for scope in user local project; do
  probe "q8-add-$scope" --reuse q8-add --no-stub -- mcp add --scope $scope "probe$scope" -- "$py" "$here/recording_mcp.py" "$root/q8-add/mcp-$scope.jsonl" > "$root/q8-add-$scope.summary" 2>&1
done
echo "mcp add wrote: $(cd $root/q8-add && find config work -type f | tr '\n' ' ')"

# 3. a hand-written $CLAUDE_CONFIG_DIR/.claude.json holding only mcpServers
seed q8-handwritten
echo "{\"mcpServers\":{\"probe\":$(server $root/q8-handwritten/mcp.jsonl)}}" > "$root/q8-handwritten/config/.claude.json"
echo "$allow" > "$root/q8-handwritten/config/settings.json"
run_p q8-handwritten q8-handwritten; verdict q8-handwritten "$root/q8-handwritten/mcp.jsonl"

# 4. a project .mcp.json, without and with approval settings
seed q8-mcpjson
echo "{\"mcpServers\":{\"probe\":$(server $root/q8-mcpjson/mcp.jsonl)}}" > "$root/q8-mcpjson/work/.mcp.json"
echo "$allow" > "$root/q8-mcpjson/config/settings.json"
run_p q8-mcpjson q8-mcpjson; verdict q8-mcpjson "$root/q8-mcpjson/mcp.jsonl"
mkdir -p "$root/q8-mcpjson-all"
echo "{\"mcpServers\":{\"probe\":$(server $root/q8-mcpjson-all/mcp.jsonl)}}" > "$root/q8-mcpjson/work/.mcp.json"
echo '{"permissions":{"allow":["mcp__probe__probe_echo"]},"enableAllProjectMcpServers":true}' > "$root/q8-mcpjson/config/settings.json"
run_p q8-mcpjson-all q8-mcpjson; verdict q8-mcpjson-all "$root/q8-mcpjson-all/mcp.jsonl"
mkdir -p "$root/q8-mcpjson-named"
echo "{\"mcpServers\":{\"probe\":$(server $root/q8-mcpjson-named/mcp.jsonl)}}" > "$root/q8-mcpjson/work/.mcp.json"
echo '{"permissions":{"allow":["mcp__probe__probe_echo"]},"enabledMcpjsonServers":["probe"]}' > "$root/q8-mcpjson/config/settings.json"
run_p q8-mcpjson-named q8-mcpjson; verdict q8-mcpjson-named "$root/q8-mcpjson-named/mcp.jsonl"

# 5. a settings file that declares mcpServers by itself: user settings.json, then --settings
seed q8-settings-user
echo "{\"permissions\":{\"allow\":[\"mcp__probe__probe_echo\"]},\"mcpServers\":{\"probe\":$(server $root/q8-settings-user/mcp.jsonl)}}" > "$root/q8-settings-user/config/settings.json"
run_p q8-settings-user q8-settings-user; verdict q8-settings-user "$root/q8-settings-user/mcp.jsonl"
seed q8-settings-flag
echo "{\"permissions\":{\"allow\":[\"mcp__probe__probe_echo\"]},\"mcpServers\":{\"probe\":$(server $root/q8-settings-flag/mcp.jsonl)}}" > "$root/q8-settings-flag/flag-settings.json"
probe q8-settings-flag -- -p --settings "$root/q8-settings-flag/flag-settings.json" --model claude-haiku-5-5 --output-format json "D70CALL1 q8-settings-flag" > "$root/q8-settings-flag.summary" 2>&1
verdict q8-settings-flag "$root/q8-settings-flag/mcp.jsonl"
