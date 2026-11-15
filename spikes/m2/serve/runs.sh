#!/bin/bash
# D70 spike, questions 1 to 7: every stdio run, in order. Zero spend (see mcp_probe.py).
# Captures land in /tmp/d70-mcp/<run>/ and a short reading of each in /tmp/d70-mcp/<run>.show.
# Question 8 is q8_registration.sh, question 9 is q9_http.sh, the interactive /clear is interactive_probe.py.
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=/tmp/d70-mcp
py=$(command -v python3)
hook="$py $here/hook_record.py"
model=(--model claude-haiku-5-5)

mk() {  # <run> [settings json to merge]: a fresh run directory with mcp.json and settings.json
  rm -rf "$root/$1"; mkdir -p "$root/$1"
  python3 - "$root/$1" "$here" "${2:-{\}}" <<'PY'
import json, sys
run, here, extra = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
json.dump({"mcpServers": {"probe": {"command": sys.executable, "args": [here + "/recording_mcp.py", run + "/mcp.jsonl"],
           "env": {"D70_MCP_CONFIG_ENV": "from-mcp-config-env"}}}}, open(run + "/mcp.json", "w"), indent=1)
settings = {"env": {"D70_SETTINGS_ENV": "from-settings-env-block"}, "permissions": {"allow": ["mcp__probe__probe_echo"]}}
settings.update(extra)
json.dump(settings, open(run + "/settings.json", "w"), indent=1)
PY
}
hooks() {  # <run>: settings with recording SessionStart, PreToolUse and PostToolUse hooks
  local h="{\"type\":\"command\",\"command\":\"$hook $root/$1/hooks.jsonl\"}"
  echo "{\"hooks\":{\"SessionStart\":[{\"hooks\":[$h]}],\"PreToolUse\":[{\"matcher\":\"mcp__probe__.*\",\"hooks\":[$h]}],\"PostToolUse\":[{\"matcher\":\"mcp__probe__.*\",\"hooks\":[$h]}]},\"permissions\":{\"allow\":[\"mcp__probe__probe_echo\",\"Bash\"]}}"
}
user() { echo "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":\"$1\"}}"; }
probe() { local run=$1; shift; python3 "$here/mcp_probe.py" "$run" "$@" > "$root/$run.summary" 2>&1; python3 "$here/mcp_show.py" "$run" > "$root/$run.show" 2>&1; echo "$run done"; }
cfg=(--settings "{run}/settings.json" --mcp-config "{run}/mcp.json")
stream=(--input-format stream-json --output-format stream-json --verbose)

# Q1 to Q3: --bare, two calls in one turn
mk q3-bare
probe q3-bare -- -p --bare "${cfg[@]}" --strict-mcp-config "${model[@]}" --output-format json "D70CALL2 please"
# Q3: not bare, two calls in turn one and one call in turn two
mk q3-turns; { user "D70CALL2 first turn"; user "D70CALL1 second turn"; } > "$root/q3-turns/stdin.jsonl"
probe q3-turns --stdin-file "{run}/stdin.jsonl" --stdin-delay 8 -- -p "${cfg[@]}" "${model[@]}" "${stream[@]}"
# Q4: main starts a subagent, the subagent calls the tool
mk q4-sub
probe q4-sub -- -p "${cfg[@]}" "${model[@]}" --output-format json "D70AGENT go"
# Q5: two claude processes at once, one config
mk q5-two
probe q5-two --parallel 2 -- -p "${cfg[@]}" "${model[@]}" --output-format json "D70CALL1 parallel"
# Q6: --session-id, --resume, --resume --fork-session, /clear
sid=11111111-2222-4333-8444-555555555555
mk q6-sid
probe q6-sid -- -p --session-id $sid "${cfg[@]}" "${model[@]}" --output-format json "D70CALL1 with session id"
mk q6-resume
probe q6-resume --reuse q6-sid -- -p --resume $sid "${cfg[@]}" "${model[@]}" --output-format json "D70CALL1 after resume"
mk q6-fork
probe q6-fork --reuse q6-sid -- -p --resume $sid --fork-session "${cfg[@]}" "${model[@]}" --output-format json "D70CALL1 after fork"
mk q6-clear; { user "D70CALL1 before clear"; user "/clear"; user "D70CALL1 after clear"; } > "$root/q6-clear/stdin.jsonl"
probe q6-clear --stdin-file "{run}/stdin.jsonl" --stdin-delay 8 -- -p "${cfg[@]}" "${model[@]}" "${stream[@]}"
# Q7: hooks (not bare, with /clear and a Bash call each side of it), hooks with --bare, on --resume, in a subagent
mk q7-hooks "$(hooks q7-hooks)"
{ user "D70CALL1 before clear"; user "D70BASH before clear"; user "/clear"; user "D70CALL1 after clear"; user "D70BASH after clear"; } > "$root/q7-hooks/stdin.jsonl"
probe q7-hooks --stdin-file "{run}/stdin.jsonl" --stdin-delay 7 --timeout 120 -- -p "${cfg[@]}" "${model[@]}" "${stream[@]}"
mk q7-hooks-bare "$(hooks q7-hooks-bare)"
probe q7-hooks-bare -- -p --bare "${cfg[@]}" --strict-mcp-config "${model[@]}" --output-format json "D70CALL1 bare hooks"
first=$(python3 -c "import json; print(json.loads(open('$root/q7-hooks/hooks.jsonl').readline())['stdin']['session_id'])")
mk q7-resume "$(hooks q7-resume)"
probe q7-resume --reuse q7-hooks -- -p --resume "$first" "${cfg[@]}" "${model[@]}" --output-format json "D70CALL1 resumed"
mk q7-sub "$(hooks q7-sub)"
probe q7-sub -- -p "${cfg[@]}" "${model[@]}" --output-format json "D70AGENT go"
