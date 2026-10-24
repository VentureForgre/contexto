#!/bin/bash
# Real tool outputs from this repository for scale_dumps.py (D41; reviewer A on PR #216):
# grep, grep -C3, and pytest after changing each key's value and after making it a string.
#   scale_dumps_gen.sh REPO OUT PYTHON
# REPO is a git checkout of this repository (it is edited and restored with git checkout per key),
# OUT the directory for the dumps, PYTHON an interpreter with pytest installed. Takes a few minutes.
set -u
if [ $# -ne 3 ]; then echo "usage: $0 REPO OUT PYTHON" >&2; exit 2; fi
REPO=$1
OUT=$2
PY=$3
mkdir -p "$OUT"
# key|file|old literal|new literal|test files
while IFS='|' read -r key file old new tests; do
  [ -z "$key" ] && continue
  (cd "$REPO" && grep -rn "$key" src tests docs spikes > "$OUT/$key.grep" 2>/dev/null)
  (cd "$REPO" && grep -rn -C3 "$key" src tests docs > "$OUT/$key.grepc" 2>/dev/null)
  for mode in value type; do
    git -C "$REPO" checkout -q -- "$file"
    if [ "$mode" = value ]; then rep="$new"; else rep="\"$old\""; fi
    "$PY" - "$REPO/$file" "$key" "$old" "$rep" <<'EOF'
import re, sys
path, key, old, rep = sys.argv[1:]
src = open(path).read()
pat = re.compile(r"^(" + re.escape(key) + r"\s*=\s*)" + re.escape(old), re.M)
new, n = pat.subn(lambda m: m.group(1) + rep, src, count=1)
assert n == 1, (key, path)
open(path, "w").write(new)
EOF
    # shellcheck disable=SC2086 # $tests is a list of paths
    (cd "$REPO" && PYTHONPATH="$REPO/src" timeout 300 "$PY" -m pytest -vv --tb=long -p no:cacheprovider $tests \
       > "$OUT/$key.pytest_$mode" 2>&1)
    git -C "$REPO" checkout -q -- "$file"
  done
  echo "$key grep=$(grep -oi "$key" "$OUT/$key.grep" | wc -l) value=$(grep -oi "$key" "$OUT/$key.pytest_value" | wc -l)" \
       "type=$(grep -oi "$key" "$OUT/$key.pytest_type" | wc -l)"
done <<'KEYS'
RECENCY_WEIGHT|src/contexto/store/search.py|0.5|0.8|tests/test_search_rank.py tests/test_search_lines.py tests/test_mcp.py
SHOWN_IN_DETAIL|src/contexto/store/search.py|3|4|tests/test_search_rank.py tests/test_search_lines.py tests/test_mcp.py
SEARCH_MORE_LIMIT|src/contexto/mcp.py|50|40|tests/test_search_lines.py tests/test_mcp.py tests/test_search_rank.py
MATCHES_PER_PAGE|src/contexto/store/render.py|3|4|tests/test_search_lines.py tests/test_mcp.py tests/test_search_rank.py
MATCH_CONTEXT|src/contexto/store/render.py|1|2|tests/test_search_lines.py tests/test_mcp.py tests/test_search_rank.py
MATCH_LINE_CHARS|src/contexto/store/render.py|200|160|tests/test_search_lines.py tests/test_mcp.py tests/test_search_rank.py
RECALL_LIMIT|src/contexto/store/render.py|32_000|24_000|tests/test_recall_window.py tests/test_recall_half.py tests/test_mcp.py
IN_VIEW_LIMIT|src/contexto/mcp.py|3|5|tests/test_in_view_search.py tests/test_mcp.py
STUB_TEXT_CHARS|src/contexto/store/pages.py|200|300|tests/test_search_rank.py tests/test_mcp.py tests/test_search_lines.py
UPSTREAM_TIMEOUT_S|src/contexto/proxy.py|900|600|tests/test_proxy_server.py
KEYS
