#!/bin/bash
# Zero-spend probe (D64): does a provider variable make Claude Code skip ANTHROPIC_BASE_URL, and so
# Contexto's proxy? Two loopback stubs answer 401: A stands for Contexto's upstream, B for the
# provider's endpoint. Each case runs `contexto run --upstream A claude -p` under sandbox-exec with
# every connection that is not to localhost denied, env -i, a temp HOME, CLAUDE_CONFIG_DIR and
# CONTEXTO_HOME, and a fake key, so nothing reaches a model or a provider. macOS only (sandbox-exec).
# Claude Code retries the 401 until a 25-second alarm stops it, so most cases exit 143; that is expected.
# Usage: probe.sh <contexto-binary> <claude-binary>      ONLY="case case" limits the cases run.
# The plain-* cases at the end run `claude -p` without Contexto, in the same sandbox (see run_plain).
set -u
CONTEXTO="$1"; CLAUDE_BIN="$2"
S="$(cd "$(dirname "$0")" && pwd)"
T="$(cd "$(mktemp -d)" && pwd -P)"  # private (0700): a fixed path under /tmp can be replaced by another process
mkdir -p "$T/bin"; ln -s "$CLAUDE_BIN" "$T/bin/claude"
port() { python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])"; }
A=$(port); B=$(port)
python3 "$S/stub.py" "$A" "$T/a.log" > /dev/null 2>&1 & PA=$!
python3 "$S/stub.py" "$B" "$T/b.log" > /dev/null 2>&1 & PB=$!
trap 'kill $PA $PB 2>/dev/null; wait 2>/dev/null' EXIT
sleep 1
cat > "$T/sandbox.sb" <<SB
(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
SB
echo "claude: $("$T/bin/claude" --version 2>&1 | head -1)"
run_case() {  # name, settings-json-or-empty, extra env assignments...
  local name="$1" settings="$2"; shift 2
  if [ -n "${ONLY:-}" ] && ! echo " $ONLY " | grep -q " $name "; then return; fi
  local dir="$T/$name"; mkdir -p "$dir/home" "$dir/config" "$dir/cwd"
  if [ -n "$settings" ]; then mkdir -p "$dir/cwd/.claude"; printf '%s' "$settings" > "$dir/cwd/.claude/settings.json"; fi
  : > "$T/a.log"; : > "$T/b.log"
  ( cd "$dir/cwd" && env -i HOME="$dir/home" PATH="$T/bin:/usr/bin:/bin" TERM=dumb CLAUDE_CONFIG_DIR="$dir/config" \
      CONTEXTO_HOME="$dir/store" ANTHROPIC_API_KEY=sk-ant-fake-not-a-key CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      DISABLE_TELEMETRY=1 DISABLE_AUTOUPDATER=1 "$@" \
      sandbox-exec -f "$T/sandbox.sb" perl -e 'alarm 25; exec @ARGV' \
      "$CONTEXTO" run --upstream "http://127.0.0.1:$A" --session probe claude -p "synthetic probe" \
      < /dev/null > "$dir/out.log" 2> "$dir/err.log" )
  local code=$?
  local a_msgs b_reqs warned logged requests
  a_msgs=$(grep -c "^POST /v1/messages$" "$T/a.log"); b_reqs=$(grep -c "^POST " "$T/b.log")
  warned=$(grep -c "contexto: warning: CLAUDE_CODE_USE_" "$dir/err.log")
  logged=$(grep -c '"event": *"provider_bypass"' "$dir/store/sessions/probe/log.jsonl" 2>/dev/null)
  requests=$(grep -c '"event": *"request"' "$dir/store/sessions/probe/log.jsonl" 2>/dev/null)
  printf '%-28s exit=%-3s upstreamA /v1/messages=%s providerB POSTs=%s (%s) warned=%s logged=%s proxy_requests=%s\n' \
    "$name" "$code" "$a_msgs" "$b_reqs" "$(sort -u "$T/b.log" | sed 's#/model/[^/]*#/model/<id>#; s#/projects/.*#/projects/...#' | head -2 | tr '\n' ';')" \
    "$warned" "${logged:-0}" "${requests:-0}"
}
U="http://127.0.0.1:$B"
run_case control ""
run_case bedrock-env "" CLAUDE_CODE_USE_BEDROCK=1 ANTHROPIC_BEDROCK_BASE_URL="$U" CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 AWS_REGION=us-east-1
run_case bedrock-env-true "" CLAUDE_CODE_USE_BEDROCK=TRUE ANTHROPIC_BEDROCK_BASE_URL="$U" CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 AWS_REGION=us-east-1
run_case bedrock-env-0 "" CLAUDE_CODE_USE_BEDROCK=0 ANTHROPIC_BEDROCK_BASE_URL="$U" CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 AWS_REGION=us-east-1
run_case bedrock-env-empty "" CLAUDE_CODE_USE_BEDROCK= ANTHROPIC_BEDROCK_BASE_URL="$U" CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 AWS_REGION=us-east-1
run_case bedrock-env-other "" CLAUDE_CODE_USE_BEDROCK=enabled ANTHROPIC_BEDROCK_BASE_URL="$U" CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 AWS_REGION=us-east-1
run_case bedrock-settings "{\"env\":{\"CLAUDE_CODE_USE_BEDROCK\":\"1\",\"ANTHROPIC_BEDROCK_BASE_URL\":\"$U\",\"CLAUDE_CODE_SKIP_BEDROCK_AUTH\":\"1\",\"AWS_REGION\":\"us-east-1\"}}"
run_case settings-0-over-env-1 "{\"env\":{\"CLAUDE_CODE_USE_BEDROCK\":\"0\"}}" CLAUDE_CODE_USE_BEDROCK=1 ANTHROPIC_BEDROCK_BASE_URL="$U" CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 AWS_REGION=us-east-1
run_case settings-empty-over-env-1 "{\"env\":{\"CLAUDE_CODE_USE_BEDROCK\":\"\"}}" CLAUDE_CODE_USE_BEDROCK=1 ANTHROPIC_BEDROCK_BASE_URL="$U" CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 AWS_REGION=us-east-1
run_case settings-true-json "{\"env\":{\"CLAUDE_CODE_USE_BEDROCK\":true,\"ANTHROPIC_BEDROCK_BASE_URL\":\"$U\",\"CLAUDE_CODE_SKIP_BEDROCK_AUTH\":\"1\",\"AWS_REGION\":\"us-east-1\"}}"
run_case vertex-env "" CLAUDE_CODE_USE_VERTEX=1 ANTHROPIC_VERTEX_BASE_URL="$U" CLAUDE_CODE_SKIP_VERTEX_AUTH=1 ANTHROPIC_VERTEX_PROJECT_ID=probe CLOUD_ML_REGION=us-east5
run_case vertex-settings "{\"env\":{\"CLAUDE_CODE_USE_VERTEX\":\"1\",\"ANTHROPIC_VERTEX_BASE_URL\":\"$U\",\"CLAUDE_CODE_SKIP_VERTEX_AUTH\":\"1\",\"ANTHROPIC_VERTEX_PROJECT_ID\":\"probe\",\"CLOUD_ML_REGION\":\"us-east5\"}}"
run_case foundry-env "" CLAUDE_CODE_USE_FOUNDRY=1 ANTHROPIC_FOUNDRY_BASE_URL="$U" CLAUDE_CODE_SKIP_FOUNDRY_AUTH=1
run_case foundry-settings "{\"env\":{\"CLAUDE_CODE_USE_FOUNDRY\":\"1\",\"ANTHROPIC_FOUNDRY_BASE_URL\":\"$U\",\"CLAUDE_CODE_SKIP_FOUNDRY_AUTH\":\"1\"}}"
run_case mantle-env "" CLAUDE_CODE_USE_MANTLE=1 ANTHROPIC_BEDROCK_MANTLE_BASE_URL="$U" CLAUDE_CODE_SKIP_MANTLE_AUTH=1 AWS_REGION=us-east-1
run_case mantle-settings "{\"env\":{\"CLAUDE_CODE_USE_MANTLE\":\"1\",\"ANTHROPIC_BEDROCK_MANTLE_BASE_URL\":\"$U\",\"CLAUDE_CODE_SKIP_MANTLE_AUTH\":\"1\",\"AWS_REGION\":\"us-east-1\"}}"
run_case anthropic-aws-env "" CLAUDE_CODE_USE_ANTHROPIC_AWS=1 ANTHROPIC_AWS_BASE_URL="$U" CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH=1 AWS_REGION=us-east-1
run_case anthropic-aws-settings "{\"env\":{\"CLAUDE_CODE_USE_ANTHROPIC_AWS\":\"1\",\"ANTHROPIC_AWS_BASE_URL\":\"$U\",\"CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH\":\"1\",\"AWS_REGION\":\"us-east-1\"}}"
run_case google-cloud-env "" CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD=1 ANTHROPIC_GOOGLE_CLOUD_BASE_URL="$U" CLAUDE_CODE_SKIP_ANTHROPIC_GOOGLE_CLOUD_AUTH=1
run_case google-cloud-settings "{\"env\":{\"CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD\":\"1\",\"ANTHROPIC_GOOGLE_CLOUD_BASE_URL\":\"$U\",\"CLAUDE_CODE_SKIP_ANTHROPIC_GOOGLE_CLOUD_AUTH\":\"1\"}}"
# Cases added after review (PR #258), first measured on 2.1.322.
BOM=$'\xef\xbb\xbf'  # a UTF-8 byte order mark at the start of the settings file
run_case bom-settings "$BOM{\"env\":{\"CLAUDE_CODE_USE_BEDROCK\":\"1\",\"ANTHROPIC_BEDROCK_BASE_URL\":\"$U\",\"CLAUDE_CODE_SKIP_BEDROCK_AUTH\":\"1\",\"AWS_REGION\":\"us-east-1\"}}"
# CLAUDE_CODE_USE_GATEWAY with the two variables it needs. The environment's base URL is stub B, so a
# gateway that used it, and not the proxy's from the launcher's --settings file, would show up at B.
run_case gateway-env "" CLAUDE_CODE_USE_GATEWAY=1 ANTHROPIC_BASE_URL="$U" ANTHROPIC_AUTH_TOKEN=fake-not-a-token
run_case gateway-settings "{\"env\":{\"CLAUDE_CODE_USE_GATEWAY\":\"1\",\"ANTHROPIC_AUTH_TOKEN\":\"fake-not-a-token\"}}" ANTHROPIC_BASE_URL="$U"
# CLAUDE_CODE_IGNORE_PERSISTED_TRUST with the variable in the project file only (D52: -p then drops a
# project file's env but a short list). The provider's own variables are in the environment, so the
# case shows whether the variable itself is dropped.
run_case ignore-trust-settings "{\"env\":{\"CLAUDE_CODE_USE_BEDROCK\":\"1\"}}" CLAUDE_CODE_IGNORE_PERSISTED_TRUST=1 ANTHROPIC_BEDROCK_BASE_URL="$U" CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 AWS_REGION=us-east-1

# Plain `claude -p`, without Contexto: does Claude Code read a settings file that starts with a byte
# order mark for the two settings contexto.upstream reads? The environment's base URL is stub A and
# the file's is stub B; the helper only creates a marker file and prints a fake key.
run_plain() {  # name, settings-json, extra env assignments...
  local name="$1" settings="$2"; shift 2
  if [ -n "${ONLY:-}" ] && ! echo " $ONLY " | grep -q " $name "; then return; fi
  local dir="$T/$name"; mkdir -p "$dir/home" "$dir/config" "$dir/cwd/.claude"
  printf '%s' "${settings//MARKER/$dir/helper-ran}" > "$dir/cwd/.claude/settings.json"
  : > "$T/a.log"; : > "$T/b.log"
  ( cd "$dir/cwd" && env -i HOME="$dir/home" PATH="$T/bin:/usr/bin:/bin" TERM=dumb CLAUDE_CONFIG_DIR="$dir/config" \
      ANTHROPIC_BASE_URL="http://127.0.0.1:$A" CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      DISABLE_TELEMETRY=1 DISABLE_AUTOUPDATER=1 "$@" \
      sandbox-exec -f "$T/sandbox.sb" perl -e 'alarm 25; exec @ARGV' \
      "$T/bin/claude" -p "synthetic probe" < /dev/null > "$dir/out.log" 2> "$dir/err.log" ) 2> /dev/null
  printf '%-28s exit=%-3s envA /v1/messages=%s fileB /v1/messages=%s helper_ran=%s\n' "$name" "$?" \
    "$(grep -c "^POST /v1/messages$" "$T/a.log")" "$(grep -c "^POST /v1/messages$" "$T/b.log")" \
    "$([ -e "$dir/helper-ran" ] && echo 1 || echo 0)"
}
run_plain plain-base-url "{\"env\":{\"ANTHROPIC_BASE_URL\":\"$U\"}}" ANTHROPIC_API_KEY=sk-ant-fake-not-a-key
run_plain plain-bom-base-url "$BOM{\"env\":{\"ANTHROPIC_BASE_URL\":\"$U\"}}" ANTHROPIC_API_KEY=sk-ant-fake-not-a-key
run_plain plain-key-helper "{\"apiKeyHelper\":\": > MARKER; echo sk-ant-fake-not-a-key\"}"
run_plain plain-bom-key-helper "$BOM{\"apiKeyHelper\":\": > MARKER; echo sk-ant-fake-not-a-key\"}"
echo "runs kept under $T"
