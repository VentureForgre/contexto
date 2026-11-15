# `contexto doctor`

`contexto doctor` checks what `contexto run` needs from the directory you run it in, and prints one
line per check. Run it in the project where you would run `contexto run claude` or
`contexto run codex`, because the upstream, the key helper and the cloud-provider switches can come
from that project's `.claude/` settings files.

```sh
contexto doctor          # one line per check
contexto doctor --json   # the same results as JSON
```

```
contexto doctor (contexto 0.5.24)

[ok]   Python 3.13.2 (3.12 or later)
[ok]   Claude Code 2.1.318 at /usr/local/bin/claude (a tested version)
[warn] Codex: 'codex' not found on PATH. Install Codex: curl -fsSL https://chatgpt.com/codex/install.sh | sh (other ways: https://learn.chatgpt.com/docs/codex/cli). If it is installed, add its directory to PATH.
       -> Only `contexto run codex` needs it
[ok]   Claude Code settings: read ~/.claude/settings.json
[ok]   Claude Code upstream: http://127.0.0.1:8317 (from ANTHROPIC_BASE_URL in the env block of ~/.claude/settings.json)
[skip] Codex upstream: not checked, codex is not on PATH or cannot be run
[ok]   Claude Code key: apiKeyHelper in ~/.claude/settings.json (not run); no value is read or shown
[skip] Codex key helper: not checked, codex is not on PATH or cannot be run
[ok]   Store home /home/me/.contexto is writable
[ok]   MCP server started and listed its 5 tools
[ok]   Cloud provider: no switch (CLAUDE_CODE_USE_BEDROCK and the like) is on, so Claude Code sends its requests through the proxy

Ready: 8 passed, 1 warning, 2 skipped.
```

Each line starts with `[ok]`, `[warn]`, `[FAIL]` or `[skip]`, and is printed as soon as its check is
done. A warning or a failure is followed by one line, after `->`, that says what to do. The last
line counts the results. A character that does not print (a control character in a host name or a
path, say) is shown as a backslash escape such as `\x08`.

## Before you paste the output into an issue

Doctor's output holds no key, no token, no helper command and no helper output, in the text form
and in `--json`. It does hold things you may not want public, so look it over first:

- paths under your home directory: where `claude` and `codex` are, the store home, the Python
  interpreter (in the `mcp` check's next step when that check fails, and in `details.executable` of
  `--json`);
- the scheme, host and port of your upstream, which may be an internal host name.

## Exit code

