#!/usr/bin/env bash
# Checks for the Release workflow's reattach and pypi jobs, run on a full-history checkout.
# Fails closed, before anything is created, unless TAG is a release tag that exists and whose
# commit is on main's first-parent chain.
#
# The first-parent chain is the list of commits main itself has pointed at: with squash merges and
# a ruleset that forbids force pushes, each merge adds one commit to it. A commit that reached main
# inside a merge commit is an ancestor of main but not on that chain, and main's tree never held
# it. A reattach builds the tagged commit's own tree with its own build script, so a tag there could
# get bytes that main never held attested and published. A rebase merge puts each of a branch's
# commits on the chain, so this check does not cover it; the project squashes.
#
# main is read from refs/remotes/origin/main, which the fetch below updates in either job's
# checkout. Never the short name origin/main: git resolves a tag named "origin/main" before it.
set -euo pipefail

TAG="${1:?usage: reattach-verify.sh <tag>}"
MAIN="refs/remotes/origin/main"

if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Not a release tag: $TAG" >&2
  exit 1
fi

git fetch --quiet --tags origin
git fetch --quiet origin "+refs/heads/main:$MAIN"
if ! commit=$(git rev-parse -q --verify "refs/tags/$TAG^{commit}"); then
  echo "Tag $TAG does not exist" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$commit" "$MAIN"; then
  echo "Tag $TAG points at $commit, which is not on main; refusing to release it" >&2
  exit 1
fi
# grep -c prints 0 and exits 1 when nothing matches; `|| true` keeps that count. If rev-list fails,
# the count is not 1 either, so the check fails closed.
on_chain=$(git rev-list --first-parent "$MAIN" | grep -cx -- "$commit" || true)
if [ "$on_chain" != 1 ]; then
  echo "Tag $TAG points at $commit, which reached main inside a merge commit and was never main's" \
    "head (it is not on main's first-parent chain); refusing to release it" >&2
  exit 1
fi
echo "$TAG -> $commit is on main's first-parent chain"
