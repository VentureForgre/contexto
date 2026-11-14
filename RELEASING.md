# Releasing Contexto

Releases are cut by hand. Merging a pull request never releases by itself:
`.github/workflows/release.yml` has no push trigger and runs only when someone starts it
(`workflow_dispatch`). A run started on `main` with `action=release`:

1. checks the request and runs CI (`.github/workflows/ci.yml`), and stops if either fails;
2. computes the next version from the Conventional Commits since the last `v*` tag, with
   [python-semantic-release](https://python-semantic-release.readthedocs.io/) (PSR) 10.7.0;
3. stops unless `CHANGELOG.md` has a section for that version;
4. stamps that version into the runner's copy of `pyproject.toml`, builds the sdist and wheel, and
   writes `SHA256SUMS` for them;
5. creates the tag `v{version}` on the commit the run was started on and pushes only that tag;
6. creates a GitHub build provenance attestation for the sdist and wheel
   (`actions/attest-build-provenance`);
7. creates a GitHub Release whose notes list those commits by type, and attaches the sdist, the
   wheel, and `SHA256SUMS` (one upload, made by the workflow after its checks);
8. if PyPI publishing is on, publishes the same sdist and wheel to PyPI as `contexto-proxy`
   (see [PyPI](#pypi)).

If no commit since the last release is releasable, it exits successfully without doing anything.

## When to release

This is the project's rule since D67:

- Releases are cut by hand and batched. The maintainer, or an agent on the maintainer's
  instruction, cuts one when `main` holds user-visible changes worth shipping, normally no more
  than one a week.
- A security fix, or a fix for a release that is broken for users, ships the same day.
- Merging a pull request never releases by itself.
- The pull request title's type still decides the size of the next version bump. The highest bump
  since the last tag wins: one `feat:` among ten `fix:` merges makes the next release a minor one.

Until v0.5.24 every `feat`, `fix` or `perf` merge cut a release: v0.5.0 to v0.5.24 are 25 releases
in 14 days. That does not carry over to PyPI. A version on PyPI can never be replaced or uploaded
again, even after it is deleted, so every release is permanent.

## Cutting a release

1. **See what the next version is.** On an up-to-date `main`:

   ```sh
   git switch main && git pull --ff-only
   psr() { uvx --from python-semantic-release==10.7.0 semantic-release -c .github/semantic-release.toml "$@"; }
   psr version --print-last-released-tag    # for example v0.5.24
   psr version --print-tag                  # the tag the next release would get, for example v0.6.0
   ```

   If both print the same tag, nothing since the last release is releasable.

2. **Write the changelog section.** Open a `docs:` pull request that adds a section
   `## X.Y.Z (YYYY-MM-DD)` to `CHANGELOG.md`, above the older versions. Write it in user terms from
   the merges since the last tag (`git log --first-parent --format=%s v0.5.24..origin/main`). A
   `docs:` title does not change the computed version.

3. **Start the release** after that pull request merges:

   ```sh
   gh workflow run release.yml --repo VentureForgre/contexto --ref main -f action=release
   gh run list --repo VentureForgre/contexto --workflow release.yml --limit 1
   ```

   or in the Actions tab: Release > Run workflow > branch `main`, action `release`, tag empty.

   The release covers everything on `main` when the run starts. If something merged after step 1,
   run `psr version --print-tag` again first. A merge that raises the bump (a `feat:` after a
   section written for a patch) changes the version, and the changelog check stops the release
   until the heading is corrected: change the heading of the section already written to the new
   version; do not add a second section. A merge that does not raise it ships under the section
   already written, so add a line for it if users will notice it.

4. **The workflow does the rest:** the eight steps at the top of this file. It refuses to start
   from any ref but `main`, and it takes no version or tag from the person who starts it. When it
   finishes, check the release page (`gh release view vX.Y.Z --repo VentureForgre/contexto`) and,
   if PyPI publishing is on, <https://pypi.org/project/contexto-proxy/>.

5. **Update the documents that name the latest release.** After the release exists, open a `docs:`
   pull request that updates the places that name the latest release by hand, those of them that
   are on `main`: the one versioned line in the README's Install section (the
   `uvx --from <release wheel URL>` line), the version example in `docs/install.md`, and the URL and
   `sha256` of the draft Homebrew formula there. At the first release after v0.5.24 the file names
   in those places also change, from `contexto-<v>...` to `contexto_proxy-<v>...`.

## The changelog

`CHANGELOG.md` is written by hand, newest version first, with an optional `## Unreleased` section
on top. Each version heading has exactly this form:

```
## 0.6.0 (2026-11-13)
```

`.github/scripts/changelog-check.py` runs in the release job after the version is computed and
before anything is built or tagged. It fails, and says what to do, when:

- `CHANGELOG.md` is missing or has no heading in that form for the computed version. A heading for
  the version in another form (`## v0.6.0`, `## [0.6.0] - 2026-11-13`) is pointed at by line. If
  the newest section is for a version above the last release (0.6.1 written, 0.7.0 computed), the
  message says to change that heading, because the section was written before a `feat:` merged;
- the section has no entry: no line besides sub-headings, a placeholder such as "Nothing yet." and
  HTML comments;
- the section appears twice, or is not the newest one;
- the date is not a real date (`2026-99-99`);
- the file contains the line `<!-- version list -->` (see below).

A heading inside a fenced code block is an example and does not count. The date in the heading is
not compared with the day of the release. Entries left under `## Unreleased` give a warning, not a
failure; a "Nothing yet." line is not an entry. Run it locally with
`python3 .github/scripts/changelog-check.py 0.6.0 --last-released v0.5.24`.

The notes on the GitHub Release are still generated by PSR from the commit subjects, and
`CHANGELOG.md` stays hand-written:

- The `release` job runs PSR with `--no-changelog`, so it does not touch the file.
- The `reattach` job runs `semantic-release changelog --post-to-release-tag` on a checkout of
  `main`. That command writes the changelog file before it posts the notes. It leaves a
  hand-written file byte-identical, as long as the file has no `<!-- version list -->` line.
- **`CHANGELOG.md` must never contain the line `<!-- version list -->`.** It is PSR's insertion
  marker: with it, the same command inserts its own generated sections into the runner's copy of
  the file. Nothing is committed, and the `reattach` job checks the tag out with `--force`, so the
  job still finishes; the guard and a test (`tests/test_release_workflow.py`) refuse the marker so
  that it never gets that far.

## The switches

Two repository variables (Settings > Secrets and variables > Actions > Variables) decide what a run
may do. Each is on only when it is exactly `true`.

| Variable | State on 2026-11-12 | What it gates |
|---|---|---|
| `RELEASES_ENABLED` | `true` since 2026-10-16 | Everything. Without it a run only prints "Releases are disabled". |
| `PYPI_PUBLISH_ENABLED` | not set | The `pypi` job. Without it nothing is sent to PyPI. |

Which jobs run:

| `action` | `RELEASES_ENABLED` | `PYPI_PUBLISH_ENABLED` | Jobs that run |
|---|---|---|---|
| any | not `true` | any | `disabled` (a notice) |
| `release` | `true` | not `true` | `request`, `ci`, `release`; then `pypi-disabled` (a notice) if a release was created |
| `release` | `true` | `true` | `request`, `ci`, `release`; then `pypi` if a release was created |
| `reattach` | `true` | any | `request`, `reattach` |
| `publish` | `true` | not `true` | `request`, `pypi-disabled` (fails: publishing is off) |
| `publish` | `true` | `true` | `request`, `pypi` |

The `request` job checks the inputs before anything else runs
(`.github/scripts/release-request.sh`): every action only from `main`; `release` with no tag;
`reattach` and `publish` with a `vX.Y.Z` tag. It catches a mistaken run, such as one started from a
feature branch. It is not what keeps another ref from publishing: a run started from a branch
executes that branch's copy of the workflow and of the script. The `pypi` environment's branch rule
and the attestation check do that (see [PyPI](#pypi)).

### `RELEASES_ENABLED`

- **Who turns it on:** the repository owner, or an agent on the owner's recorded instruction.
- **When it was turned on:** 2026-10-16, after M2 acceptance passed, under D29, which settled O5.
  v0.1.0 was released from f990703. The v0.1.0 to v0.4.0 releases and tags were deleted on
  2026-11-06 (D62); v0.5.0 is the oldest release and tag, so re-attaching it generates notes with no
  earlier tag to start from.
- **Turning it off again:** delete the variable or set it to anything other than `true`.

### `PYPI_PUBLISH_ENABLED`

- **Who turns it on:** the repository owner, after registering the trusted publisher on PyPI. The
  steps are in [docs/pypi-publishing.md](docs/pypi-publishing.md).
- **While it is off:** a release completes on GitHub as before and ends with the notice "PyPI
  publishing is off". `action=publish` fails with the same message, because the one thing it was
  asked to do did not happen.
- **Turning it off again:** delete the variable or set it to anything other than `true`.

## What a release never does

- **It never runs because of a merge.** Only a manual run of the workflow releases.
- **It never pushes a commit to `main`.** `main` is protected by a branch ruleset (pull request
  required, no force push, no deletion, no bypass actors). PSR runs with `--no-commit`, so no version
  bump or changelog commit is made. The workflow then checks that `HEAD` is still the commit the
  run was started on and that the tag points at it. Even if a step tried to push to `main`, the
  ruleset would reject the push, because `GITHUB_TOKEN` is not a bypass actor.
- **It never publishes anywhere but GitHub Releases and, when the switch is on, PyPI.** With
  `PYPI_PUBLISH_ENABLED` off, the sdist and wheel exist only as GitHub Release assets (D42). With
  it on, the same two files also go to PyPI as `contexto-proxy`, by trusted publishing (D67). There
  is no npm, Homebrew, or Scoop package.
- **It never stores a PyPI credential.** No API token or secret for PyPI exists in the repository,
  its settings, or the workflow. PyPI accepts the `pypi` job's short-lived OIDC token instead.
- **It never replaces a file on PyPI.** The `pypi` job asks PyPI what it holds for the version
  first, uploads only files PyPI does not have, and fails if PyPI has a different file.
- **It never edits the version in Git.** No file in the repository carries the release version.
  `pyproject.toml` keeps its static `version`; the release build overwrites it in the runner's
  checkout only (`uv version <v> --frozen`), and `.github/scripts/release-build.sh` fails the release
  unless both artifacts' metadata carry the computed version. The released wheel's version always
  equals its tag. The `version` field on `main` is the placeholder `0.0.0+checkout`, so a build or
  install from a checkout cannot be taken for a release, and the `pypi` job refuses to publish one.

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

A commit's type does not start a release. It decides how far the next release moves the version:

| Type | Meaning | Next release |
|---|---|---|
| `feat` | new feature | at least minor |
| `fix` | bug fix | at least patch |
| `perf` | performance improvement | at least patch |
| `docs` | documentation only | no effect |
| `chore` | maintenance | no effect |
| `refactor` | restructuring without behavior change | no effect |
| `test` | tests only | no effect |
| `ci` | CI and release automation | no effect |
| `style` | formatting only | no effect |
| `build` | build system | no effect |
| `revert` | revert of an earlier commit | no effect |

The highest bump among the commits since the last tag wins. If none of them is a `feat`, `fix` or
`perf`, there is nothing to release, and a release run exits without doing anything.

A breaking change (`feat!:`, `fix!:`, or a `BREAKING CHANGE:` footer) bumps the major version. While
the version is below 1.0.0, `major_on_zero = false` turns a breaking change into a minor bump instead.
A commit that does not follow the format has no effect on the version.

Versions start at 0.1.0. With no tags and no releasable commit, PSR proposes `v0.0.0`; the workflow
treats that as nothing to release.

## Packaging

`main` has carried the package since PR #23 (M2 B1). The workflow handles both states:

- **No `pyproject.toml`:** a releasable commit still gets a tag and a GitHub Release with notes; the
  build step prints that there is nothing to build, and no assets are attached.
- **With `pyproject.toml` (`uv_build` backend and a static version, as on `main`):** the build hook
  stamps the computed version, runs `uv build`, and writes `SHA256SUMS`; both artifacts and the
  checksums are attached.

The distribution is named `contexto-proxy` from the first release after v0.5.24 (D67), because
`contexto` on PyPI is an unrelated project. The command and the import name stay `contexto`.

| Releases | Wheel | Sdist | Metadata `Name` |
|---|---|---|---|
| after v0.5.24 | `contexto_proxy-<v>-py3-none-any.whl` | `contexto_proxy-<v>.tar.gz` | `contexto-proxy` |
| v0.5.24 and earlier | `contexto-<v>-py3-none-any.whl` | `contexto-<v>.tar.gz` | `contexto` |

v0.5.24 and earlier can never be published to PyPI as `contexto-proxy`: their metadata names another
distribution, and the `pypi` job refuses them.

`uv_build` puts the checkout's `pyproject.toml` into the sdist as `pyproject.toml.orig`, next to its
normalized copy, and has no setting to leave it out. The build hook repacks the sdist without it.
uv writes every member and the gzip header with mtime 0, and the repack keeps that, so two builds of
one tree with one uv version give the same bytes. The uv version matters: the wheel's
`.dist-info/WHEEL` names the uv that built it (`Generator: uv 0.13.7`), so CI pins uv. The repack
writes the sdist with gzip level 9, written out because Python 3.15 changed the default to 6, and
both the `release` and the `reattach` job put the same uv-managed Python 3.12 first on `PATH` for
the build.

### Verifying a release

A user can check a downloaded asset three ways:

```sh
gh attestation verify contexto_proxy-<v>-py3-none-any.whl --repo VentureForgre/contexto --source-ref refs/heads/main
shasum -a 256 -c SHA256SUMS --ignore-missing     # or: sha256sum -c SHA256SUMS --ignore-missing
```

or rebuild it from the tag with the uv version the wheel names:

```sh
git clone https://github.com/VentureForgre/contexto.git && cd contexto && git checkout v<v>
NEW_VERSION=<v> bash .github/scripts/release-build.sh   # needs uv, the version in the wheel's WHEEL file
cat dist/SHA256SUMS
```

For v0.5.24 and earlier the files are named `contexto-<v>-py3-none-any.whl` and `contexto-<v>.tar.gz`,
and the rebuilt sdist matches only with a `python3` below 3.15 (their build script uses Python's
default gzip level, which 3.15 changed).

The files on PyPI are the files of the GitHub Release, byte for byte, so the attestation check also
passes on a wheel or sdist downloaded from PyPI.

Releases from v0.5.13 on have `SHA256SUMS` and an attestation, and their sdists do not hold
`pyproject.toml.orig`. v0.5.0 to v0.5.12 have neither `SHA256SUMS` nor an attestation
(`gh attestation verify` finds none), and their sdists hold `pyproject.toml.orig`. Their wheels and
sdists rebuild the same way, from `uv version <v> --frozen` and `uv build`; the wheels of v0.5.0 to
v0.5.8 were built with uv 0.13.5, the later ones with uv 0.13.7.

No VCS-derived version plugin (hatch-vcs, setuptools-scm) is needed.

## PyPI

The `pypi` job publishes a release to <https://pypi.org/project/contexto-proxy/> by
[trusted publishing](https://docs.pypi.org/trusted-publishers/): PyPI exchanges the job's OIDC token
for a short-lived upload credential. The owner's one-time setup is in
[docs/pypi-publishing.md](docs/pypi-publishing.md). The job is off until `PYPI_PUBLISH_ENABLED` is
`true`.

It runs in two cases: after a `release` run that created a release, and for `action=publish` with
an existing tag. It starts only after the GitHub release is complete, and no other job waits for
it, so a failure here leaves the GitHub release as it is.

What it does, in order:

1. Checks that the tag is an existing `vX.Y.Z` tag on `main`'s first-parent chain
   (`.github/scripts/reattach-verify.sh`).
2. Downloads the wheel, the sdist and `SHA256SUMS` from the tag's GitHub Release
   (`gh release download`). It does not build anything.
3. Verifies them (`.github/scripts/pypi-publish.py verify`, then `sha256sum --check --strict`):
   exactly one wheel and one sdist; `SHA256SUMS` lists exactly those two, with matching digests; the
   metadata `Name` of both is `contexto-proxy` and `Version` is the tag without `v`. A release from
   before the rename, a build from a checkout (`0.0.0+checkout`), and a release without
   `SHA256SUMS` stop here.
4. Verifies the build provenance attestation of both files
   (`gh attestation verify --repo ... --signer-workflow .../release.yml --source-ref refs/heads/main --deny-self-hosted-runners`).
   A file passes only if it was attested for this repository, by `release.yml` as it was on `main`,
   on a GitHub-hosted runner. `--signer-workflow` alone would also accept a run of a branch's copy
   of `release.yml`; `--source-ref` is what excludes it. The commit is not pinned, because a
   reattach run is attested at the head of `main` at that time, not at the tag's commit.
5. Asks PyPI what it holds for the version (`pypi-publish.py plan`, which reads
   `https://pypi.org/pypi/contexto-proxy/<version>/json`):

   | PyPI answers | The job |
   |---|---|
   | 404 | uploads both files |
   | 200, every file there has our sha256 | uploads the files PyPI lacks, or nothing, and succeeds with a notice |
   | 200, a file with another sha256, or a file we do not have | fails: a PyPI version cannot be replaced; cut a new release |
   | 200, no files at all | fails: the version's files were deleted, and PyPI never takes a deleted file's name again |
   | anything else (5xx, a redirect, a broken or missing answer), three times | fails without uploading |

6. Uploads with the pinned uv:
   `uv publish --trusted-publishing always --publish-url https://upload.pypi.org/legacy/ --check-url https://pypi.org/simple/ --no-attestations <files>`.
7. Waits until PyPI lists both files with the release's digests (`pypi-publish.py confirm`, up to
   twenty requests over two minutes).

Why it is built this way:

- **The `pypi` environment.** The job runs in the GitHub environment `pypi`, and the trusted
  publisher on PyPI is registered with that environment name. The `release` and `reattach` jobs
  also hold an OIDC token, for the attestation, and they run python-semantic-release, which is
  fetched from PyPI at run time. Their token names no environment, so PyPI refuses it. Only a job
  in the `pypi` environment can publish.
- **The environment's branch rule.** The environment exists since 2026-11-13 and is limited to the
  branch `main` ("Selected branches and tags", one branch rule; D67). PyPI checks the repository,
  the workflow file name and the environment, and not the ref. So without that rule, anyone who
  can push a branch could put their own `release.yml` on it, with a job in the `pypi` environment,
  start it with `--ref <branch>`, and publish any files. The checks in this workflow would not
  run, because the branch's copy replaces them. The branch rule is a repository setting, outside
  the workflow file, and GitHub refuses the environment to a run from any other ref. Never delete
  the environment or its branch rule while the publisher is registered. A required reviewer is an
  optional extra the owner can add.
- **The attestation check.** The branch rule closes the direct path. Release assets are the other
  one: anyone with write access can replace them. Step 4 refuses files that `release.yml` on
  `main` did not build and attest.
- **What the `pypi` job does not vouch for.** The integrity of the published files rests on the
  `release` or `reattach` job that built and attested them. Those jobs run python-semantic-release
  10.7.0 with dependencies that are not pinned, and whatever they attest and attach, `pypi`
  publishes.
- **No tool fetched at run time.** The job runs pinned actions (`actions/checkout`,
  `astral-sh/setup-uv`), the pinned uv binary, and the runner's `gh`, `sha256sum` and `python3`
  with the standard library only. It does not run `uvx`, python-semantic-release or twine.
- **`--trusted-publishing always`.** uv fails if the OIDC exchange fails. It never falls back to
  another credential.
- **`--check-url`.** A second guard after step 5: uv reads PyPI's index itself, skips a file that is
  already there with the same hash, and stops on a file of the same name with other contents.
- **`--no-attestations`.** uv 0.13.7 does not create PyPI attestations (PEP 740). It only uploads
  `*.publish.attestation` files that lie next to the artifacts, and the job has none. Making them
  would need a signing tool fetched at run time. So the files on PyPI carry no PyPI attestation;
  their provenance is the GitHub attestation, which verifies against the same bytes (see
  [Verifying a release](#verifying-a-release)).

## Configuration

| File or setting | Purpose |
|---|---|
| `.github/workflows/release.yml` | Manual runs only: `release`, `reattach`, `publish`; gated on `RELEASES_ENABLED`, the `pypi` job also on `PYPI_PUBLISH_ENABLED` |
| `.github/workflows/ci.yml` | Pull request checks; also the required gate for a release (`workflow_call`) |
| `.github/semantic-release.toml` | PSR settings: `v{version}` tags, conventional parser, release types, `main` only, no version files |
| `.github/scripts/release-request.sh` | First job of every run: checks the action, the tag and the ref (every action from `main` only). It catches a mistaken run; it is not a control |
| `.github/scripts/changelog-check.py` | Release guard: `CHANGELOG.md` must have the section `## X.Y.Z (YYYY-MM-DD)` for the computed version, and no `<!-- version list -->` line |
| `.github/scripts/release-build.sh` | PSR `build_command`: stamp, build, drop `pyproject.toml.orig` from the sdist, verify artifact versions, write `SHA256SUMS`; skip cleanly without `pyproject.toml` |
| `.github/scripts/reattach-verify.sh` | Guard for `reattach` and `pypi`: the tag must be an existing `vX.Y.Z` tag on `main`'s first-parent chain |
| `.github/scripts/pypi-publish.py` | The `pypi` job's checks: verify the downloaded files, decide what may be uploaded, confirm the upload. It never uploads |
| `.github/scripts/ensure-gh.sh` | `release` and `reattach`: use `gh` if the runner has it (hosted runners do), else install a pinned, checksum-verified `gh` |
| Variable `RELEASES_ENABLED` | Releases on or off |
| Variable `PYPI_PUBLISH_ENABLED` | PyPI publishing on or off |
| Environment `pypi` | The GitHub environment of the `pypi` job, named in the trusted publisher on PyPI. It exists since 2026-11-13 and is limited to the branch `main` (D67); that rule is what stops a run from another ref from publishing. Administrators cannot bypass it. No required reviewer; the owner may add one |

CI runs `ruff check`, then `uv run --with pytest pytest` and `uv build` on each Python and OS in its
test matrix. Release's call runs the same jobs. The pytest run collects spike tests under `spikes/`
as well. That is intended, because those tests back numbers that research documents cite. The
release scripts and the shape of `release.yml` have tests of their own (`tests/test_release_*.py`).

Every CI and release job runs on a GitHub-hosted `ubuntu-latest` runner, or `macos-latest` for the
macOS test jobs. From PR #188 until the repository went public they ran on the organization's
self-hosted runner. A self-hosted runner on a public repository would let a fork's pull request
run code on that machine.

Third-party actions are pinned by commit SHA, with the release tag in a comment:
`actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.2.0, and `actions/attest-build-provenance`
v4.2.2. The `pypi` job adds no action. To update one, resolve the new tag's commit
(`gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`) and update the SHA and the comment together.
uv itself is pinned in every `setup-uv` step (`version: "0.13.7"`); keep it inside the `uv_build`
range in `pyproject.toml` (`>=0.8,<0.14`) and move both together.

Permissions. The workflow's default is `contents: read`. The `release` and `reattach` jobs have
`contents: write`, plus `id-token: write` and `attestations: write` for the attestation only. The
`pypi` job has `id-token: write`, for the token PyPI exchanges; `contents: read`, for the checkout
and the release download; and `attestations: read`, for `gh attestation verify`. No other job gets
an OIDC token, and no job but `release` and `reattach` can write anything.

The attestation runs before the upload to the GitHub Release. If it fails, the release exists with
its notes but without its files; run the reattach job for that tag to attach and attest them.

## Dry run

Compute the next version on any checkout without changing anything:

```sh
psr() { uvx --from python-semantic-release==10.7.0 semantic-release -c .github/semantic-release.toml "$@"; }
psr version --print-last-released-tag    # empty when there is no release yet
psr version --print-tag                  # the tag the next release would get
psr --noop version --no-commit --tag --push --vcs-release --no-changelog   # show every step and the notes
python3 .github/scripts/changelog-check.py "$(psr version --print-tag)" \
  --last-released "$(psr version --print-last-released-tag)"                # the changelog guard
```

The function works in bash and zsh. (A `PSR="uvx ..."` variable does not: zsh does not split
`$PSR` into words.)

To rehearse a full release without touching GitHub, clone into a scratch directory, make a bare
clone to act as `origin`, push the commits under test to its `main`, and run
`psr version --no-commit --tag --push --no-vcs-release --no-changelog` there.

To rehearse the `pypi` job's checks, build in a scratch copy and point the script at the files.
`verify` reads only the files. `plan` and `confirm` only read from the index. `plan` asks again,
up to three times in all and 5 seconds apart, while the answer is neither a 404 nor a 200 with a
readable file list; it stops at the first 404 or file list. `confirm` asks until both files are
listed, up to twenty times, 6 seconds apart.

```sh
NEW_VERSION=9.9.9 bash .github/scripts/release-build.sh
python3 .github/scripts/pypi-publish.py verify --dist dist --project contexto-proxy --tag v9.9.9
python3 .github/scripts/pypi-publish.py plan --dist dist --project contexto-proxy --tag v9.9.9 --output /tmp/upload.txt
uv publish --dry-run --trusted-publishing never --check-url https://pypi.org/simple/ dist/*.whl dist/*.tar.gz
```

`uv publish --dry-run` checks the files and the index and uploads nothing. Never run `uv publish`
by hand without `--dry-run`: publishing is the workflow's job.

## Recovery

- **The tag exists but the release is missing or has no assets** (for example, release creation or
  the upload failed after the tag was pushed): run the workflow with `action=reattach` and the
  existing tag, such as `v0.5.24`
  (`gh workflow run release.yml --ref main -f action=reattach -f tag=v0.5.24`). Like every action
  it is run from `main`. The `reattach` job checks out `main` with full history.
  It refuses anything except an existing `vX.Y.Z` tag whose commit is on `main`'s first-parent
  chain, the commits `main` itself has pointed at (`.github/scripts/reattach-verify.sh`), failing
  before it creates anything. A commit that reached `main` inside a merge commit is refused. If the release is missing, it first checks that the notes can be generated, then creates
  the release and posts that tag's Conventional Commit notes
  (`semantic-release changelog --post-to-release-tag`, which needs a branch checkout, not a detached
  tag). Finally it checks out the tag, rebuilds at the tag's version, attests the rebuilt sdist and
  wheel, and uploads them with `SHA256SUMS` (written by the workflow when the tag's build script
  predates it).
  It never creates a tag or a new version. Running `action=release` again, or "Re-run failed
  jobs", does nothing here, because it sees the tag and treats that version as released.
  A reattach never publishes to PyPI. If PyPI publishing is on, run `action=publish` for the tag
  afterwards.
- **What a reattach changes.** The rebuild uses the pinned uv, the pinned Python and the tag's own
  build script. From v0.5.13 on it gives the same bytes as the first build, as long as the uv pin
  or the Python that runs the build has not moved. The build script now writes the sdist with gzip
  level 9, which protects tags cut from now on. Tags up to v0.5.24 run their own
  script, which uses Python's default level: a reattach of one of them whose `python3` is 3.15 or
  later changes the sdist's bytes (rebuilt v0.5.24: `d367cc88...` under 3.12, `e8618343...` under
  3.15). The pinned 3.12 keeps that from happening, as long as the pin stays below 3.15.
  For v0.5.9 to v0.5.12 it also gives the same wheel and sdist (the sdist still holds
  `pyproject.toml.orig`), and adds the `SHA256SUMS` and the attestation they never had. For v0.5.8
  and earlier the sdist is the same, but the wheel differs from the one first published in one line
  of its `WHEEL` file (uv 0.13.7 instead of 0.13.5), so its checksum changes.
- **Do not reattach a release that is on PyPI after the uv pin or the Python that runs the build
  has moved.** The rebuilt wheel or sdist would differ from the file on PyPI, which cannot be
  replaced, and the GitHub asset would no longer be the PyPI file.
- **The release is on GitHub and not on PyPI.** Three ways to get there: the PyPI upload failed;
  the release was cut while PyPI publishing was off; or the `release` job failed after the tag was
  pushed, so the `pypi` job never ran, and the release was completed with `action=reattach`. Fix
  the cause, then run `gh workflow run release.yml --ref main -f action=publish -f tag=vX.Y.Z`. It
  publishes the files already on the GitHub Release, and is safe to repeat: it uploads only what
  PyPI does not have. If one file reached PyPI and the other did not, the next run uploads the
  missing one.
- **`pypi` refuses a release's assets.** It publishes only files that `release.yml` on `main` built
  and attested on a hosted runner. Assets attached any other way are refused: uploaded by hand,
  built by a run started from another ref, or from v0.5.12 and earlier, which have no attestation.
  For a release cut after the rename, `action=reattach` from `main` rebuilds and attests the
  assets, and `action=publish` then accepts them, provided the version is not on PyPI yet with
  other bytes.
- **A release is broken:** fix it with a `fix:` pull request and cut the next patch release the same
  day. If the broken version is on PyPI, also yank it there (Manage project > Releases > Options >
  Yank, with a reason). Installers skip a yanked version unless it is pinned exactly, and it stays
  downloadable for those who pinned it.
- **Never delete a release, a tag or a PyPI version to "redo" it.** PyPI never accepts the same
  file name again, even after the file, the release or the whole project is deleted. A deleted
  version number is lost; the fix is always a new version.
- **Do not create release tags by hand.** Versions come only from the workflow. Nothing technical
  enforces this: a `vX.Y.Z` tag pushed by hand onto a commit of `main`'s first-parent chain could be
  reattached and then published, and the tagged commit's own tree and build script are what gets
  built. That version could never be taken back from PyPI.
