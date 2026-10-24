#!/bin/bash
# usage: run.sh <name> <scenario.py> <prompt> [timeout]
# Runs Claude Code 2.1.302 in print mode against the local stub, with an isolated config dir.
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=${CX_SPIKE_DIR:-/tmp/cx-subagent-spike}
name=$1; scen=$2; prompt=$3; to=${4:-120}
port=$((18700 + RANDOM % 200))
cap=$root/cap-$name
rm -rf "$cap"; mkdir -p $root/config $root/work-$name
PYTHONPATH=$here python3 "$here/stub.py" $port "$cap" "$scen" > $root/stub-$name.log 2>&1 &
stub=$!
sleep 1
cd $root/work-$name
unset ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX
export CLAUDE_CONFIG_DIR=$root/config
export ANTHROPIC_BASE_URL=http://127.0.0.1:$port
export ANTHROPIC_API_KEY=sk-ant-test-stub-0000000000
export DISABLE_TELEMETRY=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1
timeout "$to" claude -p "$prompt" --dangerously-skip-permissions --model claude-haiku-5-5 > $root/out-$name.txt 2>&1
echo "rc=$?"
kill $stub 2>/dev/null
ls "$cap" | head -50
