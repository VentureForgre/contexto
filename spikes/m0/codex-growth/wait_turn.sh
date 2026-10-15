#!/bin/bash
# Block until a Codex rollout has at least <n> task_complete events, or time out.
# Usage: wait_turn.sh <rollout.jsonl> <n> [timeout_seconds]
f="$1"; n="$2"; limit="${3:-600}"; start=$(date +%s)
while true; do
  # Codex writes compact JSON ("type":"task_complete"); allow spaces in case that changes.
  c=$(grep -cE '"type": *"task_complete"' "$f" 2>/dev/null || true)
  if [ "${c:-0}" -ge "$n" ]; then echo "task_complete=$c after $(( $(date +%s) - start ))s"; exit 0; fi
  if [ $(( $(date +%s) - start )) -ge "$limit" ]; then echo "timeout: task_complete=$c"; exit 1; fi
  sleep 2
done
