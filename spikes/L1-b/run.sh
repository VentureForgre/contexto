#!/bin/bash
# M0 L1-b zero-spend experiments for docs/research/M0-interception.md.
#
# Every experiment runs a throwaway Claude Code or Codex process against a local stub
# upstream on 127.0.0.1 that answers 401 and logs only method, path, header names, body
# size, message structure, and synthetic MARKER-* tokens. The CLI builds and records its
# full request before the 401, so request contents can be checked without any model call.
# The throwaway processes get an isolated config dir (CLAUDE_CONFIG_DIR / CODEX_HOME),
# a dummy key that is not a credential, a scrubbed environment, and a cwd outside the repo.
# All output goes under <repo>/.contexto/spikes/L1-b/ (gitignored).
#
# Usage: spikes/L1-b/run.sh <experiment>
#   cc-hooks        Claude: SessionStart/UserPromptSubmit additionalContext reaches the request
#   cc-resume       Claude: a synthesized session file is accepted by --resume
#   cc-threshold    Claude: which planted usage values make resume compact before sending
#   cc-precompact   Claude: PreCompact stdout steers the summarizer; exit 2 blocks
#   cx-hooks        Codex: hook context reaches the request; transport and request fields
#   cx-resume       Codex: appended rollout items are accepted; planted token_count compacts
#   all             all of the above
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../.." && pwd)"
OUT="$REPO/.contexto/spikes/L1-b"
PORT="${M0_STUB_PORT:-18431}"
STUB_LOG="$OUT/stub-requests.log"
HOOK_LOG="$OUT/hooks.log"
SCRATCH="/private/tmp/m0-l1b-scratch"
CLAUDE_BIN="$HOME/.local/bin/claude"
CODEX_BIN="$HOME/.local/bin/codex"

setup() {
  mkdir -p "$OUT/cc-config" "$OUT/codex-home" "$SCRATCH/cc" "$SCRATCH/cx"
  python3 - "$S" "$HOOK_LOG" "$OUT" <<'EOF'
import json, sys
s, log, out = sys.argv[1:4]
events = ["SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop", "SessionEnd"]
cc = [{"hooks": [{"type": "command", "command": f"{s}/cc_hook.py {log}"}]}]
json.dump({"hooks": {e: cc for e in events}}, open(f"{out}/cc-settings.json", "w"), indent=1)
cx = [{"hooks": [{"type": "command", "command": f"{s}/codex_hook.py {log}"}]}]
json.dump({"hooks": {e: cx for e in events}}, open(f"{out}/codex-home/hooks.json", "w"), indent=1)
EOF
  if ! curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/health"; then
    python3 "$S/stub401.py" "$PORT" "$STUB_LOG" > "$OUT/stub.err" 2>&1 &
    sleep 1
  fi
}

reset_logs() { : > "$STUB_LOG"; : > "$HOOK_LOG"; }

