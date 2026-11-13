# `contexto doctor`

`contexto doctor` checks what `contexto run` needs from the directory you run it in, and prints one
line per check. Run it in the project where you would run `contexto run claude` or
`contexto run codex`, because the upstream, the key helper and the provider variables can come from
that project's `.claude/` settings files.

```sh
contexto doctor          # one line per check
contexto doctor --json   # the same results as JSON
```

```
contexto doctor (contexto 0.5.24)

[ok]   Python 3.13.2 (3.12 or later)
[ok]   Claude Code 2.1.318 at /usr/local/bin/claude (a tested version)
[warn] Codex: 'codex' not found on PATH
       -> Install Codex and put it on PATH to use `contexto run codex`
[ok]   Claude Code upstream: http://127.0.0.1:8317 (from ANTHROPIC_BASE_URL in the env block of ~/.claude/settings.json)
[skip] Codex upstream: not checked, codex is not on PATH or cannot be run
[ok]   Claude Code key: apiKeyHelper in ~/.claude/settings.json (not run); no value is read or shown
[skip] Codex key helper: not checked, codex is not on PATH or cannot be run
[ok]   Store home /home/me/.contexto is writable
[ok]   MCP server started and listed its 5 tools
[ok]   No provider variable (CLAUDE_CODE_USE_BEDROCK and the like) sends Claude Code's requests past the proxy

Ready: 7 passed, 1 warning, 2 skipped.
```

Each line starts with `[ok]`, `[warn]`, `[FAIL]` or `[skip]`. A warning or a failure is followed by
one line, after `->`, that says what to do. The last line counts the results.

## Exit code

| Code | Meaning |
|---|---|
| 0 | No check failed. There may be warnings. |
| 1 | At least one check failed. |
| 130 | Ctrl+C. |

A check fails when `contexto run` could not work at all:

- Python is older than 3.12.
- Neither `claude` nor `codex` can be run. One of the two is enough.
- An upstream is not an `http://` or `https://` URL. `contexto run` refuses to start with it.
- The store home cannot be written, or cannot be created.
- The MCP server does not start, or does not list exactly Contexto's five tools.

Everything else is a warning, because `contexto run` still starts, or a flag can supply what is
missing:

- a CLI version Contexto was not tested with, or a version that could not be read;
- one of the two CLIs missing;
- no key found for Claude Code (a login Claude Code holds also works, and doctor does not look for
  one);
- no upstream or no key helper for Codex (`--codex-upstream` and `--codex-key-helper` can supply
  them);
- a project setting skipped because the folder is not trusted;
- a variable that sends Claude Code's requests to another provider.

When a CLI is missing, the checks that only matter for that CLI are skipped, not warned about.

## What doctor never does

- **No network request.** Doctor reports the upstream Contexto would use and does not contact it,
  so it cannot tell you whether the upstream is reachable or the key is valid. The only processes it
  starts are `claude --version` and `codex --version` (as `contexto run` does) and Contexto's own
  MCP server, which it talks to over stdin and stdout. What `claude --version` and `codex --version`
  do is up to those CLIs.
- **No key helper is run.** A helper is your command and may have side effects, and for Codex a
  project's `apiKeyHelper` counts only in a folder Claude Code has recorded as trusted. Doctor says
  where a helper is configured and that it was not run, so it cannot tell you whether the helper
  works.
- **No key is printed**, nor any part of one, nor its length, nor a helper's command (a command can
  hold a key), nor a helper's output. Doctor reads only whether a key variable has a value.
- **No URL beyond its origin.** An upstream is shown as scheme, host and port. User info, the path
  and the query are left out, because each can hold a credential.
- **Nothing is left in the store home.** An existing home is tested by creating a file that has no
  name and is gone when doctor closes it. A home that does not exist is not created: doctor checks
  that its nearest existing parent directory is writable. No directory's mode is changed. The MCP
  check uses a temporary home that doctor deletes, so your store gets no session from it.

## The checks

The `id` is the check's name in `--json`.

### `python`

This Python is 3.12 or later. It is the interpreter that runs `contexto`, and the one the CLIs
start the MCP server with.

### `claude` and `codex`

