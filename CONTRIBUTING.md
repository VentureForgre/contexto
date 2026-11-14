# Contributing to Contexto

Contexto is a local proxy that keeps Claude Code and Codex sessions under a token bound so the CLI
never compacts; old turns go to a store on disk and the model reads them back with MCP tools. See
the [README](README.md) for what it does and how to use it.

Bug reports and pull requests are welcome. For a security problem, use private reporting instead
(see [SECURITY.md](SECURITY.md)).

## Set up

Contexto is a Python 3.12+ package with no runtime dependencies. Development uses
[uv](https://docs.astral.sh/uv/). Work from a git clone: one test checks that it runs inside a git
work tree, so the suite fails one test in a source archive, and the sdist has no tests at all.

```sh
git clone https://github.com/VentureForgre/contexto.git
cd contexto
uv run contexto --help                # creates .venv and runs the checkout's code
```

The distribution is named `contexto-proxy`, its name on PyPI, where `contexto` is an unrelated
project. The command and the import name are `contexto`.

An install from a checkout reports its version as `0.0.0+checkout`. Only release builds carry a real
version (see [RELEASING.md](RELEASING.md)). An install from a git URL also prints the revision, which
the installer records in the distribution's `direct_url.json`:
`contexto 0.0.0+checkout (git v1.2.3, commit 0123abcd)` for a tag or branch, and
`contexto 0.0.0+checkout (git commit 0123abcd)` when the URL names a commit or no revision.

## Tests and lint

```sh
uv run --with pytest pytest -q        # the whole suite, about 4 minutes
uvx ruff@0.17.2 check .               # lint, with the rules in pyproject.toml
uv build                              # the sdist and wheel, into dist/
bash .github/scripts/install-check.sh 3.12   # install the built wheel and sdist, run the command
bash .github/scripts/package-check.sh        # twine check, metadata, the rendered PyPI description
```

The two scripts need only uv. They build into a new temporary directory, create their virtual
environments there, and remove it when they finish. `install-check.sh` takes the Python version to
use.

The suite collects `tests/` (the package) and `spikes/` (measurement code that the research notes in
`docs/research/` cite). It makes no model calls and needs no API key. Tests that drive a real
`claude` or `codex` point it at a stub server on `127.0.0.1`.

Some tests skip when something is missing, and pytest prints the reason:

- `claude` not on `PATH`: the tests against the real Claude Code MCP client and the acceptance
  driver's launch tests.
- `codex` not on `PATH`: the Codex acceptance and live tests.
- `claude-agent-sdk` not installed: the Agent SDK recipe test of `docs/serve.md`. Run it with
  `uv run --with pytest --with claude-agent-sdk pytest tests/test_serve_recipes.py`.
- Python below 3.14: the zstd request-body test.
- A shallow clone: one research test that reads git history.
- Running as root: two file-permission tests.

CI has neither `claude` nor `codex`, so a green CI run does not cover the tests against the real
CLIs. If you change how Contexto talks to a CLI, install it and run the suite locally.

Without uv, a plain virtual environment works. Activate it, so the tests that run the `contexto`
command find the checkout's and not another installed copy:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e . pytest
pytest -q
```

`pip install -e .` installs the checkout as before. `pip list` shows it as `contexto-proxy`, and
`pip uninstall contexto-proxy` removes it.

A virtual environment made before the rename still holds the old `contexto` distribution. With uv,
run `uv sync --reinstall-package contexto-proxy` once: it removes the old distribution and puts back
the `contexto` command, which the old one owned. In a plain virtual environment run
`pip uninstall contexto` and then `pip install -e .` again; until then `contexto --version` prints
`0.0.0+unknown` and two packaging tests fail.

The suite runs on macOS and Linux. Windows is not supported (see [Platforms](docs/install.md#platforms)).

## What CI runs

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every pull request:

- `lint`: `uvx ruff@0.17.2 check .`. `ruff format` is not enforced.
- `test`: `uv run --with pytest==9.1.1 pytest` and `uv build`, on Python 3.12, 3.13, 3.14 and 3.15
  on Ubuntu, and on 3.12 and 3.15 on macOS.
- `install`: `.github/scripts/install-check.sh`, on the same Pythons and systems as `test`. It builds
  the sdist and wheel, installs the wheel into an empty virtual environment, and from a directory
  outside the checkout runs `contexto --version`, `contexto --help`, `python -m contexto --version`
  and `contexto sessions list` against an empty `CONTEXTO_HOME`. Then it installs the sdist into a
  second empty environment and checks that it holds the same package.
- `package`: `.github/scripts/package-check.sh`, once. It runs `twine check --strict` on the sdist
  and wheel, checks their name, metadata (every classifier is one PyPI knows) and contents, builds
  a wheel again from the unpacked sdist, and renders the PyPI description
  ([docs/pypi-description.md](docs/pypi-description.md)) with `readme_renderer`, the renderer PyPI
  uses. It uploads nothing.
- `ci`: passes only when lint, package and every test and install job pass. It is the one required
  check.

uv, Ruff, pytest, twine, readme-renderer, trove-classifiers and the actions are pinned, so a new
release of any of them cannot fail CI on code that did not change. Workflow runs on pull requests
from forks wait for a maintainer to approve them.

## Pull requests

- Branch from `main` and open the pull request against `main`. `main` is protected by a branch
  ruleset: changes reach it only through a pull request with a passing `ci` check.
- The pull request title must be a [Conventional Commit](https://www.conventionalcommits.org/),
  for example `fix: recall reads a page written by an older release`. Pull requests are squash
  merged, so the title becomes the commit on `main`. Merging does not release anything. The title
  decides how far the next release moves the version: `feat:` makes it at least a minor release,
  `fix:` and `perf:` at least a patch, and `docs:`, `test:`, `build:`, `ci:`, `chore:` and
  `refactor:` have no effect. The table is in [RELEASING.md](RELEASING.md#conventional-commits).
- For a bug fix, add a test that fails without the fix.
- Run the suite and the lint before you push.
- A change to what the proxy does to requests is a change to the design contract,
  [docs/milestones/M2-contexto-proxy.md](docs/milestones/M2-contexto-proxy.md). Say so in the pull
  request; the maintainers record the decision and its reason in
  [docs/DECISIONS.md](docs/DECISIONS.md).
- Do not commit API keys, tokens, Claude or Codex settings, real transcripts, or anything from
  `~/.contexto`.

Every pull request is checked by two AI reviewer agents, and pull requests from outside contributors
are also reviewed by the maintainer. The agents post their
verdicts as comments from the maintainer's account, titled "Reviewer A" and "Reviewer B". A pull
request merges once both approve its latest commit; a new push needs new verdicts. How the maintainer
runs the project with AI agents is in [docs/maintainer-process.md](docs/maintainer-process.md).

Contexto is MIT licensed ([LICENSE](LICENSE)). By opening a pull request you agree that your
contribution is licensed under the same terms.

## Where things are

- `src/contexto/`: the package. `proxy.py` is the HTTP proxy, `rewrite.py` the paging engine,
  `store/` the session store, `mcp.py` the MCP server, `launcher.py` and `codex.py` the
  `contexto run` launchers, `commands/` the other commands.
- `tests/`: the package tests.
- `spikes/` and `docs/research/`: measurement code and results from the design work. They are not
  part of the package.
- `docs/milestones/`: design contracts. `docs/DECISIONS.md`: decisions and their reasons.
- [docs/cli-updates.md](docs/cli-updates.md): how a new Claude Code or Codex version is tested.
- [docs/README.md](docs/README.md): the index of the guides, design records and research notes.

## Releases

Releases are cut by hand and batched, normally no more than one a week; a security fix or a fix
for a broken release ships the same day. Merging a pull request never releases by itself. The
maintainer starts the release workflow, which computes the version from the Conventional Commits
on `main` since the last release and requires a section for it in `CHANGELOG.md`.
[RELEASING.md](RELEASING.md) describes the rule, the steps, the version rules, and recovery.
