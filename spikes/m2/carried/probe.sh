#!/bin/bash
# Zero-token probe of where Claude Code 2.1.301 places carried content: the blocks of
# messages[0] and every role "system" message. Runs throwaway Claude Code processes against
# shape_stub.py (loopback, 401, structure only) with an isolated config dir, a dummy key that is
# not a credential, a scrubbed environment, and a pinned model.
# Scenarios: fresh turn; resumed synthetic history of two text turns; resumed synthetic history
# with a tool loop. Output: <repo>/.contexto/spikes/M2-A2/shapes.jsonl
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../../.." && pwd)"
OUT="$REPO/.contexto/spikes/M2-A2"
TMP="$(cd "$(mktemp -d /tmp/m2-carried.XXXXXX)" && pwd -P)"
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
mkdir -p "$OUT" "$TMP/cwd" "$TMP/config"
: > "$OUT/shapes.jsonl"
python3 "$S/shape_stub.py" "$PORT" "$OUT/shapes.jsonl" > /dev/null 2>&1 &
STUB=$!
trap 'kill $STUB 2>/dev/null; wait $STUB 2>/dev/null; rm -rf "$TMP"' EXIT
sleep 1

claude_run() {  # claude_run <label> <args...>
  echo "{\"scenario\": \"$1\"}" >> "$OUT/shapes.jsonl"; shift
  (cd "$TMP/cwd" && env -i HOME="$HOME" PATH="$PATH" TERM=dumb CLAUDE_CONFIG_DIR="$TMP/config" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" ANTHROPIC_API_KEY="dummy-not-a-credential" \
    CLAUDE_CODE_MAX_RETRIES=0 perl -e 'alarm 60; exec @ARGV' "$HOME/.local/bin/claude" -p \
    --model claude-haiku-5-5 --strict-mcp-config "$@" < /dev/null > /dev/null 2>&1)
}

synth() {  # synth <kind> -> prints a session id; kind = text2 | toolloop
  python3 - "$TMP/config/projects" "$TMP/cwd" "$1" <<'PY'
import json, os, re, sys, uuid
from datetime import UTC, datetime
projects, cwd, kind = sys.argv[1:4]
sid = str(uuid.uuid4())
now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
common = {"cwd": cwd, "sessionId": sid, "isSidechain": False, "userType": "external",
          "version": "2.1.301", "gitBranch": "", "timestamp": now}
usage = {"input_tokens": 1000, "output_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
recs, parent = [], None
def add(rtype, message, **extra):
    global parent
    u = str(uuid.uuid4())
    recs.append({**common, "type": rtype, "uuid": u, "parentUuid": parent, "message": message, **extra})
    parent = u
def assistant(content, mid):
    add("assistant", {"id": mid, "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
                      "content": content, "stop_reason": "end_turn", "stop_sequence": None, "usage": usage})
add("user", {"role": "user", "content": "First synthetic prompt."})
if kind == "toolloop":
    assistant([{"type": "tool_use", "id": "toolu_synth01", "name": "Bash",
                "input": {"command": "echo synthetic", "description": "synthetic"}}], "msg_synth01")
    add("user", {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_synth01",
                                              "content": "synthetic", "is_error": False}]},
        toolUseResult={"stdout": "synthetic", "stderr": "", "interrupted": False})
    assistant([{"type": "text", "text": "Synthetic answer after tool."}], "msg_synth02")
else:
    assistant([{"type": "text", "text": "Synthetic answer one."}], "msg_synth01")
    add("user", {"role": "user", "content": "Second synthetic prompt."})
    assistant([{"type": "text", "text": "Synthetic answer two."}], "msg_synth02")
d = os.path.join(projects, re.sub(r"[^A-Za-z0-9]", "-", cwd)); os.makedirs(d, exist_ok=True)
with open(os.path.join(d, sid + ".jsonl"), "w") as f:
    for r in recs: f.write(json.dumps(r) + "\n")
print(sid)
PY
}

claude_run fresh "synthetic new prompt"
claude_run resumed-text2 --resume "$(synth text2)" "synthetic new prompt"
claude_run resumed-toolloop --resume "$(synth toolloop)" "synthetic new prompt"
