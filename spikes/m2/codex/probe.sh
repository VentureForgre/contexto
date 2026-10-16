#!/bin/bash
# Zero-token Codex 0.154.0 probes against responses_stub.py (loopback, 401, structure only).
# Each scenario uses its own temporary CODEX_HOME, env -i, and synthetic credentials that are not
# real (a dummy key, or a synthetic ChatGPT-login auth.json with unsigned fake tokens).
# Output: <repo>/.contexto/spikes/M2-A5/codex-shapes.jsonl
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../../.." && pwd)"
OUT="$REPO/.contexto/spikes/M2-A5"
TMP="$(cd "$(mktemp -d /tmp/m2-codex.XXXXXX)" && pwd -P)"
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
mkdir -p "$OUT" "$TMP/cwd"
: > "$OUT/codex-shapes.jsonl"
python3 "$S/responses_stub.py" "$PORT" "$OUT/codex-shapes.jsonl" > /dev/null 2>&1 &
STUB=$!
trap 'kill $STUB 2>/dev/null; wait $STUB 2>/dev/null; rm -rf "$TMP"' EXIT
sleep 1
BASE="http://127.0.0.1:$PORT/v1"

run() {  # run <label> <home> <extra env assignment or ''> <codex args...>
  local label="$1" home="$2" extra="$3"; shift 3
  mkdir -p "$home"
  echo "{\"scenario\": \"$label\"}" >> "$OUT/codex-shapes.jsonl"
  (cd "$TMP/cwd" && env -i HOME="$HOME" PATH="$PATH" TERM=dumb CODEX_HOME="$home" $extra \
    perl -e 'alarm 60; exec @ARGV' "$HOME/.local/bin/codex" exec --skip-git-repo-check "$@" "synthetic probe prompt" \
    < /dev/null > "$OUT/$label.out" 2>&1)
  echo "$label exit $?"
}

PROVIDER="model_providers.cx={name=\"contexto-probe\",base_url=\"$BASE\",wire_api=\"responses\""
# 1. Custom provider defined only with -c, env_key auth with a dummy key.
run custom-envkey "$TMP/h1" "CONTEXTO_PROBE_KEY=dummy-not-a-credential" \
  -c "$PROVIDER,env_key=\"CONTEXTO_PROBE_KEY\"}" -c model_provider=cx -c model=gpt-6-astra
# 2. Custom provider with requires_openai_auth, API-key mode (synthetic key in a temp auth.json).
printf '{"OPENAI_API_KEY": "dummy-not-a-credential"}\n' > /dev/null
mkdir -p "$TMP/h2"; printf '{"OPENAI_API_KEY":"dummy-not-a-credential"}' > "$TMP/h2/auth.json"
run custom-openai-auth-apikey "$TMP/h2" "" \
  -c "$PROVIDER,requires_openai_auth=true}" -c model_provider=cx -c model=gpt-6-astra
# 3. Same, ChatGPT-login mode with a synthetic, unsigned auth.json (not a real credential).
mkdir -p "$TMP/h3"
python3 - "$TMP/h3/auth.json" <<'PY'
import base64, json, sys, time
def jwt(claims):
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(claims)}.c3ludGhldGlj"
auth = {"https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_account_id": "acct-synthetic",
                                        "chatgpt_user_id": "user-synthetic"}}
claims = {"email": "probe@example.invalid", "exp": int(time.time()) + 86400, **auth}
json.dump({"OPENAI_API_KEY": None, "tokens": {"id_token": jwt(claims), "access_token": jwt(claims),
           "refresh_token": "synthetic-refresh", "account_id": "acct-synthetic"},
           "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, open(sys.argv[1], "w"))
PY
run custom-openai-auth-chatgpt "$TMP/h3" "" \
  -c "$PROVIDER,requires_openai_auth=true}" -c model_provider=cx -c model=gpt-6-astra
# 4. Built-in openai provider pointed at the stub (M0 shape: WebSocket attempts, then HTTP).
run builtin-openai-base "$TMP/h4" "OPENAI_API_KEY=dummy-not-a-credential CODEX_API_KEY=dummy-not-a-credential" \
  -c "openai_base_url=\"$BASE\"" -c model=gpt-6-astra
# 5. MCP server registered with -c only.
run mcp-registered "$TMP/h5" "CONTEXTO_PROBE_KEY=dummy-not-a-credential" \
  -c "$PROVIDER,env_key=\"CONTEXTO_PROBE_KEY\"}" -c model_provider=cx -c model=gpt-6-astra \
  -c "mcp_servers.contexto.command=\"python3\"" -c "mcp_servers.contexto.args=[\"$S/mcp_probe_server.py\"]"
