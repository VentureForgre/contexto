# Install

[Contexto](../README.md) / [Documentation](README.md)

The [README](../README.md#install) has the one command that installs Contexto. This page has the
install script's options, the other ways to install, how to upgrade each, how to check a download,
how to uninstall, and what to do when an install goes wrong.

## What you need

- Python 3.12 or later. Contexto has no runtime dependencies.
- macOS or Linux. See [Platforms](#platforms).
- `claude` (Claude Code) or `codex` (Codex CLI) on `PATH`.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) or
  [pipx](https://pipx.pypa.io/stable/installation/), to install a command-line tool into its own
  environment.

## Two names

The command and the Python import are `contexto`. The distribution, which is the name installers
use, changed once:

| Releases | Distribution | Wheel file |
|---|---|---|
| up to 0.5.24 | `contexto` | `contexto-<version>-py3-none-any.whl` |
| after 0.5.24 | `contexto-proxy` | `contexto_proxy-<version>-py3-none-any.whl` |

The name changed because `contexto` on PyPI is an unrelated library. `pip install contexto`,
`pipx install contexto` and `uvx contexto` fetch that library, not this project.

Both distributions provide the same `contexto` command, so only one of them can be installed at a
time. See [From the earlier distribution name](#from-the-earlier-distribution-name).

## The install script

```sh
curl -fsSL https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh | sh
```

To read it before running it, download it and run the file:

```sh
curl -fsSLO https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh
less install.sh
sh install.sh
```

What it does, in order:

1. Finds the latest release. It follows the redirect of
   `https://github.com/VentureForgre/contexto/releases/latest` and takes the tag from the final URL.
   It does not use the GitHub API, so there is no rate limit and no login.
2. Downloads that release's `SHA256SUMS`. It accepts the file only if every line is a SHA-256 and
   one of the file names a release of that version can have.
3. Downloads the wheel into a temporary directory that only you can read, and checks its SHA-256
   against `SHA256SUMS`. The directory is removed when the script exits.
4. With `--attest`, checks the wheel's build attestation.
5. Removes an earlier Contexto install that provides the same `contexto` command: one under the
   other distribution name, or one made by the other tool.
6. Installs the checked wheel file with `uv tool install` or `pipx install`.
7. Runs `contexto --version` and prints the result.

Until step 4 has passed, nothing is installed or removed. A failed check stops the script with a
message and a non-zero exit status.

What it never does: install uv, pipx or Python; use `sudo`; edit a shell profile; read or write your
session data in `~/.contexto` (or `$CONTEXTO_HOME`).

It needs `curl`, and `sha256sum` or `shasum`. Downloads are https only.

### Options

Options go after `sh -s --` when the script is piped:

```sh
curl -fsSL https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh | sh -s -- --dry-run
```

| Option | What it does |
|---|---|
| `--version X.Y.Z` | Installs that release, not the latest. `vX.Y.Z` works too. Releases before v0.5.13 have no `SHA256SUMS`, so the script refuses them. |
| `--with uv` or `--with pipx` | Installs with that tool. Without it, the script uses the tool that already has Contexto, else uv if it is on `PATH`, else pipx. |
| `--attest` | Also requires `gh attestation verify` to pass for the wheel. Needs the GitHub CLI and `gh auth login`. |
| `--force` | Reinstalls when that release is already installed, and lets uv or pipx overwrite a `contexto` command they do not own. |
| `--dry-run` | Prints what would be downloaded and run. Nothing is installed or removed, and the wheel is not downloaded. It does read the release's `SHA256SUMS` to learn the wheel's name. |
| `--help` | Prints the options. |

| Environment variable | What it does |
|---|---|
| `CONTEXTO_VERSION` | Same as `--version`. The option wins. |
| `CONTEXTO_INSTALL_BASE_URL` | Where the releases are, for a mirror or a test. Default `https://github.com/VentureForgre/contexto`. It must be https; plain http is accepted only for `127.0.0.1` and `localhost`. The script says so in its output when this is set. `--attest` still checks against `VentureForgre/contexto`. |

### Python

uv is told to use Python 3.12 or later (`--python '>=3.12'`), and fetches one if it has to. For pipx
the script checks pipx's default Python. If that is older than 3.12, it passes `--python` with the
newest of `python3.15` to `python3.12` on `PATH`, and stops with a message when there is none.

### Exit status

0 when Contexto is installed or already up to date, and after a dry run. 1 for a failed download,
check or install. 2 for an option or a value the script does not accept.

## Other ways to install

Each command below takes a release wheel. Set the version once:

```sh
v=0.5.24      # the newest is on https://github.com/VentureForgre/contexto/releases
wheel=contexto-$v-py3-none-any.whl          # after 0.5.24: contexto_proxy-$v-py3-none-any.whl
url=https://github.com/VentureForgre/contexto/releases/download/v$v/$wheel
```

### A release wheel by URL

```sh
uv tool install "$url"        # or: pipx install "$url"
contexto --version
```

`pip install "$url"` works too, inside an active Python 3.12+ virtual environment. None of these
checks the download; to check it first, see [Checking the download](#checking-the-download).

`pipx` installs with its default Python. If that is older than 3.12, add `--python python3.12`.

### Run without installing

```sh
uvx --from "$url" contexto --version
uvx --from "$url" contexto run claude
```

uv keeps the wheel in its cache and builds a throwaway environment for it.

### From PyPI

```sh
uv tool install contexto-proxy          # or: pipx install contexto-proxy
uvx --from contexto-proxy contexto --version
```

If [pypi.org/project/contexto-proxy](https://pypi.org/project/contexto-proxy/) does not exist yet,
no release has been published to PyPI; use the script or a release wheel. Releases under the name
`contexto` were never published there. Never install `contexto` from PyPI: that is the unrelated
library.

### Download with gh

The GitHub CLI downloads the wheel and its checksums together:

```sh
gh release download "v$v" --repo VentureForgre/contexto --pattern '*.whl' --pattern SHA256SUMS
shasum -a 256 -c SHA256SUMS --ignore-missing     # or: sha256sum -c SHA256SUMS --ignore-missing
uv tool install ./"$wheel"                       # or: pipx install ./"$wheel"
```

### From a checkout

`uv tool install .` in the repository root installs the checkout. It reports its version as
`0.0.0+checkout`, because only release builds carry a real version; include the commit
(`git rev-parse --short HEAD`) when you report a problem. `uv run contexto --help` runs the checkout
without installing it. [CONTRIBUTING.md](../CONTRIBUTING.md) has the development setup.

### From git

```sh
uv tool install "git+https://github.com/VentureForgre/contexto@v$v"      # a tag, a branch or a commit
pipx install "git+https://github.com/VentureForgre/contexto@v$v"
```

A git install builds from source, so it has the same limitation as a checkout: the package version
is the placeholder `0.0.0+checkout`, whatever tag you asked for. What `contexto --version` prints
depends on the code you installed:

- Code after 0.5.24 names the revision, on one line, with the commit in lowercase. It prints
  `contexto 0.0.0+checkout (git v0.6.0, commit 1a2b3c4d)` when the installer recorded the revision
  you asked for (here the tag `v0.6.0`), and `contexto 0.0.0+checkout (git commit 1a2b3c4d)` when
  it recorded only the commit, or when the revision you asked for is itself a commit.
- The tag v0.5.24 and anything earlier prints plain `contexto 0.0.0+checkout`.

`pip list`, `pipx list` and `uv tool list` show `0.0.0+checkout` in every case. When the version
matters, for example in a bug report or a pinned setup, install a release wheel.

## Upgrade

Contexto makes no network calls of its own and never checks for a new version. Nothing tells you
when a release is out. The [changelog](../CHANGELOG.md) and the
[Releases page](https://github.com/VentureForgre/contexto/releases) list what changed.

An upgrade never touches your session data in `~/.contexto` (or `$CONTEXTO_HOME`).

| Installed with | Upgrade with |
|---|---|
| The install script | Run the script again. It does nothing when you already have the latest release. |
| A release wheel, by URL or by file | Install the newer wheel the same way. With pipx add `--force`: without it pipx keeps the old one. |
| PyPI | `uv tool upgrade contexto-proxy` or `pipx upgrade contexto-proxy` |
| A checkout | `git pull`, then `uv tool install --force .` |
| git | Install again with `--force`, naming the newer tag or commit. |

`uv tool upgrade` and `pipx upgrade` do nothing useful for an install from a wheel file or a wheel
URL, which includes an install made by the script. Measured with uv 0.13.5 and pipx 1.18.4: after
the script, `uv tool upgrade` fails with "Distribution not found", because the wheel was in a
temporary directory; after a URL install it prints "Nothing to upgrade"; and `pipx upgrade` says no
package index was checked. Run the script again instead.

pipx then suggests `pipx install --force contexto`. Do not run that: it installs the unrelated
library from PyPI.

### From the earlier distribution name

If `uv tool list` or `pipx list` shows `contexto` and not `contexto-proxy`, you have a release from
before the name changed (0.5.24 or earlier).

The script handles the change. After the new wheel has passed its checks, it removes the `contexto`
install and then installs `contexto-proxy`. It prints the uninstall command it runs.

By hand, uninstall first, then install. Where PyPI has no release yet, give the wheel's URL in
place of `contexto-proxy`:

```sh
uv tool uninstall contexto && uv tool install contexto-proxy
pipx uninstall contexto && pipx install contexto-proxy
```

The order matters, because both distributions provide the `contexto` command. This is what uv 0.13.5
and pipx 1.18.4 do when it is not followed:

| You have | You run | What happens |
|---|---|---|
| `contexto` in uv | `uv tool install` of a `contexto-proxy` wheel | uv stops: "Executable already exists: contexto". Nothing changes. |
| `contexto` in pipx | `pipx install` of a `contexto-proxy` wheel | pipx installs it, exits 0, and leaves the `contexto` command on the old release. |
| `contexto` in pipx | `uv tool install` of either wheel | uv stops: "Executable already exists: contexto". |
| `contexto` in uv | `pipx install` of either wheel | pipx installs it, exits 0, and leaves the command on uv's install. |
| both names in uv, after `--force` | `uv tool uninstall contexto` | uv deletes the `contexto` command, though `contexto-proxy` owns it by then. |
| both names in pipx, after `--force` | `pipx uninstall contexto` | pipx removes the old install and leaves the command alone. |

Under one name, an upgrade replaces the install in place: `uv tool install` of a newer wheel does it
directly, and `pipx install` does it with `--force`.

pip in a virtual environment has the same trap. `pip install` of a `contexto-proxy` wheel next to
`contexto` succeeds and `pip list` shows both. `pip uninstall contexto` afterwards deletes the
package and the `contexto` command, while `contexto-proxy` stays listed and no longer works. So run
`pip uninstall contexto` first, then install.

## Checking the download

Contexto sees every prompt and every auth header your CLI sends, so check that the wheel came from
this repository's release workflow before you install it. Releases from v0.5.13 on carry a
`SHA256SUMS` asset and a GitHub build provenance attestation. Releases v0.5.0 to v0.5.12 have
neither.

**Checksums.** `SHA256SUMS` lists the SHA-256 of the wheel and the sdist:

```sh
gh release download "v$v" --repo VentureForgre/contexto --pattern '*.whl' --pattern SHA256SUMS
shasum -a 256 -c SHA256SUMS --ignore-missing     # or: sha256sum -c SHA256SUMS --ignore-missing
```

A match shows that the download is intact: the file you have is the file the release holds. It does
not show who built the file, because `SHA256SUMS` comes from the same release as the wheel. Someone
who could replace the wheel could replace the checksums too. The install script makes this check on
every run.

**Attestation.** The release workflow signs a statement that it built the wheel, and GitHub keeps
it. This shows that a workflow in this repository built the file:

```sh
gh attestation verify "$wheel" --repo VentureForgre/contexto     # needs gh auth login
```

The script makes this check with `--attest`. Without it, the script prints the command.

**Rebuild.** The build is reproducible: rebuilding the wheel from the release tag with the same uv
version gives the same bytes. [RELEASING.md](../RELEASING.md#verifying-a-release) has the commands.

For v0.5.12 and earlier, compare `shasum -a 256 contexto-<version>-py3-none-any.whl` with the digest
GitHub records for the asset:

```sh
gh release view "v$v" --repo VentureForgre/contexto --json assets --jq '.assets[] | .name + " " + .digest'
```

## Uninstall

```sh
uv tool uninstall contexto-proxy      # or: pipx uninstall contexto-proxy
uv tool uninstall contexto            # a release up to 0.5.24: the earlier name
pip uninstall contexto-proxy          # an install made with pip in a virtual environment
```

`uv tool list` and `pipx list` show which name you have, and which tool has it.

Your session data stays in `~/.contexto` (or `$CONTEXTO_HOME`). Delete that directory to remove
every stored conversation; see [What is stored and where](sessions.md#what-is-stored-and-where).

## Platforms

CI tests Python 3.12 to 3.15 on Linux, and 3.12 and 3.15 on macOS. Windows is not supported: the
store locks files with `fcntl`, the launcher forwards POSIX signals (`SIGHUP`), and the store relies
on POSIX file modes (0700 directories, 0600 files).

WSL is untested: nobody has run Contexto or its test suite there. Installers refuse the wheel on
Python 3.11 and earlier.

## Troubleshooting

These are install problems. Problems while Contexto runs are in
[troubleshooting.md](troubleshooting.md).

### The wrong `contexto` from PyPI

`pip install contexto`, `pipx install contexto` and `uvx contexto` install an unrelated library
that happens to have the name. This project's command prints `contexto` and a version for
`contexto --version`, and lists `run`, `proxy` and `sessions` in `contexto --help`.

Remove the other one the way it was installed (`pip uninstall contexto`, `pipx uninstall contexto`
or `uv tool uninstall contexto`), then run the install script.

### `contexto: command not found`

uv and pipx put commands in their own bin directory, usually `~/.local/bin`. The script prints the
directory and the `export PATH=...` line when that directory is not on `PATH`. To find it later:

```sh
uv tool dir --bin
pipx environment --value PIPX_BIN_DIR
```

Add that directory to `PATH` in your shell profile. `uv tool update-shell` and `pipx ensurepath` do
that edit for you. The script never edits a profile.

### pipx uses an old Python

pipx installs with the Python it was installed with, which can be older than 3.12. The install then
fails, because the wheel requires 3.12. Name a newer one:

```sh
pipx install --python python3.12 "$url"
```

The script does this for you when a `python3.12` to `python3.15` is on `PATH`. When none is, it
stops and says so. Install a newer Python, or install uv, which fetches one.

### Two installs, and the wrong one runs

Your shell runs the first `contexto` on `PATH`. To see which one that is and what else is there:

```sh
command -v contexto                  # the one that runs
ls -l "$(command -v contexto)"       # where it points: a uv tool, a pipx venv, or a virtual environment
type -a contexto                     # every contexto on PATH, in order (bash and zsh)
uv tool list
pipx list --short
```

Keep one. Uninstall the others, then run the install script with `--force` so the command is put
back if an uninstall removed it. An active virtual environment that has Contexto comes first on
`PATH`; deactivate it or uninstall Contexto there.

The script checks for this. If the `contexto` it finds after installing does not report the release
it installed, it stops with an error that names the path of the one that ran.

## Homebrew formula (draft, not published)

There is no Homebrew tap for Contexto. This is a draft formula for one. It has not been published,
and it has not been tested with `brew install`. Publishing it needs a new public repository,
`VentureForgre/homebrew-tap`, which is the repository owner's to create.

The formula builds a Python virtual environment from a release sdist. This one uses the v0.5.24
sdist, with its real URL and SHA-256:

```ruby
class Contexto < Formula
  include Language::Python::Virtualenv

  desc "Local proxy that keeps Claude Code and Codex sessions under a token bound"
  homepage "https://github.com/VentureForgre/contexto"
  url "https://github.com/VentureForgre/contexto/releases/download/v0.5.24/contexto-0.5.24.tar.gz"
  sha256 "d367cc8813dbd2d55479d58437ae5e3d2b5ee79a1ddfa89a86533401bf0958db"
  license "MIT"

  # The sdist's build backend is uv_build, which is a Rust program. Homebrew builds every
  # Python package from source, the build backend included.
  depends_on "rust" => :build
  depends_on "python@3.13"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "contexto #{version}", shell_output("#{bin}/contexto --version")
  end
end
```

Once the sdist is named `contexto_proxy`, two lines change: `url` ends in
`v<version>/contexto_proxy-<version>.tar.gz`, and `sha256` is that file's digest from the release's
`SHA256SUMS`. The class stays `Contexto` and the file stays `Formula/contexto.rb`, so the formula
and the command keep the name `contexto`. Homebrew takes the version from the file name in `url`;
if `brew info` shows the wrong one for the new name, add a `version` line.

What is known and not known about it:

- `ruby -c` accepts the file. `brew style` was not run: it would not start without downloading
  Ruby gems first.
- The build step is the open question. Homebrew runs pip with `--no-binary=:all:`, so pip has to
  build `uv_build` from its own sdist, and that needs Rust. It may need more `depends_on` lines or
  `resource` blocks than the draft has.
- Contexto has no runtime dependencies, so the formula needs no `resource` blocks for those.

To publish it:

1. The owner creates the public repository `VentureForgre/homebrew-tap`.
2. Add the formula as `Formula/contexto.rb`, with the `url` and `sha256` of the newest release.
3. On a Mac, run `brew tap VentureForgre/tap`, then `brew install --build-from-source contexto`,
   `brew test contexto` and `brew audit --strict --new contexto`. Fix what fails, starting with the
   build step above.
4. Push. Users then install with `brew install VentureForgre/tap/contexto`.
5. For every later release, update `url` and `sha256` and push. Nothing does this automatically.

Until then, on a Mac with Homebrew, `brew install uv` and the install script are the short way.
