#!/usr/bin/env bash
# Checks the inputs of a manual run of the Release workflow, before any other job starts.
#
#   release-request.sh <action> <tag> <ref>
#
#   release    cut the next release. The tag must be empty: the version comes from the Conventional
#              Commits, never from the person who starts the run.
#   reattach   rebuild and re-attach the assets of an existing release tag.
#   publish    publish an existing release tag's assets to PyPI.
#
# Every action is refused from any ref but refs/heads/main. reattach and publish need a tag of the
# form vX.Y.Z. That the tag exists, and is on main, is checked later by reattach-verify.sh, in the
# jobs that have a checkout.
#
# This catches a mistaken run, such as one started from a feature branch. It is not a control: a
# run started from another ref executes that ref's copy of the workflow and of this script. What
# stops such a run from publishing is the `pypi` environment's branch rule on GitHub (main only),
# and the pypi job's attestation check, which accepts only files built by release.yml on main.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: release-request.sh <action> <tag> <ref>" >&2
  exit 2
fi
ACTION="$1"
TAG="$2"
REF="$3"

fail() {
  echo "::error::$1"
  exit 1
}

release_tag() {
  # A second check besides the pattern below: nothing but the characters of a release tag.
  case "$1" in
    *[!v0-9.]* | "") return 1 ;;
  esac
  [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

from_main() {
  # $1: the gh arguments that start this action again from main.
  if [ "$REF" != "refs/heads/main" ]; then
    fail "The Release workflow runs from main only, and this run was started from ${REF:-an unknown ref}. Start it again with the branch main: gh workflow run release.yml --ref main $1"
  fi
}

case "$ACTION" in
  release)
    from_main "-f action=release"
    if [ -n "$TAG" ]; then
      fail "action=release takes no tag: leave the tag input empty. The next version is computed from the Conventional Commits since the last release. To work on an existing tag, use action=reattach or action=publish."
    fi
    echo "Release requested from main."
    ;;
  reattach | publish)
    if ! release_tag "$TAG"; then
      fail "action=$ACTION needs the tag of an existing release, in the form vX.Y.Z (for example v0.5.24). Got: '${TAG//[^[:print:]]/?}'"
    fi
    from_main "-f action=$ACTION -f tag=$TAG"
    echo "$ACTION requested for $TAG."
    ;;
  *)
    fail "Unknown action '${ACTION//[^[:print:]]/?}'. Use release, reattach or publish."
    ;;
esac