# claude_stub <cwd> <args...>: throwaway Claude Code against the stub. EXTRA_ENV adds env pairs.
claude_stub() {
  local cwd="$1"; shift
  (cd "$cwd" && env -i HOME="$HOME" PATH="$PATH" TERM=dumb \
    CLAUDE_CONFIG_DIR="$OUT/cc-config" ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" \
    ANTHROPIC_API_KEY="dummy-not-a-credential" CLAUDE_CODE_MAX_RETRIES=0 ${EXTRA_ENV:-} \
    perl -e 'alarm 60; exec @ARGV' "$CLAUDE_BIN" -p "$@" < /dev/null)
}

# codex_stub <args...>: throwaway Codex against the stub (base URL via -c, temporary CODEX_HOME).
codex_stub() {
  (cd "$SCRATCH/cx" && env -i HOME="$HOME" PATH="$PATH" TERM=dumb CODEX_HOME="$OUT/codex-home" \
    CODEX_API_KEY="dummy-not-a-credential" OPENAI_API_KEY="dummy-not-a-credential" \
    perl -e 'alarm 60; exec @ARGV' "$CODEX_BIN" exec --skip-git-repo-check \
    --dangerously-bypass-hook-trust -c "openai_base_url=http://127.0.0.1:$PORT/v1" "$@" < /dev/null)
}

summary() { python3 "$S/summarize.py" "$STUB_LOG" "$HOOK_LOG"; }

cc_hooks() {
  echo "== cc-hooks: fresh session with probe hooks"
  reset_logs
  claude_stub "$SCRATCH/cc" --settings "$OUT/cc-settings.json" "Code word? MARKER-CC-NEXT" 2>&1 | head -2
  summary
}

cc_resume() {
  echo "== cc-resume: synthesized session file, then --resume"
  reset_logs
  local sid; sid=$(python3 "$S/synth_session.py" "$OUT/cc-config/projects" "$SCRATCH/cc" 1200)
  claude_stub "$SCRATCH/cc" --resume "$sid" "Code word? MARKER-H3-NEXT" 2>&1 | head -2
  summary
}

# first_request <planted tokens> [flags...]: prints whether the first POST was the new turn or a compaction call.
first_request() {
  local tokens="$1"; shift
  reset_logs
  local sid; sid=$(python3 "$S/synth_session.py" "$OUT/cc-config/projects" "$SCRATCH/cc" "$tokens")
  claude_stub "$SCRATCH/cc" --resume "$sid" "$@" "Code word? MARKER-H3-NEXT" > /dev/null 2>&1
  python3 - "$STUB_LOG" <<'EOF'
import json, sys
posts = [json.loads(l) for l in open(sys.argv[1]) if '"POST"' in l]
print("no-request" if not posts else ("normal-turn" if "MARKER-H3-NEXT" in posts[0]["markers"] else "compaction-first"))
EOF
}

cc_threshold() {
  echo "== cc-threshold: planted usage on resume (claude-opus-5-5, 1M window)"
  printf '%-8s %-36s %-24s %s\n' tokens env flags first_request
  local t
  for t in 150000 500000 960000 970000 975000 980000 999000; do
    printf '%-8s %-36s %-24s %s\n' "$t" default none "$(EXTRA_ENV= first_request "$t")"
  done
  printf '%-8s %-36s %-24s %s\n' 101000 default "--autocompact 100000" "$(EXTRA_ENV= first_request 101000 --autocompact 100000)"
  printf '%-8s %-36s %-24s %s\n' 199000 default "--autocompact 200000" "$(EXTRA_ENV= first_request 199000 --autocompact 200000)"
  printf '%-8s %-36s %-24s %s\n' 120000 CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=10 none "$(EXTRA_ENV=CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=10 first_request 120000)"
  printf '%-8s %-36s %-24s %s\n' 999000 DISABLE_AUTO_COMPACT=1 none "$(EXTRA_ENV=DISABLE_AUTO_COMPACT=1 first_request 999000)"
}

cc_precompact() {
  local mode sid
  for mode in steer block; do
    echo "== cc-precompact ($mode): resume with planted 999000 tokens and probe hooks"
    reset_logs
    sid=$(python3 "$S/synth_session.py" "$OUT/cc-config/projects" "$SCRATCH/cc" 999000)
    if [ "$mode" = block ]; then EXTRA_ENV=PRECOMPACT_BLOCK=1; else EXTRA_ENV=; fi
    EXTRA_ENV=$EXTRA_ENV claude_stub "$SCRATCH/cc" --resume "$sid" --settings "$OUT/cc-settings.json" \
      "Code word? MARKER-H3-NEXT" 2>&1 | head -2
    summary
  done
}

cx_hooks() {
  echo "== cx-hooks: fresh Codex session with probe hooks"
  rm -rf "$OUT/codex-home/sessions"
  reset_logs
  codex_stub "Code word? MARKER-CX-NEXT" 2>&1 | grep -m1 "unexpected status"
  summary
}

cx_resume() {
  local f tid v name marker tokens
  f=$(find "$OUT/codex-home/sessions" -name 'rollout-*.jsonl' | head -1)
  if [ -z "$f" ]; then echo "cx-resume needs a rollout: run cx-hooks first"; return 1; fi
  tid=$(basename "$f" .jsonl | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
  for v in A:MARKER-CX-ASSIST-A:1000 B:MARKER-CX-ASSIST-B:260000; do
    IFS=: read -r name marker tokens <<< "$v"
    echo "== cx-resume ($name): append assistant item + token_count total=$tokens, then exec resume"
    python3 "$S/codex_append.py" "$f" "$marker" "$tokens"
    reset_logs
    codex_stub resume "$tid" "Code word? MARKER-CX-NEXT-$name" 2>&1 | grep -m1 "unexpected status"
    summary
  done
}

setup
case "${1:-}" in
  cc-hooks) cc_hooks ;;
  cc-resume) cc_resume ;;
  cc-threshold) cc_threshold ;;
  cc-precompact) cc_precompact ;;
  cx-hooks) cx_hooks ;;
  cx-resume) cx_resume ;;
  all) cc_hooks; cc_resume; cc_threshold; cc_precompact; cx_hooks; cx_resume ;;
  *) sed -n '2,22p' "$0"; exit 2 ;;
esac
