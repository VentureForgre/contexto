#!/bin/bash
# One H4 rollover measurement: a fresh throwaway Claude Code session seeded with a synthetic
# handoff (piped on stdin) and asked one recall question. Uses the lane 1 live-call recipe:
# cwd under the worktree's .contexto/spikes/L1-c/ (inherits the seat credential as configured),
# seat env stripped, flag-level --settings with disableAllHooks, --strict-mcp-config, pinned model.
# Prints wall-clock seconds, the CLI's reported duration, whether the answer matched, and the
# session JSONL usage (numbers only). Usage: run_seed.sh <label> <handoff.md> <key_index>
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../../.." && pwd)"
OUT="$REPO/.contexto/spikes/L1-c"
label="$1"; handoff="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; key="$3"
cwd="$OUT/cwd"; mkdir -p "$cwd"; cwd="$(cd "$cwd" && pwd -P)"
if [ "$key" = 0 ]; then
  question="Reply with only the word OK."; expected="OK"
else
  question="Using only the handoff above, reply with only the value of key-$(printf %04d "$key")."
  expected=$(python3 -c "import sys; sys.path.insert(0, '$S'); from make_handoff import value; print(value($key))")
fi
strip=$(env | grep -oE '^(OPENRIG|RIGGED)_[A-Z_]+' | sed 's/^/-u /' | tr '\n' ' ')
t0=$(python3 -c 'import time; print(time.time())')
(cd "$cwd" && env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_CHILD_SESSION \
  -u CLAUDE_CODE_MESSAGING_SOCKET -u CLAUDE_CODE_MESSAGING_TOKEN -u CLAUDE_PID \
  -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SESSION_ATTENDED -u CLAUDE_CODE_EXECPATH $strip \
  perl -e 'alarm 180; exec @ARGV' claude -p --model claude-haiku-5-5 \
  --settings '{"disableAllHooks": true}' --strict-mcp-config --output-format json "$question" \
  < "$handoff" > "$OUT/run-$label.json" 2> "$OUT/run-$label.err")
rc=$?
t1=$(python3 -c 'import time; print(time.time())')
python3 - "$OUT/run-$label.json" "$expected" "$label" "$rc" "$t0" "$t1" "$cwd" "$S" <<'PY'
import json, re, subprocess, sys, os
path, expected, label, rc, t0, t1, cwd, s = sys.argv[1:9]
d = json.load(open(path))
ans = (d.get("result") or "").strip().strip("`").strip()
sid = d.get("session_id")
proj = os.path.expanduser("~/.claude/projects/" + re.sub(r"[^A-Za-z0-9]", "-", cwd))
print(f"label={label} rc={rc} wall_s={float(t1) - float(t0):.1f} duration_ms={d.get('duration_ms')} "
      f"duration_api_ms={d.get('duration_api_ms')} is_error={d.get('is_error')} answer_matches={ans == expected} "
      f"session={sid}")
print(subprocess.run([sys.executable, f"{s}/session_usage.py", f"{proj}/{sid}.jsonl"],
                     capture_output=True, text=True).stdout, end="")
PY
