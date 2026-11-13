#!/usr/bin/env bash
# CI's `install` job, and the same check on a developer's macOS or Linux machine.
#
# Builds the sdist and the wheel, installs the built wheel into an empty virtual environment, and runs
# the installed command from a directory outside the checkout, so what runs is the installed package
# and not the source tree. Then installs the sdist into a second empty environment and compares the
# two, so a file missing from the sdist is caught.
#
#   bash .github/scripts/install-check.sh [PYTHON]
#
# PYTHON is a version or an interpreter path as `uv venv --python` takes it (for example 3.12). Without
# it uv chooses: $UV_PYTHON when set, as in CI, else its default. EXPECT_VERSION (default
# 0.0.0+checkout, what a checkout builds) is the version the artifacts must carry.
#
# Needs uv. Everything is written under one new temporary directory, which is removed on exit.
set -euo pipefail

if [ "$#" -gt 1 ]; then
  echo "usage: install-check.sh [PYTHON]" >&2
  exit 2
fi
if [ -n "${1:-}" ]; then
  export UV_PYTHON="$1"
fi
expect="contexto ${EXPECT_VERSION:-0.0.0+checkout}"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
work="$(mktemp -d "${TMPDIR:-/tmp}/contexto-install-check.XXXXXX")"
trap 'rm -rf "$work"' EXIT
work="$(cd "$work" && pwd -P)"
case "$work/" in
  "$repo"/*)
    echo "The temporary directory $work is inside the checkout; set TMPDIR to a directory outside it." >&2
    exit 1
    ;;
esac

# Nothing from the caller's environment may put the checkout, or another Contexto, in front of the
# installed one.
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP VIRTUAL_ENV CONTEXTO_HOME
export COLUMNS=200  # argparse wraps --version and --help to the terminal width

fail() {
  echo "install-check: $*" >&2
  exit 1
}

# expect_output WANT COMMAND...: the command exits 0 and prints exactly WANT.
expect_output() {
  local want="$1" got
  shift
  got="$("$@")" || fail "'$*' exited $?"
  [ "$got" = "$want" ] || fail "'$*' printed '$got', expected '$want'"
  echo "ok: $* -> $got"
}

# new_env NAME ARTIFACT: an empty virtual environment under $work with only ARTIFACT installed.
# --no-index: Contexto has no runtime dependencies, so nothing may be fetched for it. A build of the
# sdist still gets its build backend (uv has uv_build built in for the range pyproject.toml allows).
new_env() {
  local env="$work/$1" artifact="$2" installed
  uv venv --quiet --no-project "$env"
  if [ "${artifact%.whl}" != "$artifact" ]; then
    uv pip install --quiet --no-cache --no-index --python "$env/bin/python" "$artifact"
  else
    uv pip install --quiet --no-cache --python "$env/bin/python" "$artifact"
  fi
  installed="$(uv pip freeze --quiet --python "$env/bin/python")"
  case "$installed" in
    "contexto-proxy @ file://"*) ;;
    *) fail "expected only contexto-proxy in $env, found: $installed" ;;
  esac
  [ "$(printf '%s\n' "$installed" | wc -l | tr -d ' ')" = 1 ] || fail "$env holds more than contexto-proxy: $installed"
}

site_packages() {
  "$work/$1/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'
}

echo "== Build the sdist and the wheel"
(cd "$repo" && uv build --quiet --out-dir "$work/dist")
shopt -s nullglob
wheels=("$work"/dist/*.whl)
sdists=("$work"/dist/*.tar.gz)
shopt -u nullglob
if [ "${#wheels[@]}" -ne 1 ] || [ "${#sdists[@]}" -ne 1 ]; then
  fail "expected one wheel and one sdist, found: $(ls "$work/dist")"
fi
wheel="${wheels[0]}"
sdist="${sdists[0]}"
echo "built $(basename "$wheel") and $(basename "$sdist")"

mkdir "$work/run" "$work/home"
cd "$work/run"

echo "== Install the wheel into an empty environment"
new_env wheel-env "$wheel"
py="$work/wheel-env/bin/python"
"$py" --version
"$py" - "$work/wheel-env" <<'PY'
import os
import sys

import contexto

env = os.path.realpath(sys.argv[1])
where = os.path.realpath(contexto.__file__)
if not where.startswith(env + os.sep):
    sys.exit(f"install-check: contexto was imported from {where}, not from {env}")
print(f"ok: contexto is imported from {where}")
PY

expect_output "$expect" "$work/wheel-env/bin/contexto" --version
expect_output "$expect" "$py" -m contexto --version
help="$("$work/wheel-env/bin/contexto" --help)" || fail "'contexto --help' exited $?"
for word in "usage: contexto" proxy run sessions; do
  case "$help" in
    *"$word"*) ;;
    *) fail "'contexto --help' does not mention '$word'" ;;
  esac
done
echo "ok: contexto --help"

echo "== Read the store, offline"
# A real command against an empty data directory: it lists no sessions and exits 0.
CONTEXTO_HOME="$work/home" expect_output "[]" "$work/wheel-env/bin/contexto" sessions list --json
# The same command with an audit hook that refuses name lookups, connections and child processes.
CONTEXTO_HOME="$work/home" "$py" - <<'PY'
import sys

REFUSED = {
    "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto",
    "socket.sendmsg", "subprocess.Popen", "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.system",
}
seen = []


def hook(event, args):
    if event in REFUSED:
        seen.append(event)
        raise RuntimeError(f"{event} during an offline command")


sys.addaudithook(hook)

from contexto.cli import main  # noqa: E402

code = main(["sessions", "list"])
if seen or code:
    sys.exit(f"install-check: 'contexto sessions list' exited {code} and tried {seen or 'nothing refused'}")
print("ok: contexto sessions list made no network call and started no process")
PY

echo "== Install the sdist into a second empty environment"
new_env sdist-env "$sdist"
expect_output "$expect" "$work/sdist-env/bin/contexto" --version
# The sdist must build the same package the wheel holds: same files, same metadata.
wheel_site="$(site_packages wheel-env)"
sdist_site="$(site_packages sdist-env)"
diff -r -x __pycache__ "$wheel_site/contexto" "$sdist_site/contexto" \
  || fail "the package installed from the sdist differs from the wheel's"
for name in METADATA entry_points.txt; do
  diff "$wheel_site"/contexto_proxy-*.dist-info/"$name" "$sdist_site"/contexto_proxy-*.dist-info/"$name" \
    || fail "$name installed from the sdist differs from the wheel's"
done
echo "ok: the sdist installs the same package and metadata as the wheel"

echo "install-check passed on $("$py" --version)"
