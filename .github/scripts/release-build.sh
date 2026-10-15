#!/usr/bin/env bash
# Build hook for python-semantic-release (build_command). semantic-release runs it after
# computing the next version and before tagging; a failure here aborts the release.
#
# The version is stamped into this checkout's pyproject.toml only. Nothing is committed:
# the release workflow runs semantic-release with --no-commit.
set -euo pipefail

: "${NEW_VERSION:?NEW_VERSION must be set by semantic-release}"

if [ ! -f pyproject.toml ]; then
  echo "No pyproject.toml on this commit; nothing to build. The release gets notes only."
  exit 0
fi

rm -rf dist
uv version "$NEW_VERSION" --frozen
uv build --out-dir dist

# Fail the release if a built artifact does not carry the computed version.
shopt -s nullglob
wheels=(dist/*.whl)
sdists=(dist/*.tar.gz)
if [ "${#wheels[@]}" -ne 1 ] || [ "${#sdists[@]}" -ne 1 ]; then
  echo "Expected one wheel and one sdist in dist/, found: $(ls dist)" >&2
  exit 1
fi
python3 - "$NEW_VERSION" "${wheels[0]}" "${sdists[0]}" <<'PY'
import sys, tarfile, zipfile
want, wheel, sdist = sys.argv[1:]
def version(text):
    return next((l.split(": ", 1)[1] for l in text.splitlines() if l.startswith("Version: ")), None)
with zipfile.ZipFile(wheel) as z:
    got_wheel = version(z.read(next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))).decode())
with tarfile.open(sdist) as t:
    member = next(m for m in t.getmembers() if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO"))
    got_sdist = version(t.extractfile(member).read().decode())
for name, got in (("wheel", got_wheel), ("sdist", got_sdist)):
    print(f"{name} METADATA Version: {got}")
    if got != want:
        sys.exit(f"{name} carries version {got!r}, expected {want!r}")
PY
echo "Built ${wheels[0]} and ${sdists[0]} at version $NEW_VERSION"
