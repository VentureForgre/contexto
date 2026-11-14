#!/usr/bin/env bash
# CI's `package` job, and the same check on a developer's macOS or Linux machine.
#
# Builds the sdist and the wheel and checks what PyPI would get: `twine check --strict`, the name,
# metadata (every classifier is one PyPI knows) and contents of both artifacts, a wheel built again
# from the unpacked sdist, and a render of the long description with readme_renderer, the renderer
# PyPI uses.
#
#   bash .github/scripts/package-check.sh
#
# Needs uv. Everything is written under one new temporary directory, which is removed on exit. Nothing
# is uploaded anywhere.
set -euo pipefail

# Pinned like Ruff and pytest in ci.yml, so a new release cannot fail CI on code that did not change.
TWINE="twine@7.0.0"
README_RENDERER="readme-renderer[md]==46.0"
# PyPI's list of classifiers. An upload with a classifier that is not on it is refused, and by then
# the tag and the GitHub release exist. A classifier newer than this pin needs the pin moved.
TROVE_CLASSIFIERS="trove-classifiers==2026.9.21.13"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
work="$(mktemp -d "${TMPDIR:-/tmp}/contexto-package-check.XXXXXX")"
trap 'rm -rf "$work"' EXIT
work="$(cd "$work" && pwd -P)"
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP VIRTUAL_ENV

echo "== Build the sdist and the wheel"
(cd "$repo" && uv build --quiet --out-dir "$work/dist")
shopt -s nullglob
wheels=("$work"/dist/*.whl)
sdists=("$work"/dist/*.tar.gz)
shopt -u nullglob
if [ "${#wheels[@]}" -ne 1 ] || [ "${#sdists[@]}" -ne 1 ]; then
  echo "package-check: expected one wheel and one sdist, found: $(ls "$work/dist")" >&2
  exit 1
fi
wheel="${wheels[0]}"
sdist="${sdists[0]}"
echo "built $(basename "$wheel") and $(basename "$sdist")"

cd "$work"

echo "== twine check --strict"
uvx --quiet --with "$README_RENDERER" "$TWINE" check --strict "$wheel" "$sdist"

echo "== Build a wheel from the unpacked sdist"
mkdir unpacked rebuilt
tar -xzf "$sdist" -C unpacked
shopt -s nullglob
roots=("$work"/unpacked/*/)
shopt -u nullglob
if [ "${#roots[@]}" -ne 1 ]; then
  echo "package-check: expected one directory in the sdist, found: $(ls unpacked)" >&2
  exit 1
fi
(cd "${roots[0]}" && uv build --quiet --wheel --out-dir "$work/rebuilt")
shopt -s nullglob
rebuilt=("$work"/rebuilt/*.whl)
shopt -u nullglob
if [ "${#rebuilt[@]}" -ne 1 ]; then
  echo "package-check: the unpacked sdist did not build one wheel" >&2
  exit 1
fi

echo "== Metadata, contents and the rendered long description"
uv run --quiet --no-project --with "$README_RENDERER" --with "$TROVE_CLASSIFIERS" \
  python "$repo/.github/scripts/package-check.py" "$wheel" "$sdist" "${rebuilt[0]}" "$repo"

echo "package-check passed"
