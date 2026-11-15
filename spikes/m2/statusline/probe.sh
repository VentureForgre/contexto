#!/bin/sh
# A status line command that records how Claude Code ran it: probe.sh <record file> <label> [argument]
# The label EMPTY prints nothing; any other prints PROBE-<label>-LINE.
{
  printf '=== %s\n' "$2"
  printf 'arg3=%s\n' "$3"
  printf 'parent=%s\n' "$(ps -o command= -p "$PPID" 2>/dev/null | cut -c1-200)"
  printf 'cwd=%s\n' "$(pwd)"
  printf 'tty=%s\n' "$( [ -t 0 ] && echo stdin-tty || echo stdin-not-tty )"
  env | sort | sed 's/^/env:/'
  printf 'stdin:'
  cat
  printf '\n'
} >> "$1"
[ "$2" = EMPTY ] || printf 'PROBE-%s-LINE\n' "$2"
