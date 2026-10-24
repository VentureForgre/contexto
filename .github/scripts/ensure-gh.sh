#!/usr/bin/env bash
# Make the GitHub CLI available to the release jobs. The GitHub-hosted runners they run on ship gh,
# so this normally only reports its version. The install below is a fallback for a runner without
# gh, since a release without gh publishes no sdist or wheel (v0.2.2).
#
# If gh is already on PATH it is used as is. Otherwise a pinned gh release is downloaded into
# RUNNER_TEMP, its archive checked against the SHA-256 published in that release's checksums
# file, and its bin directory added to GITHUB_PATH for the later steps of the job.
set -euo pipefail

GH_VERSION="2.102.0"
GH_SHA256_LINUX_AMD64="bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386"

if command -v gh >/dev/null 2>&1; then
  echo "gh found: $(gh --version | head -1)"
  exit 0
fi

: "${RUNNER_TEMP:?RUNNER_TEMP must be set (run this inside a GitHub Actions job)}"
: "${GITHUB_PATH:?GITHUB_PATH must be set (run this inside a GitHub Actions job)}"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) arch="amd64"; expected="$GH_SHA256_LINUX_AMD64" ;;
  *) echo "No pinned gh checksum for $(uname -s)-$(uname -m); install gh on this runner." >&2; exit 1 ;;
esac

name="gh_${GH_VERSION}_linux_${arch}"
archive="$RUNNER_TEMP/$name.tar.gz"
url="https://github.com/cli/cli/releases/download/v${GH_VERSION}/${name}.tar.gz"

curl --fail --silent --show-error --location --retry 3 --output "$archive" "$url"
echo "$expected  $archive" | sha256sum --check --strict -
tar -xzf "$archive" -C "$RUNNER_TEMP"
rm -f "$archive"

bin="$RUNNER_TEMP/$name/bin"
"$bin/gh" --version | head -1
echo "$bin" >> "$GITHUB_PATH"
