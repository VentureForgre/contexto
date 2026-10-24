#!/bin/bash
# Zero-spend first check of a CLI version (D38, docs/cli-updates.md): one `claude -p` and one
# `codex exec` request to a loopback stub that answers 401, then classify.py reads them with
# Contexto's recognisers. Each CLI runs under env -i with a temp HOME, CLAUDE_CONFIG_DIR or
# CODEX_HOME, and a fake key; nothing reaches a model. Bodies stay in the printed temp directory.
# Usage (repo root, venv active): spikes/m2/cliversion/check.sh [claude-binary] [codex-binary]
set -u
S="$(cd "$(dirname "$0")" && pwd)"
CLAUDE="${1:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
CODEX="${2:-$(command -v codex || echo "$HOME/.local/bin/codex")}"
TMP="$(cd "$(mktemp -d /tmp/cx-cliversion.XXXXXX)" && pwd -P)"
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
mkdir -p "$TMP/home" "$TMP/claude-config" "$TMP/codex-home" "$TMP/cwd"
python3 "$S/stub.py" "$PORT" "$TMP" > /dev/null 2>&1 &
STUB=$!
trap 'kill $STUB 2>/dev/null; wait $STUB 2>/dev/null' EXIT
sleep 1
echo "captures under $TMP"
echo "claude --version: $("$CLAUDE" --version 2>&1 | head -1)"
echo "codex --version: $("$CODEX" --version 2>&1 | head -1)"
cd "$TMP/cwd" || exit 1
env -i HOME="$TMP/home" PATH="$PATH" TERM=dumb CLAUDE_CONFIG_DIR="$TMP/claude-config" \
  ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" ANTHROPIC_API_KEY=sk-ant-fake-not-a-key \
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_TELEMETRY=1 \
  perl -e 'alarm 40; exec @ARGV' "$CLAUDE" -p "synthetic probe" < /dev/null > "$TMP/claude.out" 2>&1
printf '{"OPENAI_API_KEY":"dummy-not-a-credential"}' > "$TMP/codex-home/auth.json"
env -i HOME="$TMP/home" PATH="$PATH" TERM=dumb CODEX_HOME="$TMP/codex-home" \
  perl -e 'alarm 40; exec @ARGV' "$CODEX" exec --skip-git-repo-check \
  -c "model_providers.cx={name=\"cliversion-probe\",base_url=\"http://127.0.0.1:$PORT/v1\",wire_api=\"responses\",requires_openai_auth=true}" \
  -c model_provider=cx -c model=gpt-6-astra -c analytics.enabled=false -c features.plugins=false \
  "synthetic probe" < /dev/null > "$TMP/codex.out" 2>&1
cd "$S/../../.." || exit 1
python3 "$S/classify.py" "$TMP"
