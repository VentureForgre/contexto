# Contexto

Contexto is a local proxy that keeps Claude Code and Codex sessions from compacting. It runs on your
machine between the CLI and the model API and keeps every request under a fixed token bound. When a
conversation gets close to the bound, it moves the oldest turns out of the prompt into a store on
disk, word for word, and gives the model MCP tools to search and read them back.

It is not a model, an API client or a hosted service. It makes no model calls of its own: it reads
and rewrites the requests your CLI already makes. It does not summarize anything.

## The name

This distribution is `contexto-proxy`. The command it installs is `contexto`, and the Python package
is `contexto`. The PyPI project named `contexto` is unrelated: `pip install contexto` installs
something else.

## Install

```sh
uv tool install contexto-proxy
pipx install contexto-proxy
uvx --from contexto-proxy contexto --version
```

Use one of the first two; the third runs it once without installing. `pipx` installs with its
default Python: if that is older than 3.12, add `--python python3.12`.

Earlier releases were installed from GitHub under the name `contexto`. Both provide the `contexto`
command, so uninstall that one first (`pipx uninstall contexto` or `uv tool uninstall contexto`).

## Use

```sh
contexto run claude
contexto run codex
```

Contexto options go before the CLI's name, and everything after it is passed to the CLI unchanged:
`contexto run --bound 120000 claude --model claude-haiku-5-5`. Codex needs a command that prints its
API key, for example `contexto run --codex-key-helper 'my-key-command' codex`. The
[README](https://github.com/VentureForgre/contexto/blob/main/README.md) covers the rest: how it
works, the Codex setup, every option, inspecting sessions and troubleshooting.

## Requirements

- Python 3.12 or later.
- macOS or Linux. Windows is not supported.
- `claude` (Claude Code) or `codex` (Codex CLI) on `PATH`.
- No runtime dependencies.

## Check what you install

Contexto sees every prompt and every auth header your CLI sends. Check that what you install was
built by this project's release workflow. Every version on PyPI is also a GitHub release, which
carries the same sdist and wheel, a `SHA256SUMS` file and a build provenance attestation, and the
build is reproducible from the release tag. The commands are in
[Verifying a release](https://github.com/VentureForgre/contexto/blob/main/RELEASING.md#verifying-a-release).

Session data stays on your machine, under `~/.contexto`.

## Links

- [README](https://github.com/VentureForgre/contexto/blob/main/README.md)
- [Changelog](https://github.com/VentureForgre/contexto/blob/main/CHANGELOG.md)
- [Security policy](https://github.com/VentureForgre/contexto/blob/main/SECURITY.md)
- [License](https://github.com/VentureForgre/contexto/blob/main/LICENSE) (MIT)
