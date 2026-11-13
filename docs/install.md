# Install

The [README](../README.md#install) has the requirements and the one command that installs Contexto
from the release URL. This page has the other ways to install, how to check the download, and how
to uninstall.

## Other ways to install

`pip install <the same URL>` works too, inside an active Python 3.12+ virtual environment. To check
the download before installing, follow [Checking the download](#checking-the-download) instead.

Or download it first with the GitHub CLI:

```sh
gh release download v0.5.24 --repo VentureForgre/contexto --pattern 'contexto-0.5.24-py3-none-any.whl'
uv tool install ./contexto-0.5.24-py3-none-any.whl   # or: pipx install ./contexto-0.5.24-py3-none-any.whl
```

`pipx` installs with its default Python. If that is older than 3.12, add `--python python3.12`.

## Checking the download

Contexto sees every prompt and every auth header your CLI sends, so check that the wheel came from
this repository's release workflow before you install it. Releases from v0.5.13 on carry a
`SHA256SUMS` asset and a GitHub build provenance attestation. Download the wheel, check it, then
install from the file:

```sh
gh release download v0.5.24 --repo VentureForgre/contexto \
  --pattern 'contexto-0.5.24-py3-none-any.whl' --pattern SHA256SUMS
shasum -a 256 -c SHA256SUMS --ignore-missing          # Linux: sha256sum -c SHA256SUMS --ignore-missing
gh attestation verify contexto-0.5.24-py3-none-any.whl --repo VentureForgre/contexto   # needs gh auth login
pipx install ./contexto-0.5.24-py3-none-any.whl
```

For v0.5.12 and earlier, compare `shasum -a 256 contexto-<version>-py3-none-any.whl` with the digest
GitHub records for the asset:
`gh release view v<version> --repo VentureForgre/contexto --json assets --jq '.assets[] | .name + " " + .digest'`.

The build is reproducible: rebuilding the wheel from the release tag with the same uv version gives
the same bytes. [RELEASING.md](../RELEASING.md#verifying-a-release) has the commands.

## From a checkout

`uv tool install .` in the repository root installs the checkout. It reports its version as
`0.0.0+checkout`, because only release builds carry a real version; include the commit
(`git rev-parse --short HEAD`) when you report a problem.

## Uninstall

`pipx uninstall contexto`, `uv tool uninstall contexto`, or `pip uninstall contexto`, matching how
you installed it. Your session data stays in `~/.contexto` (or `$CONTEXTO_HOME`). Delete that
directory to remove every stored conversation; see [What is stored and where](sessions.md#what-is-stored-and-where).

## Platforms

CI tests Python 3.12 to 3.15 on Linux, and 3.12 and 3.15 on macOS. Windows is not supported: the
store locks files with `fcntl`, the launcher forwards POSIX signals (`SIGHUP`), and the store relies
on POSIX file modes (0700 directories, 0600 files).
