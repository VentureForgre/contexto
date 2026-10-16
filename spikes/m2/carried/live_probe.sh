#!/bin/bash
# Zero-token live multi-turn shape probe: a throwaway Claude Code session talks to
# fake_model_stub.py on loopback for six turns (turn 1 asks for a tool, so its turn has a tool
# loop; turns 4 and 6 change --effort), resuming the CLI-written session each time; then a
# four-turn session with a per-turn context hook, a two-turn session with UserPromptSubmit and
# PostToolUse hooks (turn 1 runs a tool), and a four-turn session that adds and removes an
# MCP server. Isolated config, dummy key, scrubbed env,
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

# Hook case: a UserPromptSubmit hook that adds per-turn context (as OpenRig seats do).
HOOKS="$TMP/hook-settings.json"
printf '{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 %s"}]}]}}\n' \
  "$S/hook_context.py" > "$HOOKS"
SID2=$(python3 -c "import uuid; print(uuid.uuid4())")
turn hook-turn1 --session-id "$SID2" --settings "$HOOKS" "Synthetic hook prompt one."
turn hook-turn2 --resume "$SID2" --settings "$HOOKS" "Synthetic hook prompt two."
turn hook-turn3 --resume "$SID2" --settings "$HOOKS" "Synthetic hook prompt three."
turn hook-turn4 --resume "$SID2" --settings "$HOOKS" "Synthetic hook prompt four."

# PostToolUse case: hooks on both UserPromptSubmit and PostToolUse; turn 1 runs a tool.
HOOKS2="$TMP/hook-settings-2.json"
printf '{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 %s"}]}], "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 %s"}]}]}}\n' \
  "$S/hook_context.py" "$S/hook_context.py" > "$HOOKS2"
SID4=$(python3 -c "import uuid; print(uuid.uuid4())")
turn posttool-turn1-tool --session-id "$SID4" --settings "$HOOKS2" "Synthetic tool prompt one. USE_TOOL"
turn posttool-turn2 --resume "$SID4" --settings "$HOOKS2" "Synthetic tool prompt two."

# Tool-addition case: a resume that adds an MCP server (its tools are sent upfront behind a
# non-first-party base URL), then a resume that drops it again.
MCP="$TMP/mcp.json"
printf '{"mcpServers": {"probe": {"command": "python3", "args": ["%s"]}}}\n' "$S/mcp_probe_server.py" > "$MCP"
SID3=$(python3 -c "import uuid; print(uuid.uuid4())")
turn mcp-turn1-none --session-id "$SID3" "Synthetic tools prompt one."
turn mcp-turn2-added --mcp-config "$MCP" --resume "$SID3" "Synthetic tools prompt two."  # --mcp-config takes several values
turn mcp-turn3-kept --mcp-config "$MCP" --resume "$SID3" "Synthetic tools prompt three."
turn mcp-turn4-removed --resume "$SID3" "Synthetic tools prompt four."