| Code | Meaning |
|---|---|
| 0 | No check failed. There may be warnings. |
| 1 | At least one check failed, or the reader of doctor's output left before the end (`contexto doctor \| head -1`). |
| 130 | Ctrl+C. |
| 143, 129 | SIGTERM, SIGHUP (128 plus the signal's number). |

When doctor is stopped by Ctrl+C, SIGTERM or SIGHUP (a closed terminal), it kills the MCP server it
started and removes its temporary home first. SIGKILL cannot be caught: the temporary home
(`contexto-doctor-*` in the temporary directory) then stays, and holds nothing of yours.

A check fails when `contexto run` could not work at all:

- Python is older than 3.12.
- Neither `claude` nor `codex` can be run. One of the two is enough.
- An upstream is not an `http://` or `https://` URL. `contexto run` refuses to start with it.
- The store home is one `contexto run` cannot use (see [`store`](#store)).
- The MCP server does not start, or does not list exactly Contexto's five tools.
- A cloud-provider switch is on, or a settings file that may turn one on cannot be read:
  `contexto run claude` refuses to launch (exit 2; see [`provider`](#provider)).
- A check cannot be made at all: the working directory was deleted, there is no home directory, or
  no temporary directory can be created. The line names which, and what to set.

Everything else is a warning, because `contexto run` still starts, or a flag can supply what is
missing:

- a CLI version Contexto was not tested with, or a version that could not be read;
- one of the two CLIs missing;
- a settings file that is there and cannot be used;
- a store home that cannot be written while its `sessions` directory can be used;
- no key found for Claude Code (a login Claude Code holds also works, and doctor does not look for
  one);
- no upstream or no key helper for Codex (`--codex-upstream` and `--codex-key-helper` can supply
  them);
- a project setting skipped because the folder is not trusted.

When a CLI is missing, the checks that only matter for that CLI are skipped, not warned about.
`provider` is skipped only when `claude` is not on PATH or not executable (a broken symlink or a
directory counts as not executable), because `contexto run claude` stops there before its refusal.
A `claude` that is executable but cannot start (a missing `#!` interpreter, say) still gets the
check: the launcher reaches its refusal with it.

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
missing `#!` interpreter) counts as missing, and the line says which. When `codex` is not on `PATH`
at all, the line gives Codex's install command (Claude Code's for `claude`), and the next step only
says which command needs it.

A `codex` that starts while this build of Contexto lacks its Codex support (the `contexto.codex`
module) is a warning, `…, but codex support is not installed in this build`, with the step to
reinstall Contexto: `contexto run codex` says the same and exits 2. Codex then counts as missing,
and its two checks are skipped with that reason.

### `settings`

The Claude Code settings files `contexto run claude` reads from this directory: the main checkout's
`.claude/settings.local.json`, this directory's `.claude/settings.local.json` and
`.claude/settings.json`, `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`), and
Claude Code's managed settings file (`/Library/Application Support/ClaudeCode/managed-settings.json`
on macOS, `/etc/claude-code/managed-settings.json` on Linux), which only the
[`provider`](#provider) check and the key check read. The line names the ones that were read. Each
file is read as `contexto run claude` reads it, by the same reader, and the checks below use what
it read.

A file that is there and cannot be used is a warning that names it and says what is wrong:

- it is not a regular file (a named pipe, a socket or a device, which is never opened for reading);
- it is larger than 4 MiB, or it cannot be read;
- it is not valid JSON: a syntax error, a wide encoding Claude Code does not load (UTF-16BE,
  UTF-16LE without its byte order mark, UTF-32), nested too deep, or holding `NaN` or `Infinity`,
  which Claude Code 2.1.322 rejects;
- it has an `env` that is not a JSON object: Claude Code 2.1.322 loads nothing from such a file, not
  even its `apiKeyHelper`.

A UTF-8 file with a byte that is not UTF-8 is read with that byte as U+FFFD, and a UTF-16LE file
with its byte order mark is read even when it holds a lone surrogate or ends in one odd byte, as
Claude Code 2.1.322 reads them (D70).

Contexto takes nothing from such a file, so without this line a broken file that sets your upstream
would show only as "nothing sets ANTHROPIC_BASE_URL". It can also stop the launch: `contexto run
claude` refuses while a file it could not read may turn on a cloud provider (see
[`provider`](#provider)). A file with an `env` that is not an object, and one that was read whole and
cannot name a switch, do not stop it.

Read like any other file, as Claude Code reads it: a file that starts with a UTF-8 byte order mark,
and one holding an integer longer than Python's 4,300-digit limit (JavaScript reads it as Infinity).
A directory and a file you cannot open count as absent: Claude Code runs as you and cannot read them
either. A JSON document that is not an object sets nothing.

### `claude-upstream` and `codex-upstream`

The upstream `contexto run` would use from this directory with no `--upstream` or
`--codex-upstream`, and what set it. The host is shown in ASCII, as it goes on the wire: a name with
letters from another alphabet appears in its `xn--` form, not as the name it may imitate.

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

The base URL is not read from organization-managed Claude Code settings, here or by `contexto run`.

`claude-upstream` fails when the URL is not one `contexto run claude` can use: not http(s), malformed,
or holding a lone surrogate, which a settings file can give it (a `\ud800` escape, or a malformed
UTF-16LE file). The line names where it came from and does not repeat the URL.

`claude-upstream` fails when Claude Code's upstream is this Contexto home's `contexto serve`, the base
URL [serve.md](serve.md) has a client put in its settings: `contexto run claude` refuses it, because
requests would pass two engines that open the same store. Pass `--upstream` to `contexto run`, or
use the client through the service. The check shows only the origin, never the URL's path, which
holds the service's token.

### `claude-key` and `codex-key`

Whether a key or key helper is configured, by name and place.

Claude Code sends its own credential through the proxy; Contexto never reads or stores it. Doctor
looks for `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN` in the
environment and in the `env` blocks of the settings files above (the managed file's first), and for
an `apiKeyHelper` in the files other than the managed one. Finding none is a warning, not a failure: Claude Code may hold a login, which doctor does
not look for.

Codex gets its key from a helper command that `contexto run codex` hands to it:
`CONTEXTO_CODEX_KEY_HELPER`, else Claude Code's `apiKeyHelper`, a project's only in a trusted
folder. With neither, `contexto run codex` needs `--codex-key-helper`. A
`CONTEXTO_CODEX_KEY_HELPER` that is set and holds only spaces is a warning: `contexto run codex`
takes it as the helper and refuses to start ("the key helper command is empty").

### `store`

The store home (`$CONTEXTO_HOME`, else `~/.contexto`) is one `contexto run` can use. The check
follows what the launcher does, and changes nothing:

- A home that does not exist yet can be created. The check reads permissions only, so a read-only
  or full file system shows up at `contexto run`, not here.
- A home that exists is a directory, and sessions can be made in it. They go in its `sessions`
  directory. `contexto run` sets that directory to mode 0700 itself, but only after it has looked in
  it. So a `sessions` that you own and can read and enter (0500) passes, and the line says so; one
  you cannot read fails, with the `chmod` to run. One you can read but not enter (0400, 0600) passes
  on Python 3.14 and later, where `contexto run` gets past that first look, and fails on 3.12 and
  3.13, where it does not: doctor answers for the Python it runs on. A `sessions` that belongs to
  another user fails. Without a `sessions` directory, the home must be writable.
- A home that cannot be written, with a `sessions` directory that can be used, is a warning:
  `contexto run` starts, and cannot create its lock file and salt file in the home.
- A home whose path contains `${` fails when `claude` is installed: `contexto run claude` refuses
  it, because Claude Code would expand it in the MCP config. `contexto run codex` accepts it.

### `mcp`

Doctor starts the MCP server the way `contexto run` has the CLI start it (this Python by its
absolute path, `-P -m contexto mcp --session=<id>`, in the current directory), sends `initialize`
and `tools/list`, and compares the tool names with Contexto's five: `contexto_recall`,
`contexto_pin`, `contexto_close`, `contexto_state` and `contexto_search`. The whole exchange has
10 seconds; a server still running after that is killed, with any process it started in its process
group.

A failure here usually means Contexto is not installed in the Python that runs it. `-P` keeps the
current directory off the import path, so running from a source directory is not enough. Codex
starts MCP servers with a restricted environment, which this check does not imitate: a Contexto
that is importable only through `PYTHONPATH` passes here and fails under Codex.

### `provider`

`contexto run claude` routes Claude Code through the proxy by setting `ANTHROPIC_BASE_URL`. With one
of these switches on, Claude Code sends every request to that provider's own endpoint and none to
`ANTHROPIC_BASE_URL`, so the proxy would see nothing and the session would run with no bound:

| Variable | Provider |
|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | Amazon Bedrock |
| `CLAUDE_CODE_USE_VERTEX` | Google Vertex AI |
| `CLAUDE_CODE_USE_FOUNDRY` | Microsoft Foundry |
| `CLAUDE_CODE_USE_MANTLE` | Amazon Bedrock (Mantle) |
| `CLAUDE_CODE_USE_ANTHROPIC_AWS` | Claude Platform on AWS |
| `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD` | Claude Platform on Google Cloud |

`contexto run claude` refuses such a launch with exit 2, before it opens a session. That refusal is
D70's: the rule, the places it looks and why it errs toward refusing are in the
[`contexto run` reference](run-reference.md#environment-variables) and the
[contract](milestones/M2-contexto-proxy.md#architecture) ("Cloud providers (D70)"). This check is
the launcher's own, made with the same function and the same arguments, so doctor fails exactly
when the launch would be refused. Its line is what the launcher prints first, and its next step is
the rest of the launcher's message, word for word:

```
[FAIL] CLAUDE_CODE_USE_BEDROCK is set in the process environment. With it on, Claude Code sends its requests to Amazon Bedrock and none to ANTHROPIC_BASE_URL: `contexto run claude` refuses to launch (exit 2)
       -> Contexto's proxy would see no request and the session would run with no bound, so the launch is refused. Contexto refuses when a switch is on in any of these places, whichever of them Claude Code would apply. To run through Contexto: unset CLAUDE_CODE_USE_BEDROCK for this command (env -u CLAUDE_CODE_USE_BEDROCK contexto run ...); and give Contexto an upstream that speaks the Anthropic Messages API (--upstream or ANTHROPIC_BASE_URL). To keep using the provider, run claude without contexto.
```

`env -u` unsets the switch for that one command only, so a plain `contexto doctor` afterwards still
fails this check; run `env -u CLAUDE_CODE_USE_BEDROCK contexto doctor` to see what the launch will see.

In short: a switch counts when it is `1`, `true`, `yes` or `on` (any case; a JSON `true` or `1`
too) in the environment or in the `env` block of any of the settings files under
[`settings`](#settings), whichever of them Claude Code would apply, and with
`CLAUDE_CODE_IGNORE_PERSISTED_TRUST` set or not. A settings file that is there, could not be read
and may turn a switch on fails the check too, and the line names the file and what is wrong with it.

Where the sixth switch comes from: `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD` is in the Claude Code
2.1.318 and 2.1.322 programs, with the provider name above, and on no documentation page. On 2.1.318,
in `-p` mode with no model call ([spikes/m2/providers](../spikes/m2/providers/README.md)), at `1` in
the environment or in a project settings file's `env` block, it sent every model request to its own
endpoint and none to the proxy, as the other five did there. D70 measured the other five on 2.1.322.

`CLAUDE_CODE_USE_GATEWAY` is not a switch here. Claude Code 2.1.322 has that variable, and it needs
`ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`. Measured on 2.1.322 in `-p` mode with both set, the
variable in the environment and then in a project settings file: every model request went through
the proxy.

## What doctor does not check

- Whether the upstream is reachable, the key is valid, or a key helper works: it makes no network
  request and runs no helper.
- A login that Claude Code or Codex holds.
- `--upstream`, `--codex-upstream`, `--codex-key-helper` and `--codex-auth chatgpt`: it reports what
  `contexto run` does without them.
- Organization-managed Claude Code settings other than the managed settings file (a
  device-management profile, a server), and the base URL in that file.
- The type of every setting. Claude Code loads nothing from a settings file in which any setting has
  the wrong type. Contexto checks the `env` block's type only, so a file with another wrong-typed
  setting (for example `permissions` as a string) is read by doctor and `contexto run` though Claude
  Code ignores it: an upstream or key helper set there is reported, and used, when Claude Code would
  not use it, and a switch set there refuses the launch. With only an `apiKeyHelper` there, doctor
  says `Ready` and Claude Code then says "Not logged in". Claude Code names such a file when it
  starts ("These settings files did not load"), which tells you which file to fix.

## JSON

`contexto doctor --json` prints one object when every check is done, and exits with the same code
as the text form. Stopped before that (Ctrl+C, SIGTERM, SIGHUP), it prints nothing on stdout.

```json
{
  "schema": 1,
  "contexto": "0.5.24",
  "ok": true,
  "summary": {"pass": 8, "warn": 1, "fail": 0, "skip": 2},
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
- `checks` always holds the eleven checks in this order: `python`, `claude`, `codex`, `settings`,
  `claude-upstream`, `codex-upstream`, `claude-key`, `codex-key`, `store`, `mcp`, `provider`.
- `status` is `pass`, `warn`, `fail` or `skip`.
- `summary` is the line the text form prints. Its wording is for people and may change; read
  `status` and `details` in scripts.
- `action` is the "what to do" line, a string for `warn` and `fail` and `null` otherwise.

`details` by check. A skipped check has `{}`. A check that itself broke has `{"error": <type>}`,
the exception's type. A check that could not be made has `{"error": <fault>}`, one of
`no_working_directory`, `no_home_directory`, `no_store_home` and `no_temp_directory`.

| Check | `details` |
|---|---|
| `python` | `version`, `executable` |
| `claude`, `codex` | `cli`, `path` (or `null`), `version` (or `null`), `tested` (`true`, `false`, or `null` when the version is unknown), `tested_versions`; `error` when the CLI is missing (`not_found`), its version could not be read (`timeout`, `exit_<code>`, `unparsed`, or an error type), or, for `codex`, Contexto's Codex support is not installed (`codex_support_missing`) |
| `settings` | `files`: one `{"kind", "state"}` for each file looked for, highest precedence first and the managed file last, where `kind` is `main-checkout-local`, `local`, `project`, `user` or `managed` and `state` is `read`, `absent` or `unusable`; an unusable file also has `problem`, what is wrong with it (the wording of the line). Never a path. |
| `claude-upstream` | `origin` (`scheme://host[:port]`, or `null` when the URL is invalid), `source` (`default`, `env`, or `settings:<kind>` with kind `user`, `project`, `local` or `main-checkout-local`); `ignored` (the project file kinds skipped) when a base URL was skipped |
| `codex-upstream` | `origin` and `source` (`codex-env` or `claude-<source>+/v1`), both `null` when there is none; `ignored` as above |
| `claude-key` | `found`: a list of `{"name", "source"}`, where `name` is a variable or `apiKeyHelper` and `source` is `env` or `settings:<kind>` (`managed` included). Never a value. |
| `codex-key` | `name` (`CONTEXTO_CODEX_KEY_HELPER`, `apiKeyHelper`, or `null`), `source` (`env`, `settings:<kind>`, or `null`); `ignored` when a project's helper was skipped as untrusted; `error` (`blank`) when `CONTEXTO_CODEX_KEY_HELPER` holds only spaces |
| `store` | `path`, `exists` |
| `mcp` | `tools` (the names listed, sorted), `missing`, `unexpected`, `seconds` (how long the check took) |
| `provider` | `variables`: a list of `{"variable", "provider", "sources"}` for each switch that is on, where `sources` lists every place it is on (`env` or `settings:<kind>`); `unread`: a list of `{"source", "problem"}` for each settings file that could not be read and may turn a switch on. Both empty when the check passes. |