Whether the CLI is on `PATH`, its version from `<cli> --version`, and whether that version is one
Contexto was tested with (`TESTED_VERSIONS` in `src/contexto/cliversions.py`; see
[cli-updates.md](cli-updates.md)). An untested version is a warning, as it is in `contexto run`.
A file on `PATH` that is not executable, is a broken symlink, or cannot be started (for example a
missing `#!` interpreter) counts as missing, and the line says which.

### `claude-upstream` and `codex-upstream`

The upstream `contexto run` would use from this directory with no `--upstream` or
`--codex-upstream`, and what set it.

For Claude Code, first match wins: `env.ANTHROPIC_BASE_URL` in a Claude Code settings file (the
main checkout's `.claude/settings.local.json`, then this directory's `.claude/settings.local.json`
and `.claude/settings.json`, then `~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json`
when that variable is set), then `ANTHROPIC_BASE_URL` in the environment, then
`https://api.anthropic.com`.

For Codex in API-key mode (the default): `CONTEXTO_CODEX_UPSTREAM`, else Claude Code's upstream
plus `/v1`. A project file's base URL counts for Codex only in a folder Claude Code has recorded as
trusted. If Claude Code's upstream is Anthropic's API, Codex has no upstream and the check warns:
pass `--codex-upstream`. Doctor does not check `--codex-auth chatgpt`, which uses the ChatGPT
backend and Codex's own login.

Organization-managed Claude Code settings are not read, here or by `contexto run`.

### `claude-key` and `codex-key`

Whether a key or key helper is configured, by name and place.

Claude Code sends its own credential through the proxy; Contexto never reads or stores it. Doctor
looks for `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN` in the
environment and in the `env` blocks of the settings files above, and for an `apiKeyHelper` in those
files. Finding none is a warning, not a failure: Claude Code may hold a login, which doctor does
not look for.

Codex gets its key from a helper command that `contexto run codex` hands to it:
`CONTEXTO_CODEX_KEY_HELPER`, else Claude Code's `apiKeyHelper`, a project's only in a trusted
folder. With neither, `contexto run codex` needs `--codex-key-helper`.

### `store`

The store home (`$CONTEXTO_HOME`, else `~/.contexto`) and, when it exists, its `sessions`
directory can be written; or, when the home does not exist yet, it can be created. For a home that
does not exist the check reads permissions only, so a read-only or full file system shows up at
`contexto run`, not here.

### `mcp`

Doctor starts the MCP server the way `contexto run` has the CLI start it (this Python by its
absolute path, `-P -m contexto mcp --session=<id>`, in the current directory), sends `initialize`
and `tools/list`, and compares the tool names with Contexto's five: `contexto_recall`,
`contexto_pin`, `contexto_close`, `contexto_state` and `contexto_search`. The whole exchange has
10 seconds; a server still running after that is killed.

A failure here usually means Contexto is not installed in the Python that runs it. `-P` keeps the
current directory off the import path, so running from a source directory is not enough. Codex
starts MCP servers with a restricted environment, which this check does not imitate: a Contexto
that is importable only through `PYTHONPATH` passes here and fails under Codex.

### `provider`

`contexto run claude` routes Claude Code through the proxy by setting `ANTHROPIC_BASE_URL`. Claude
Code uses that variable only for Anthropic's API. When one of these variables is on, Claude Code
sends its model requests to that provider instead, the proxy sees none of them, and the session is
not kept under the bound:

| Variable | Provider |
|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | Amazon Bedrock |
| `CLAUDE_CODE_USE_FOUNDRY` | Microsoft Foundry |
| `CLAUDE_CODE_USE_ANTHROPIC_AWS` | Claude Platform on AWS |
| `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD` | Claude Platform on Google Cloud |
| `CLAUDE_CODE_USE_MANTLE` | Amazon Bedrock (Mantle) |
| `CLAUDE_CODE_USE_VERTEX` | Google Vertex AI |

A variable is on when its value is `1`, `true`, `yes` or `on`, in any letter case. An empty value,
`0`, `false` and any other text leave it off. When several are on, Claude Code uses the first in
the table's order.

Doctor looks in the environment and in the `env` blocks of the settings files listed under
`claude-upstream`. The highest file that sets a variable decides it, and the environment decides
only when no file sets it, so `"CLAUDE_CODE_USE_BEDROCK": "0"` in a settings file turns off a value
exported in your shell. The line names each variable that is on and where it is set.

