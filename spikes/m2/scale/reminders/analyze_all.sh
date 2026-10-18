#!/bin/bash
# Analyse the runs of one run_all.sh invocation into results/ (summary.md, CSVs).
# Usage: spikes/m2/scale/reminders/analyze_all.sh <runs_root>
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../../../.." && pwd)"
ROOT="$1"
PY="$REPO/.venv/bin/python"
mkdir -p "$S/results"
args=()
for r in opus55-resume opus55-stream sonnet46-resume sonnet46-stream haiku55-resume opus55-stream-todo \
         opus55-stream-plain sonnet46-stream-plain probe-claude-opus-4-7 probe-claude-sonnet-4-5 \
         probe-claude-haiku-4-5 probe-claude-sonnet-5 probe-claude-fable-5; do
  [ -d "$ROOT/$r" ] && args+=("$r=$ROOT/$r")
done
"$PY" "$S/analyze.py" "${args[@]}" --out "$S/results" > "$S/results/summary.md" 2>&1
for r in opus55-resume opus55-stream sonnet46-resume sonnet46-stream; do
  [ -d "$ROOT/$r" ] && "$PY" "$S/timeline.py" "$ROOT/$r" --csv "$S/results/timeline-$r.csv" > /dev/null
done
python3 -I "$S/features.py" "$S/results/reminders.csv" > "$S/results/features.md"
python3 -I "$S/table.py" "$S/results/turns.csv" > "$S/results/table.md"
echo "analysis: $S/results/summary.md"
