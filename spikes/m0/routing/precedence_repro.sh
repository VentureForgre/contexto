#!/bin/bash
# Zero-token repro: which base URL a Claude Code process uses when its cwd is a linked git worktree.
#
# Builds a temporary git repo with a linked worktree. The main checkout's and the worktree's
# .claude/settings.local.json each set env.ANTHROPIC_BASE_URL to a different loopback stub, and the
# process environment points at a third stub. Each stub (spikes/m0/interception/stub401.py) answers
# 401 and logs method, path, and header names only. Case 1 runs from the worktree with no flag-level
# override; case 2 adds a flag-level --settings base URL pointing at a fourth stub. The CLI gets an
# isolated CLAUDE_CONFIG_DIR, a dummy key that is not a credential, a scrubbed environment, and a
# pinned model, so no model is called. Output goes under <repo>/.contexto/spikes/L1-f/.
#
# Usage: spikes/m0/routing/precedence_repro.sh
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../../.." && pwd)"
OUT="$REPO/.contexto/spikes/L1-f"
STUB="$REPO/spikes/m0/interception/stub401.py"
CLAUDE_BIN="$HOME/.local/bin/claude"
mkdir -p "$OUT"
TMP="$(cd "$(mktemp -d /tmp/m0-routing.XXXXXX)" && pwd -P)"
PIDS=()
cleanup() {
  local p
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; wait "$p" 2>/dev/null; done
  # Remove the linked worktree registration before deleting the files.
  git -C "$TMP/main" worktree remove --force "$TMP/wt" > /dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

free_port() { python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])"; }
start_stub() {  # start_stub <name> -> sets PORT_<name>
  local port; port=$(free_port)
  : > "$OUT/stub-$1.log"
  python3 "$STUB" "$port" "$OUT/stub-$1.log" > /dev/null 2>&1 &
  PIDS+=($!)
  eval "PORT_$1=$port"
}
for name in env main worktree flag; do start_stub "$name"; done
sleep 1

git init -q "$TMP/main"
git -C "$TMP/main" -c user.email=repro@example.invalid -c user.name=repro commit -q --allow-empty -m init
git -C "$TMP/main" worktree add -q "$TMP/wt" -b wt > /dev/null 2>&1
for d in main wt; do mkdir -p "$TMP/$d/.claude"; done
printf '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%s"}}\n' "$PORT_main" > "$TMP/main/.claude/settings.local.json"
printf '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%s"}}\n' "$PORT_worktree" > "$TMP/wt/.claude/settings.local.json"

run() {  # run <extra claude args...>
  (cd "$TMP/wt" && env -i HOME="$HOME" PATH="$PATH" TERM=dumb CLAUDE_CONFIG_DIR="$TMP/config" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT_env" ANTHROPIC_API_KEY="dummy-not-a-credential" \
    CLAUDE_CODE_MAX_RETRIES=0 \
    perl -e 'alarm 60; exec @ARGV' "$CLAUDE_BIN" -p --model claude-haiku-5-5 --strict-mcp-config "$@" hi \
    < /dev/null > /dev/null 2>&1)
}
report() {
  local name
  for name in env main worktree flag; do
    python3 - "$OUT/stub-$name.log" "$name" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
hits = {}
for r in rows:
    key = f"{r['method']} {r['path']}"
    hits[key] = hits.get(key, 0) + 1
print(f"  stub {sys.argv[2]:9s} {hits or 'no requests'}")
PY
    : > "$OUT/stub-$name.log"
  done
}

echo "case 1: cwd = linked worktree, no flag-level override"
run --settings '{"disableAllHooks": true}'
report
echo "case 2: same, plus flag-level --settings env.ANTHROPIC_BASE_URL"
run --settings "{\"disableAllHooks\": true, \"env\": {\"ANTHROPIC_BASE_URL\": \"http://127.0.0.1:$PORT_flag\"}}"
report
