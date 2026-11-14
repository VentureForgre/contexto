# Turning on PyPI publishing

Contexto's release workflow can publish each release to PyPI as `contexto-proxy` (D67). The
command and the import name stay `contexto`. Publishing is off until the repository owner does the
steps on this page. Step 2 needs the owner's PyPI account. Steps 3 and 4 change the repository's
settings, which is the owner's decision.

How the workflow publishes, and what it checks first, is in [RELEASING.md](../RELEASING.md#pypi).
There is no API token to create, store or rotate. PyPI trusts the `pypi` job of this repository's
`release.yml` directly ([trusted publishing](https://docs.pypi.org/trusted-publishers/)).

## 1. Check the `pypi` environment and its `main` branch rule

The GitHub environment `pypi` already exists. It was created on 2026-11-13 with deployment limited
to the branch `main` ("Selected branches and tags", one branch rule `main`), no required reviewer
and no wait timer (D67). Check that this is still so before you register anything on PyPI.

In the repository: Settings > Environments > `pypi`. Under "Deployment branches and tags" it must
say "Selected branches and tags" with one rule, the branch `main`.

Or from a shell:

```sh
gh api repos/VentureForgre/contexto/environments/pypi --jq '{name, protection_rules: [.protection_rules[].type], deployment_branch_policy}'
gh api repos/VentureForgre/contexto/environments/pypi/deployment-branch-policies --jq '[.branch_policies[] | {name, type}]'
```

The expected output:

```
{"deployment_branch_policy":{"custom_branch_policies":true,"protected_branches":false},"name":"pypi","protection_rules":["branch_policy"]}
[{"name":"main","type":"branch"}]
```

If the first command answers 404, or the second lists anything but the one branch `main`, put it
right first: Settings > Environments > New environment, named `pypi`; Deployment branches and tags
> "Selected branches and tags" > Add deployment branch or tag rule > branch `main`.

Why this comes first. PyPI checks three things in the job's token: the repository, the workflow's
file name and the environment name. It does not check the branch. A run started from a branch
executes that branch's copy of `release.yml`, so the checks and the switch in `main`'s copy do not
apply to it. With an environment that any ref may use, anyone who can push a branch to this
repository could put their own `release.yml` on it, with a job in the `pypi` environment, start it
from that branch, and publish any files as `contexto-proxy`, from the moment the publisher is
registered. PyPI's security-model page says the same: by default "anybody who can unconditionally
commit to your repository can also modify your publishing workflow", and it recommends a dedicated
environment. The branch rule is a repository setting, outside the workflow file, and GitHub refuses
the environment to a run from any ref but `main`.

**Never delete the `pypi` environment or its branch rule while the publisher is registered on
PyPI.** A workflow run that names a missing environment creates it again with no rules at all. If
either has to go, remove the publisher on PyPI first.

## 2. Register the pending publisher on PyPI

The project does not exist on PyPI yet, so the publisher is registered on the account, as a
"pending" publisher. PyPI creates the project when the publisher is first used.

1. Sign in to <https://pypi.org> with the account that should own the project. The account needs
   two-factor authentication.
2. Open the account's Publishing page: <https://pypi.org/manage/account/publishing/>
   ("Publishing" in the account sidebar).
3. Under "Add a new pending publisher", choose the GitHub tab and fill in exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `contexto-proxy` |
   | Owner | `VentureForgre` |
   | Repository name | `contexto` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

4. Click "Add". The pending publisher is then listed on the same page.

Fill in the environment name, although the form marks it optional. With it, PyPI accepts an OIDC
token only from a job that runs in the GitHub environment `pypi`. The workflow's `release` and
`reattach` jobs also hold an OIDC token (they sign the build attestation), and they run a tool
fetched from PyPI at run time. Without the environment name on PyPI, a token from one of those jobs
could publish, and so could a run from any branch, because the branch rule of step 1 guards only
jobs that name the environment. With it, only the `pypi` job can.

"Workflow name" is the file name only, not a path. The workflow must stay
`.github/workflows/release.yml`, and the publishing job must stay in that file: PyPI does not accept
a reusable workflow as the trusted workflow.

## 3. Add a required reviewer (optional)

In the repository: Settings > Environments > `pypi` > Required reviewers. Add yourself. Every run of
the `pypi` job then waits until a reviewer approves it (Actions > the run > Review deployments). One
approval is enough. Leave "Prevent self-review" off if you are the only reviewer and you start the
runs. Only a repository admin can change an environment's rules.

- **Without a reviewer** (the state today), the `pypi` job runs as soon as the release job has
  finished. What gets published is then decided by who can start the workflow on `main` and by the
  workflow's own checks.
- **With a reviewer,** nothing reaches PyPI until you approve. The workflow runs one at a time
  (`concurrency: release`). While a `pypi` job waits for approval its run stays open, a second
  Release run waits behind it, and a third run started meanwhile replaces the waiting second one,
  which is cancelled. So approve or reject promptly.

A reviewer is also the only thing that would stop a tag pushed by hand. Tags must not be pushed by
hand: a `vX.Y.Z` tag on a commit of `main` could be reattached and published, and with no required
reviewer nothing stops it. A version on PyPI can never be taken back.

## 4. Turn the switch on

```sh
gh variable set PYPI_PUBLISH_ENABLED --body true --repo VentureForgre/contexto
```

or in the repository: Settings > Secrets and variables > Actions > Variables > New repository
variable, name `PYPI_PUBLISH_ENABLED`, value `true`.

## 5. Publish

Either cut the next release ([RELEASING.md](../RELEASING.md#cutting-a-release)), which publishes
when it finishes, or publish a release that already exists:

```sh
gh workflow run release.yml --repo VentureForgre/contexto --ref main -f action=publish -f tag=vX.Y.Z
```

Only a release whose files are named `contexto_proxy-<v>-py3-none-any.whl` and
`contexto_proxy-<v>.tar.gz` can be published. v0.5.24 and earlier are named `contexto-<v>...`, and
their metadata says `Name: contexto`; the job refuses them and says why. It also refuses files that
`release.yml` on `main` did not build and attest.

Publish soon after step 2. A pending publisher does not reserve the name (see below).

## 6. Check that it worked

- The run's `pypi` job is green, and its last step says PyPI lists both files with the release's
  checksums.
- <https://pypi.org/project/contexto-proxy/> shows the version.
- On the account's Publishing page the publisher is no longer listed as pending. It now belongs to
  the project ("Publishing" in the project's sidebar).
- It installs and runs:

  ```sh
  uvx --from contexto-proxy contexto --version
  ```

If the upload step fails with `invalid-publisher`, PyPI received a valid token that matches no
publisher. Compare the five fields in step 2 with the table, letter by letter. Then run
`action=publish` for the same tag. The GitHub release is not affected by a failed PyPI job.

## What to know

**A pending publisher does not reserve the name.** PyPI's documentation: a pending publisher "does
not create a project or reserve a project's name until it is actually used to publish". If someone
else registers `contexto-proxy` first, the pending publisher is invalidated. The project is created
at the first upload and is owned by the account that registered the pending publisher (that is what
PyPI's server code does; the documentation does not say it).

**PyPI can refuse a name that looks free.** Its documentation lists four reasons a name may be
unavailable even if no project of that name appears: it conflicts with a module of the Python
standard library; it is "too similar to an existing project, creating a risk of confusion"; an
administrator has prohibited it; or another user has registered it and not published yet. The
documentation does not say how similarity is measured, so only the first upload shows whether
`contexto-proxy` is accepted. What is known:

- `https://pypi.org/pypi/<name>/json` returned 404 on 2026-11-12 for `contexto-proxy`,
  `contexto_proxy` and `contextoproxy`. No project has the name in any of those spellings.
- `contexto` is taken by an unrelated project (ConTexto 0.2.0), which is why the distribution was
  renamed.
- `contexto-cli` and `ctxo` also returned 404 on that day. They are the fallbacks.

If PyPI refuses the name, the failure is in the upload step, where the project would be created.
The GitHub release is unaffected, but its files are named for `contexto-proxy` and cannot be
published under another name. Using a fallback means changing the name in every place
`git grep -n 'contexto-proxy\|contexto_proxy'` finds. The ones that matter:

- `name` in `pyproject.toml`, and `uv.lock`;
- the `DISTRIBUTION` lookup in `src/contexto/__init__.py`. If it is missed, `contexto --version`
  reports a wrong version;
- `PROJECT` and the environment `url` in `.github/workflows/release.yml`;
- the package-check and install-check scripts in `.github/scripts/`;
- `tests/test_packaging.py` and `tests/test_release_workflow.py`;
- `docs/pypi-description.md` and the other documents.

Then register a new pending publisher for the new name and cut a new release.

**A version is permanent.** PyPI never accepts a file name twice: "PyPI does not allow for a
filename to be reused, even once a project has been deleted and recreated." A file name is the
project name, the version and the distribution type, so a deleted version cannot be uploaded again.
Deleting is "permanent and irreversible", and deleting the project releases the name to any other
user.

**Prefer yanking to deleting.** To withdraw a bad release, yank it: Manage project > Releases >
Options > Yank, with a reason. Installers ignore a yanked release unless it is requested with an
exact pin (`==`), and its files stay available. Then release a fix under a new version. PyPI yanks
whole releases, not single files.

**Turning publishing off.** Delete the variable, or set it to anything but `true`:

```sh
gh variable delete PYPI_PUBLISH_ENABLED --repo VentureForgre/contexto
```

Releases then go to GitHub only, and each release run says so in a notice. The variable is read by
`main`'s copy of the workflow, so it is a switch, not a lock. To withdraw PyPI's trust, remove the
publisher on the project's Publishing page.
