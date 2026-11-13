# Turning on PyPI publishing

Contexto's release workflow can publish each release to PyPI as `contexto-proxy` (D67). The
command and the import name stay `contexto`. Publishing is off until the repository owner does the
steps on this page. Nothing here can be done by an agent or by the workflow: the first three steps
need the owner's PyPI account and the repository's settings.

How the workflow publishes, and what it checks first, is in [RELEASING.md](../RELEASING.md#pypi).
There is no API token to create, store or rotate. PyPI trusts the `pypi` job of this repository's
`release.yml` directly ([trusted publishing](https://docs.pypi.org/trusted-publishers/)).

## 1. Register the pending publisher on PyPI

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
could publish. With it, only the `pypi` job can.

"Workflow name" is the file name only, not a path. The workflow must stay
`.github/workflows/release.yml`, and the publishing job must stay in that file: PyPI does not accept
a reusable workflow as the trusted workflow.

## 2. Protect the `pypi` environment on GitHub (optional, recommended)

In the repository: Settings > Environments > New environment, named `pypi`. Then:

- **Required reviewers:** add yourself. Every run of the `pypi` job then waits until a reviewer
  approves it (Actions > the run > Review deployments). One approval is enough. Leave "Prevent
  self-review" off if you are the only reviewer and you start the runs.
- **Deployment branches and tags:** choose "Selected branches and tags" and add a branch rule for
  `main`. A run started from any other ref then cannot use the environment.

What happens either way:

- **If you skip this step,** the first run of the `pypi` job creates the environment itself, with no
  protection rules. The job runs without waiting. The environment name still matches the one on
  PyPI, so publishing works.
- **If you add a required reviewer,** nothing reaches PyPI until you approve. While the job waits,
  the run stays open and the next Release run waits behind it, so approve or reject promptly.

Only a repository admin can change an environment's rules.

## 3. Turn the switch on

```sh
gh variable set PYPI_PUBLISH_ENABLED --body true --repo VentureForgre/contexto
```

or in the repository: Settings > Secrets and variables > Actions > Variables > New repository
variable, name `PYPI_PUBLISH_ENABLED`, value `true`.

## 4. Publish

Either cut the next release ([RELEASING.md](../RELEASING.md#cutting-a-release)), which publishes
when it finishes, or publish a release that already exists:

```sh
gh workflow run release.yml --repo VentureForgre/contexto --ref main -f action=publish -f tag=vX.Y.Z
```

Only a release whose files are named `contexto_proxy-<v>-py3-none-any.whl` and
`contexto_proxy-<v>.tar.gz` can be published. v0.5.24 and earlier are named `contexto-<v>...`, and
their metadata says `Name: contexto`; the job refuses them and says why.

Publish soon after step 1. A pending publisher does not reserve the name (see below).

## 5. Check that it worked

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
publisher. Compare the five fields in step 1 with the table, letter by letter, and check that the
job ran in the `pypi` environment. Then run `action=publish` for the same tag. The GitHub release is
not affected by a failed PyPI job.

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
published under another name. Using a fallback takes: the `name` in `pyproject.toml`; `PROJECT` and
the environment `url` in `.github/workflows/release.yml`; the project name in the tests
(`tests/test_release_workflow.py`) and in the documents; a new pending publisher for the new name;
and a new release.

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

Releases then go to GitHub only, and each release run says so in a notice. To also withdraw PyPI's
trust, remove the publisher on the project's Publishing page.
