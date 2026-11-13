#!/bin/bash
# D70 spike, question 10: which values of CLAUDE_CODE_USE_BEDROCK and of
# CLAUDE_CODE_GATEWAY_HINT_HEADERS does Claude Code treat as on?
# usage: q10_truthy.sh [values|providers|all]   (captures under $D70_OUT, default /tmp/d70-truthy)
# Each run is one `claude -p` through http_probe.py: a scrubbed environment, a temporary config
# directory, the key stub-key, fake AWS values, two stubs on 127.0.0.1 and a sandbox that denies
# every other outbound connection. Zero spend.
#   bedrock-*: ANTHROPIC_BASE_URL names the "base" stub and ANTHROPIC_BEDROCK_BASE_URL the
#              "provider" stub. The variable is on when the provider stub gets the requests.
#   hint-*:    one stub. The variable is on when a request carries x-claude-code-request-class.
#   <provider>-*: the other four switches with the values true and 0, and which wins when the
#              process environment and the config directory's settings.json disagree.
set -u
here=$(cd "$(dirname "$0")" && pwd)
export D70_OUT=${D70_OUT:-/tmp/d70-truthy}
mkdir -p "$D70_OUT"
aws=(--second --env AWS_ACCESS_KEY_ID=fake --env AWS_SECRET_ACCESS_KEY=fake --env AWS_REGION=us-east-1
     --env "ANTHROPIC_BEDROCK_BASE_URL=http://127.0.0.1:{PORT2}/bedrock_prefix")

bedrock() {  # <run> <value>
  python3 "$here/http_probe.py" "bedrock-$1" "${aws[@]}" --env "CLAUDE_CODE_USE_BEDROCK=$2" --timeout 60
}
hint() {  # <run> <value>
  python3 "$here/http_probe.py" "hint-$1" --env "CLAUDE_CODE_GATEWAY_HINT_HEADERS=$2" --timeout 60
}

values() {
  bedrock one 1 & bedrock true true & bedrock zero 0 & bedrock false false &
  wait
  bedrock empty "" & bedrock yes yes & bedrock on on & bedrock upper TRUE &
  wait
  bedrock no no & bedrock two 2 & bedrock padded " 1 " & bedrock enabled enabled &
  wait
  # The switch set only in the config directory's settings.json env block.
  python3 "$here/http_probe.py" bedrock-cfg-true "${aws[@]}" --cfg-env CLAUDE_CODE_USE_BEDROCK=true --timeout 60 &
  python3 "$here/http_probe.py" bedrock-cfg-zero "${aws[@]}" --cfg-env CLAUDE_CODE_USE_BEDROCK=0 --timeout 60 &
  hint true true & hint yes yes &
  wait
  hint on on & hint two 2 & hint false false & hint upper TRUE &
  wait
}

provider() {  # <run> <switch>=<value> <base URL variable> [more K=V ...]
  local run=$1 switch=$2 base=$3; shift 3
  local extra=()
  for pair in "$@"; do extra+=(--env "$pair"); done
  python3 "$here/http_probe.py" "$run" --second --env "$switch" --env "$base=http://127.0.0.1:{PORT2}/provider_prefix" \
    ${extra[@]+"${extra[@]}"} --timeout 60
}
awsenv=(AWS_ACCESS_KEY_ID=fake AWS_SECRET_ACCESS_KEY=fake AWS_REGION=us-east-1)

providers() {
  for value in true 0; do
    provider "vertex-$value" "CLAUDE_CODE_USE_VERTEX=$value" ANTHROPIC_VERTEX_BASE_URL CLAUDE_CODE_SKIP_VERTEX_AUTH=1 \
      ANTHROPIC_VERTEX_PROJECT_ID=fake-project CLOUD_ML_REGION=us-east5 &
    provider "foundry-$value" "CLAUDE_CODE_USE_FOUNDRY=$value" ANTHROPIC_FOUNDRY_BASE_URL ANTHROPIC_FOUNDRY_API_KEY=fake &
    provider "mantle-$value" "CLAUDE_CODE_USE_MANTLE=$value" ANTHROPIC_BEDROCK_MANTLE_BASE_URL "${awsenv[@]}" &
    provider "anthropic-aws-$value" "CLAUDE_CODE_USE_ANTHROPIC_AWS=$value" ANTHROPIC_AWS_BASE_URL "${awsenv[@]}" \
      ANTHROPIC_AWS_WORKSPACE_ID=wrkspc_fake &
    wait
  done
  # Process environment against the config directory's settings.json env block.
  python3 "$here/http_probe.py" bedrock-env1-cfg0 "${aws[@]}" --env CLAUDE_CODE_USE_BEDROCK=1 \
    --cfg-env CLAUDE_CODE_USE_BEDROCK=0 --timeout 60 &
  python3 "$here/http_probe.py" bedrock-env0-cfg1 "${aws[@]}" --env CLAUDE_CODE_USE_BEDROCK=0 \
    --cfg-env CLAUDE_CODE_USE_BEDROCK=1 --timeout 60 &
  python3 "$here/http_probe.py" hint-env1-cfg0 --env CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 \
    --cfg-env CLAUDE_CODE_GATEWAY_HINT_HEADERS=0 --timeout 60 &
  python3 "$here/http_probe.py" hint-env0-cfg1 --env CLAUDE_CODE_GATEWAY_HINT_HEADERS=0 \
    --cfg-env CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 --timeout 60 &
  wait
  # The launcher's own --settings file says 1 while the user's environment or settings say 0.
  python3 "$here/http_probe.py" hint-flag1-env0 --env CLAUDE_CODE_GATEWAY_HINT_HEADERS=0 \
    --flag-env CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 --timeout 60 &
  python3 "$here/http_probe.py" hint-flag1-cfg0 --cfg-env CLAUDE_CODE_GATEWAY_HINT_HEADERS=0 \
    --flag-env CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 --timeout 60 &
  wait
  # A settings env value that is not a string. The base URL is in the same file, so a file Claude
  # Code rejects as a whole shows as no request at either stub.
  for value in true 1; do
    python3 "$here/http_probe.py" "bedrock-cfg-json-$value" "${aws[@]}" --base-via config-settings \
      --cfg-env-json "CLAUDE_CODE_USE_BEDROCK=$value" --timeout 60 &
  done
  wait
}

case ${1:-all} in
  values) values ;;
  providers) providers ;;
  all) values; providers ;;
  *) echo "usage: q10_truthy.sh [values|providers|all]" >&2; exit 2 ;;
esac

for run in "$D70_OUT"/*/; do
  run=${run%/}
  [ -f "$run/index.tsv" ] || continue
  python3 - "$run" <<'PY'
import csv, sys
from pathlib import Path
run = Path(sys.argv[1])
rows = list(csv.DictReader((run / "index.tsv").open(), delimiter="\t"))
stubs = {s: sum(r["stub"] == s for r in rows) for s in ("base", "provider")}
classes = sorted({r["x-claude-code-request-class"] for r in rows if r["method"] == "POST"})
print(f"{run.name}\tbase={stubs['base']}\tprovider={stubs['provider']}\trequest-class={','.join(classes)}")
PY
done
