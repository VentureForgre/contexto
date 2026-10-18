#!/bin/bash
# Zero-spend matrix: resume and stream sessions of 48 turns on three models, then the analysis.
# Raw bodies stay under $ROOT (a temp dir, deleted by nothing: inspect, then remove by hand).
# Usage: spikes/m2/scale/reminders/run_all.sh [turns]   (from the repo root, after the venv setup)
# Wall time about 1 minute for 48 turns on an M-series Mac.
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../../../.." && pwd)"
TURNS="${1:-48}"
ROOT="$(cd "$(mktemp -d /tmp/rem-runs.XXXXXX)" && pwd -P)"
mkdir -p "$S/results"
echo "runs under $ROOT"
run() {  # run <label> <mode> <model> [--env K=V ...]
  local label=$1 mode=$2 model=$3; shift 3
  python3 "$S/run_session.py" --mode "$mode" --turns "$TURNS" --model "$model" --root "$ROOT/$label" "$@" \
    > "$ROOT/$label.log" 2>&1
  echo "$label exit $? ($(grep -c '^turn ' "$ROOT/$label.log") turns, non-loopback lines: $(grep -c 'bad=[1-9]' "$ROOT/$label.log"))"
}
run opus55-resume resume claude-opus-5-5
run opus55-stream stream claude-opus-5-5
run sonnet46-resume resume claude-sonnet-4-6
run sonnet46-stream stream claude-sonnet-4-6
run haiku55-resume resume claude-haiku-5-5
run opus55-stream-todo stream claude-opus-5-5 --env CLAUDE_CODE_ENABLE_TODO_TOOLS=1
run opus55-stream-plain stream claude-opus-5-5 --plain
run sonnet46-stream-plain stream claude-sonnet-4-6 --plain
# Placement probes: the first 12-turn cycle (every feature once) on more model ids. The fake upstream
# accepts any id, so these show how the CLI's id-based behaviour places reminders, nothing more.
for m in claude-opus-4-7 claude-sonnet-4-5 claude-haiku-4-5 claude-sonnet-5 claude-fable-5; do
  TURNS_SAVE=$TURNS; TURNS=12; run "probe-$m" stream "$m"; TURNS=$TURNS_SAVE
done
"$S/analyze_all.sh" "$ROOT"
