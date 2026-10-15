#!/usr/bin/env bash
# Checks for the Release workflow's reattach job, run on a full-history checkout of main.
# Fails closed, before anything is created, unless TAG is a release tag that exists and
# whose commit is on main.
set -euo pipefail

TAG="${1:?usage: reattach-verify.sh <tag>}"

if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Not a release tag: $TAG" >&2
  exit 1
fi

git fetch --quiet --tags origin
git fetch --quiet origin main
if ! commit=$(git rev-parse -q --verify "refs/tags/$TAG^{commit}"); then
  echo "Tag $TAG does not exist" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$commit" origin/main; then
  echo "Tag $TAG points at $commit, which is not on main; refusing to release it" >&2
  exit 1
fi
echo "$TAG -> $commit is on main"