`contexto run claude` prints the same warning on stderr before Claude Code starts, records a
`provider_bypass` event in the session log, and still launches:

```
contexto: warning: CLAUDE_CODE_USE_BEDROCK is set in the environment, so Claude Code sends model requests to Amazon Bedrock and not through Contexto: the proxy sees none of them and this session is not kept under the bound. Unset CLAUDE_CODE_USE_BEDROCK to send requests through Contexto, or run claude without Contexto to use Amazon Bedrock.
```

Where this comes from: the variable list, its order and the on/off rule were read from the Claude
Code 2.1.318 and 2.1.322 programs on 2026-11-12. Claude Code's documentation (the env-vars,
amazon-bedrock and llm-gateway pages on code.claude.com) names five of the six;
`CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD` is in both programs and not on those pages. The bypass
itself was measured on Claude Code 2.1.318 in `-p` mode with no model call
([spikes/m2/providers](../spikes/m2/providers/README.md)): with each of the six variables on, in
the environment or in a project settings file's `env` block, no model request reached the proxy.
Not measured: interactive mode, two variables on at once, and other Claude Code versions. Not
looked for: organization-managed settings, and a provider that Claude Code selects from a login it
holds rather than from a variable.

## JSON

`contexto doctor --json` prints one object and exits with the same code as the text form.

```json
{
  "schema": 1,
  "contexto": "0.5.24",
  "ok": true,
  "summary": {"pass": 7, "warn": 1, "fail": 0, "skip": 2},
  "checks": [
    {
      "id": "claude",
      "status": "pass",
      "summary": "Claude Code 2.1.318 at /usr/local/bin/claude (a tested version)",
      "action": null,
      "details": {
        "cli": "claude",
        "path": "/usr/local/bin/claude",
        "version": "2.1.318",
        "tested": true,
        "tested_versions": ["2.1.302", "2.1.310", "2.1.314", "2.1.318"]
      }
    }
  ]
}
```

- `schema` is `1`. It changes only if a field below is removed or changes meaning; new fields may
  be added without changing it.
- `ok` is `true` when no check failed, which is when the exit code is 0.
- `checks` always holds the ten checks in this order: `python`, `claude`, `codex`,
  `claude-upstream`, `codex-upstream`, `claude-key`, `codex-key`, `store`, `mcp`, `provider`.
- `status` is `pass`, `warn`, `fail` or `skip`.
- `summary` is the line the text form prints. Its wording is for people and may change; read
  `status` and `details` in scripts.
- `action` is the "what to do" line, a string for `warn` and `fail` and `null` otherwise.

`details` by check (a skipped check has `{}`; a check that itself broke has `{"error": <type>}`):

| Check | `details` |
|---|---|
| `python` | `version`, `executable` |
| `claude`, `codex` | `cli`, `path` (or `null`), `version` (or `null`), `tested` (`true`, `false`, or `null` when the version is unknown), `tested_versions`; `error` when the CLI is missing (`not_found`) or its version could not be read (`timeout`, `exit_<code>`, `unparsed`, or an error type) |
| `claude-upstream` | `origin` (`scheme://host[:port]`, or `null` when the URL is invalid), `source` (`default`, `env`, or `settings:<kind>` with kind `user`, `project`, `local` or `main-checkout-local`); `ignored` (the project file kinds skipped) when a base URL was skipped |
| `codex-upstream` | `origin` and `source` (`codex-env` or `claude-<source>+/v1`), both `null` when there is none; `ignored` as above |
| `claude-key` | `found`: a list of `{"name", "source"}`, where `name` is a variable or `apiKeyHelper` and `source` is `env` or `settings:<kind>`. Never a value. |
| `codex-key` | `name` (`CONTEXTO_CODEX_KEY_HELPER`, `apiKeyHelper`, or `null`), `source` (`env`, `settings:<kind>`, or `null`); `ignored` when a project's helper was skipped as untrusted |
| `store` | `path`, `exists` |
| `mcp` | `tools` (the names listed, sorted), `missing`, `unexpected`, `seconds` (how long the check took) |
| `provider` | `variables`: a list of `{"variable", "source"}` for each one that is on; `provider` (the one Claude Code would use) when the list is not empty |
