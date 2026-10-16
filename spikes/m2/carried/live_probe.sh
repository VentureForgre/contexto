#!/bin/bash
# Zero-token live multi-turn shape probe: a throwaway Claude Code session talks to
# fake_model_stub.py on loopback for six turns (turn 1 asks for a tool, so its turn has a tool
# loop; turns 4 and 6 change --effort), resuming the CLI-written session each time. Isolated config, dummy key, scrubbed env,
# pinned model. Output: <repo>/.contexto/spikes/M2-A2/live-shapes.jsonl
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../../.." && pwd)"
OUT="$REPO/.contexto/spikes/M2-A2"
TMP="$(cd "$(mktemp -d /tmp/m2-live.XXXXXX)" && pwd -P)"
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
mkdir -p "$OUT" "$TMP/cwd" "$TMP/config"
echo "synthetic file" > "$TMP/cwd/synthetic.txt"
: > "$OUT/live-shapes.jsonl"
python3 "$S/fake_model_stub.py" "$PORT" "$OUT/live-shapes.jsonl" "$TMP/cwd/synthetic.txt" > "$OUT/fake-stub.err" 2>&1 &
STUB=$!
trap 'kill $STUB 2>/dev/null; wait $STUB 2>/dev/null; rm -rf "$TMP"' EXIT
sleep 1
SID=$(python3 -c "import uuid; print(uuid.uuid4())")
turn() {  # turn <label> <claude args...>
  echo "{\"scenario\": \"$1\"}" >> "$OUT/live-shapes.jsonl"; shift
  (cd "$TMP/cwd" && env -i HOME="$HOME" PATH="$PATH" TERM=dumb CLAUDE_CONFIG_DIR="$TMP/config" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" ANTHROPIC_API_KEY="dummy-not-a-credential" \
    perl -e 'alarm 90; exec @ARGV' "$HOME/.local/bin/claude" -p --model claude-haiku-5-5 \
    --strict-mcp-config --allowedTools Read "$@" < /dev/null > /dev/null 2>&1)
  echo "  exit $?" >&2
}
turn turn1-with-tool --session-id "$SID" "Synthetic prompt one. USE_TOOL"
turn turn2 --resume "$SID" "Synthetic prompt two."
turn turn3 --resume "$SID" "Synthetic prompt three."
turn turn4-effort-high --resume "$SID" --effort high "Synthetic prompt four."
turn turn5 --resume "$SID" "Synthetic prompt five."
turn turn6-effort-low --resume "$SID" --effort low "Synthetic prompt six."
