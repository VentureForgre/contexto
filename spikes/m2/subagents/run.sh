#!/bin/bash
# usage: run.sh <name> <scenario.py> <prompt> [timeout]
# Runs Claude Code in print mode against the local stub, under env -i with an isolated config dir and
# HOME and the auto-updater off. CLAUDE=<binary> picks the Claude Code to run (default: claude on PATH).
# Exits 1 without running Claude Code if the stub is not listening; otherwise with Claude Code's status.
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=${CX_SPIKE_DIR:-/tmp/cx-subagent-spike}
name=$1; scen=$2; prompt=$3; to=${4:-120}
claude=${CLAUDE:-$(command -v claude)}
port=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
cap=$root/cap-$name
rm -rf "$cap"; mkdir -p $root/config $root/home $root/work-$name
PYTHONPATH=$here python3 "$here/stub.py" $port "$cap" "$scen" > $root/stub-$name.log 2>&1 &
stub=$!
trap 'kill $stub 2>/dev/null; wait $stub 2>/dev/null' EXIT
up=0
for _ in $(seq 50); do  # the stub prints "listening" once it holds the port; a bind error ends it
  grep -qx listening $root/stub-$name.log && { up=1; break; }
  kill -0 $stub 2>/dev/null || break
  sleep 0.1
done
if [ $up = 0 ]; then
  echo "stub not listening on port $port (see $root/stub-$name.log)"
  exit 1
fi
cd $root/work-$name
env -i HOME="$root/home" PATH="$PATH" TERM=dumb CLAUDE_CONFIG_DIR="$root/config" \
  ANTHROPIC_BASE_URL=http://127.0.0.1:$port ANTHROPIC_API_KEY=sk-ant-test-stub-0000000000 \
  DISABLE_TELEMETRY=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 \
  timeout "$to" "$claude" -p "$prompt" --dangerously-skip-permissions --model claude-haiku-5-5 \
  < /dev/null > $root/out-$name.txt 2>&1
rc=$?
echo "rc=$rc"
ls "$cap" | head -50
exit $rc
