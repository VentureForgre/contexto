# Releasing Contexto

Releases are automated with [python-semantic-release](https://python-semantic-release.readthedocs.io/)
(PSR) 10.7.0. Every push to `main`, which happens only by merging a pull request, runs
`.github/workflows/release.yml`. If the commits since the last release contain a releasable change,
the workflow:

1. runs CI (`.github/workflows/ci.yml`) and stops if it fails;
2. computes the next version from the Conventional Commits since the last `v*` tag;
3. stamps that version into the runner's copy of `pyproject.toml` and builds the sdist and wheel;
4. creates the tag `v{version}` on the merged commit and pushes only that tag;
5. creates a GitHub Release whose notes list those commits by type, and attaches the sdist and wheel
   (one upload, made by the workflow after its checks).

Otherwise it exits successfully without doing anything.

## Releases are on (since v0.1.0)

`RELEASES_ENABLED` was set to `true` on 2026-10-16 under D29, and v0.1.0 was released from
f990703. Every push to `main` with a releasable commit (`feat`, `fix`, `perf`, or a breaking
change) now cuts a release; `docs`, `test`, `chore`, `ci`, and similar prefixes do not. The rest of
this section describes the gate itself.

### The gate

The release and recovery jobs run only when the repository variable `RELEASES_ENABLED` is exactly
`true` (Settings > Secrets and variables > Actions > Variables). While it is unset (the state until
2026-10-16), a push to `main` runs only a job that prints "Releases are disabled": no
CI gate, no tag, no release, and no upload. A manual dispatch does nothing either.

- **Who turns it on:** the repository owner, or intake on the owner's recorded instruction.
- **When it was turned on:** after M2 acceptance passed, under D29, which settled O5. The push that
  merged PR #141 released every releasable commit since the start of history as v0.1.0.
- **Turning it off again:** delete the variable or set it to anything other than `true`.

## What a release never does

- **It never pushes a commit to `main`.** `main` is protected by a branch ruleset (pull request
  required, no force push, no deletion, no bypass actors). PSR runs with `--no-commit`, so no version
  bump or changelog commit is made. The workflow then checks that `HEAD` is still the merged commit
  and that the tag points at it. Even if a step tried to push to `main`, the ruleset would reject the
  push, because `GITHUB_TOKEN` is not a bypass actor.
- **It never publishes a package anywhere else.** This repository is private: no PyPI, npm,
  Homebrew, or Scoop. The sdist and wheel exist only as GitHub Release assets.
- **It never edits the version in Git.** No file in the repository carries the release version.
  `pyproject.toml` keeps its static `version`; the release build overwrites it in the runner's
  checkout only (`uv version <v> --frozen`), and `.github/scripts/release-build.sh` fails the release
  unless both artifacts' metadata carry the computed version. The released wheel's version always
  equals its tag; the `version` field on `main` is not the release version.

Tag creation is not restricted by any ruleset, so `GITHUB_TOKEN` with `contents: write` can push the
tag and create the release. A tag pushed with `GITHUB_TOKEN` does not trigger other workflows.

## Conventional Commits

Every commit that reaches `main` must follow [Conventional Commits](https://www.conventionalcommits.org/).
With a merge commit, every commit on the branch reaches `main` and is parsed; the merge commit itself
is skipped. With a squash merge, the pull request title becomes the commit message, so the title must
follow the format too.

```
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```

| Type | Meaning | Release |
|---|---|---|
| `feat` | new feature | minor |
| `fix` | bug fix | patch |
| `perf` | performance improvement | patch |
| `docs` | documentation only | none |
| `chore` | maintenance | none |
| `refactor` | restructuring without behavior change | none |
| `test` | tests only | none |
| `ci` | CI and release automation | none |
| `style` | formatting only | none |
| `build` | build system | none |
| `revert` | revert of an earlier commit | none |

A breaking change (`feat!:`, `fix!:`, or a `BREAKING CHANGE:` footer) bumps the major version. While
the version is below 1.0.0, `major_on_zero = false` turns a breaking change into a minor bump instead.
A commit that does not follow the format never triggers a release.

Versions start at 0.1.0: the first release comes from the first `feat` on `main`. With no tags and
no releasable commit, PSR proposes `v0.0.0`; the workflow treats that as nothing to release.

## Packaging

`main` has no package yet. The workflow handles both states:

- **No `pyproject.toml`:** a releasable commit still gets a tag and a GitHub Release with notes; the
  build step prints that there is nothing to build, and no assets are attached.
- **With `pyproject.toml` (`uv_build` backend and a static version, as in PR #2):** the build hook
  stamps the computed version and runs `uv build`, and both artifacts are attached.

No VCS-derived version plugin (hatch-vcs, setuptools-scm) is needed.

## Configuration

| File | Purpose |
|---|---|
| `.github/semantic-release.toml` | PSR settings: `v{version}` tags, conventional parser, release types, `main` only, no version files |
| `.github/scripts/release-build.sh` | PSR `build_command`: stamp, build, and verify artifact versions; skip cleanly without `pyproject.toml` |
| `.github/scripts/reattach-verify.sh` | Recovery guard: the tag must be an existing `vX.Y.Z` tag on `main` |
| `.github/workflows/ci.yml` | Pull request checks; also the required gate for releases (`workflow_call`) |
| `.github/workflows/release.yml` | Release on push to `main`; manual recovery for an existing tag; both gated on `RELEASES_ENABLED` |

CI runs Ruff only when the repository configures it, runs `uv run --with pytest pytest` only when the
tree has test files, and checks that the package builds when `pyproject.toml` exists. On a tree with
none of those, every job passes without doing anything. The pytest run collects spike tests under
`spikes/` as well. That is intended, because those tests back numbers that research documents cite.

Third-party actions are pinned by commit SHA, with the release tag in a comment:
`actions/checkout` v7.0.1 and `astral-sh/setup-uv` v10.2.0. To update one, resolve the new tag's
commit (`gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`) and update the SHA and the comment
together.

## Dry run

Compute the next version on any checkout without changing anything:

```sh
PSR="uvx --from python-semantic-release==10.7.0 semantic-release -c .github/semantic-release.toml"
$PSR version --print-last-released-tag    # empty when there is no release yet
$PSR version --print-tag                  # the tag the next release would get
$PSR --noop version --no-commit --tag --push --vcs-release --no-changelog   # show every step and the notes
```

To rehearse a full release without touching GitHub, clone into a scratch directory, make a bare
clone to act as `origin`, push the commits under test to its `main`, and run
`$PSR version --no-commit --tag --push --no-vcs-release --no-changelog` there.

## Recovery

- **The tag exists but the release is missing or has no assets** (for example, release creation or
  the upload failed after the tag was pushed): run the Release workflow manually from the Actions tab
  with the existing tag, such as `v0.2.0`. The `reattach` job checks out `main` with full history.
  It refuses anything except an existing `vX.Y.Z` tag whose commit is on `main`
  (`.github/scripts/reattach-verify.sh`, `git merge-base --is-ancestor`), failing before it creates
  anything. If the release is missing, it first checks that the notes can be generated, then creates
  the release and posts that tag's Conventional Commit notes
  (`semantic-release changelog --post-to-release-tag`, which needs a branch checkout, not a detached
  tag). Finally it checks out the tag, rebuilds at the tag's version, and uploads the sdist and wheel.
  It never creates a tag or a new version. A plain re-run of the
  release workflow does nothing here, because it sees the tag and treats that version as released.
- **A release is broken:** fix it with a `fix:` pull request; the merge produces the next patch
  release. Deleting a published tag or release is a maintainer decision made in the GitHub UI.
- **Do not create release tags by hand.** Versions come only from the workflow.
