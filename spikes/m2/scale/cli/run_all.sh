#!/bin/bash
# Claude Code scale spike: every sweep in the README, in order. Zero model spend: the CLI only
# talks to fake_upstream.py on 127.0.0.1 (see harness.py for the isolation and checks).
#
# Usage: spikes/m2/scale/cli/run_all.sh <phase...>   (phases: probe A A2 B C C2 D G G2 F E E2)
# Work dir: $SCALE_WORK (default /tmp/scale-cli/work); CSV: $SCALE_CSV (default
# /tmp/scale-cli/results.csv). Large session files are deleted after each run.
set -eu
S="$(cd "$(dirname "$0")" && pwd)"
WORK="${SCALE_WORK:-/tmp/scale-cli/work}"
CSV="${SCALE_CSV:-/tmp/scale-cli/results.csv}"
mkdir -p "$WORK"
run() { python3 -I "$S/run_scale.py" "$WORK" "$CSV" --timeout 1200 "$@"; }

for phase in "$@"; do
  echo "== phase $phase  $(uptime)"
  case "$phase" in
    probe) python3 -I "$S/probe.py" "$(dirname "$WORK")" ;;
    # A: contexto-like usage (constant 100,000), claude-opus-5-5, resume + one prompt
    A)  run --mode contexto --sizes 500000,2000000,5000000,10000000,30000000,60000000,100000000 --label A ;;
    A2) run --mode contexto --sizes 200000000,300000000 --label A ;;
    # B: honest usage (bytes / 1.83), claude-opus-5-5: where does the CLI compact?
    B)  run --mode honest --sizes 500000,900000,950000,1000000,2000000,5000000,30000000 --label B ;;
    # C/C2: honest usage, claude-haiku-5-5
    C)  run --mode honest --model claude-haiku-5-5 --sizes 100000,150000,180000,200000,500000 --label C ;;
    C2) run --mode honest --model claude-haiku-5-5 --sizes 1000000,2000000 --label C ;;
    # D: contexto-like usage, claude-haiku-5-5
    D)  run --mode contexto --model claude-haiku-5-5 --sizes 500000,2000000,30000000 --label D ;;
    # G/G2: a 200K-window model (claude-haiku-4-5), honest then contexto-like
    G)  run --mode honest --model claude-haiku-4-5 --sizes 150000,170000,180000,190000,200000,500000 --label G ;;
    G2) run --mode contexto --model claude-haiku-4-5 --sizes 2000000,30000000 --label G ;;
    # F: honest usage with the fake upstream refusing prompts over 1,000,000 tokens (real API behaviour)
    F)  run --mode honest --window 1000000 --sizes 2000000 --label F ;;
    # E/E2: contexto-like usage, three Read tool_use round trips after the resume prompt
    E)  run --mode contexto --tool-loop 3 --sizes 30000000,100000000 --label E ;;
    E2) run --mode contexto --tool-loop 3 --model claude-haiku-4-5 --sizes 30000000 --label E ;;
    *) sed -n '2,8p' "$0"; exit 2 ;;
  esac
done
