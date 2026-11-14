#!/usr/bin/env bash
# Build hook for python-semantic-release (build_command). semantic-release runs it after
# computing the next version and before tagging; a failure here aborts the release.
#
# The version is stamped into this checkout's pyproject.toml only. Nothing is committed:
# the release workflow runs semantic-release with --no-commit.
#
# The files written here are the release: the workflow attaches them to the GitHub Release, and
# the pypi job later publishes those same assets (it downloads them; it does not run this script).
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
import gzip, hashlib, io, os, sys, tarfile, zipfile
want, wheel, sdist = sys.argv[1:]

# uv_build puts the checkout's pyproject.toml in the sdist as pyproject.toml.orig, next to its
# normalized copy, and has no setting to leave it out. Repack the sdist without it, keeping every
# other member's bytes and metadata, so the result is as reproducible as uv's own output (uv writes
# mtime 0 for the members and the gzip header).
with tarfile.open(sdist) as t:
    members = [(m, t.extractfile(m).read() if m.isfile() else None) for m in t.getmembers()]
kept = [(m, data) for m, data in members if m.name.count("/") != 1 or not m.name.endswith("/pyproject.toml.orig")]
if len(kept) != len(members):
    tmp = sdist + ".tmp"
    with open(tmp, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as out:
        for m, data in kept:
            out.addfile(m, None if data is None else io.BytesIO(data))
    os.replace(tmp, sdist)
    print("Removed pyproject.toml.orig from the sdist")

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

# The release's checksum asset, in the format `sha256sum -c` and `shasum -a 256 -c` read.
with open(os.path.join(os.path.dirname(wheel), "SHA256SUMS"), "w", encoding="ascii") as sums:
    for path in sorted((sdist, wheel), key=os.path.basename):
        with open(path, "rb") as f:
            sums.write(f"{hashlib.sha256(f.read()).hexdigest()}  {os.path.basename(path)}\n")
PY
echo "Built ${wheels[0]} and ${sdists[0]} at version $NEW_VERSION"
cat dist/SHA256SUMS
