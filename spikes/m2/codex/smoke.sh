#!/bin/bash
# M2 A6 Codex smoke through `contexto run codex` (API-key mode only, D22 and D12).
#   smoke.sh preflight             zero tokens: the proxy forwards to responses_stub.py; dummy key helper
#   smoke.sh turn <n> <session>    one live turn: n=1 starts the conversation, n>1 resumes it
# Live turns need A6_KEY_HELPER (the rig's key helper command; the key itself is never in a file
# here) and A6_UPSTREAM (the owner's proxy base URL). Codex runs with env -i in a non-git scratch
# directory, so no OpenRig or Claude Code session variables reach it. A watchdog reads the session
# log and kills the run on the stop rules (A6 plan, manager-1 2026-10-16 02:47Z). Output stays in
# <repo>/.contexto/spikes/M2-A6/ (ignored by Git).
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../../.." && pwd)"
OUT="$REPO/.contexto/spikes/M2-A6"
SCRATCH=/tmp/m2-a6-scratch
BOUND=42000
FLOOR=6000
PER_REQUEST_KILL=46000   # planned per-request maximum 37,000 plus 25%
CUMULATIVE_KILL=225000
mkdir -p "$OUT" "$SCRATCH"
[ -d "$SCRATCH/.git" ] && { echo "scratch must not be a Git repository"; exit 2; }
# One large synthetic tool output per turn: about 40 KB of plain text lines, no numbers to speak of.
cat > "$SCRATCH/gen.py" <<'PY'
words = ["amber", "basalt", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper"]
for i in range(560):
    print(" ".join(words[(i + j) % len(words)] for j in range(11)))
PY

PROMPT="Run python3 gen.py once and reply with one short line saying how many lines it printed."
# Turn 1 showed the model pipes the tool output away (turn growth about 100 tokens), so turns 2 and 3
# carry the 40 KB synthetic text in the prompt itself: deterministic growth, little output.
PAYLOAD_PROMPT="Below is a synthetic log. Do not run any commands. Reply with one short line: how many lines does it have?
$(python3 "$SCRATCH/gen.py")"
MCP_PROMPT="If a tool named contexto_state is available to you, directly or through tool search, call it once with no arguments. Do not run any other commands. Reply with one short line: yes or no for whether you called it."

mode="${1:-}"
case "$mode" in
  preflight)
    SID="preflight-$(date +%s)"
    HOME_DIR="$OUT/preflight-home"
    PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
    : > "$OUT/preflight-stub.jsonl"
    python3 "$S/responses_stub.py" "$PORT" "$OUT/preflight-stub.jsonl" > /dev/null 2>&1 &
    STUB=$!
    trap 'kill $STUB 2>/dev/null; wait $STUB 2>/dev/null' EXIT
    sleep 1
    printf '#!/bin/sh\necho dummy-not-a-credential\n' > "$OUT/dummy-helper.sh"
    chmod 700 "$OUT/dummy-helper.sh"
    HELPER="$OUT/dummy-helper.sh"
    UP="http://127.0.0.1:$PORT"
    EXTRA=(--codex-upstream "http://127.0.0.1:$PORT/v1")
    CODEX_ARGS=(exec --skip-git-repo-check -c model=gpt-6-astra -c model_reasoning_effort=low "$MCP_PROMPT")
    LIMIT=90
    ;;
  turn)
    n="${2:?turn number}"; SID="${3:?session id}"
    HOME_DIR="$OUT/home"
    HELPER="${A6_KEY_HELPER:?set A6_KEY_HELPER}"
    UP="${A6_UPSTREAM:?set A6_UPSTREAM}"
    EXTRA=()
    P="$PROMPT"; [ "$n" -ge 2 ] && P="$PAYLOAD_PROMPT"; [ "$n" -ge 4 ] && P="$MCP_PROMPT"
    if [ "$n" -eq 1 ]; then
      CODEX_ARGS=(exec --skip-git-repo-check -c model=gpt-6-astra -c model_reasoning_effort=low "$P")
    else
      CODEX_ARGS=(exec --skip-git-repo-check -c model=gpt-6-astra -c model_reasoning_effort=low resume --last "$P")
      # Turn 4 measured that exec mode refuses MCP calls ("requires approval, but approval policy is
      # never"); turn 5 pre-approves the contexto server's tools (values: auto, prompt, writes, approve).
      [ "$n" -ge 5 ] && CODEX_ARGS=(exec --skip-git-repo-check -c model=gpt-6-astra -c model_reasoning_effort=low \
        -c 'mcp_servers.contexto.default_tools_approval_mode="approve"' resume --last "$P")
    fi
    LIMIT=600
    ;;
  *) echo "usage: smoke.sh preflight | smoke.sh turn <n> <session>"; exit 2 ;;
esac

LOG="$HOME_DIR/sessions/$SID/log.jsonl"
(cd "$SCRATCH" && exec env -i HOME="$HOME" PATH="$REPO/.venv/bin:$HOME/.local/bin:/usr/bin:/bin" TERM=dumb \
  CONTEXTO_HOME="$HOME_DIR" perl -e "alarm $LIMIT; exec @ARGV" "$REPO/.venv/bin/contexto" run \
  --bound "$BOUND" --headroom-floor "$FLOOR" --session "$SID" --upstream "$UP" \
  --codex-key-helper "$HELPER" ${EXTRA[@]+"${EXTRA[@]}"} codex "${CODEX_ARGS[@]}" \
  < /dev/null > "$OUT/$mode-$SID-${2:-0}.out" 2>&1) &
RUN=$!

# Watchdog: real usage from the proxy's request entries (all turns of the session so far).
verdict() {
  python3 - "$LOG" "$PER_REQUEST_KILL" "$CUMULATIVE_KILL" <<'PY'
import json, sys
path, per, cum = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    rows = [json.loads(l) for l in open(path) if l.strip()]
except OSError:
    print("ok 0"); sys.exit()
total = 0
for r in rows:
    ev = r.get("event")
    if ev in ("fallback_failed", "transform_error", "upstream_4xx"):
        print(f"stop {ev}"); sys.exit()
    if ev == "over_bound" and r.get("source") == "real":
        print("stop over_bound_real"); sys.exit()
    if ev == "request":
        if r.get("status") == 501 or (isinstance(r.get("status"), int) and 400 <= r["status"] < 500 and "/models" not in str(r.get("path"))):
            print(f"stop status_{r.get('status')}"); sys.exit()
        u = r.get("usage") or {}
        used = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
        if (u.get("input_tokens") or 0) > per:
            print(f"stop per_request {u.get('input_tokens')}"); sys.exit()
        total += used
if total > cum:
    print(f"stop cumulative {total}"); sys.exit()
print(f"ok {total}")
PY
}
while [ "$mode" = turn ] && kill -0 $RUN 2>/dev/null; do  # the stub answers 401 by design
  v=$(verdict)
  case "$v" in
    stop*) echo "WATCHDOG $v: killing the run"; pkill -TERM -P $RUN 2>/dev/null; kill -TERM $RUN 2>/dev/null
           break ;;
  esac
  sleep 0.5
done
wait $RUN; code=$?
echo "$mode $SID turn ${2:-0} exit $code; watchdog $(verdict)"
