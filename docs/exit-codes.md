# Exit codes

[Contexto](../README.md) / [Documentation](README.md)

The exit codes of the `contexto` commands. The Command column names the subcommand, so `run` is
`contexto run`.

| Command | Code | Meaning |
|---|---|---|
| `run` | the CLI's | Claude Code's or Codex's own exit code |
| `run` | 128 + N | The CLI died from signal N |
| `run` | 1 | Contexto could not start: the proxy, or the session store (for example a `CONTEXTO_HOME` that exists but cannot be written) |
| `run` | 2 | Usage error, for example `--bound` above 170,000 or not above the headroom floor (25,000 by default), an invalid `--session` id or one that only starts existing ids, a `CONTEXTO_HOME` that is a file, a `--settings` after `claude`, a cloud-provider switch such as `CLAUDE_CODE_USE_BEDROCK` that is on in the environment or a Claude Code settings file (Claude Code would then bypass Contexto), a Claude Code settings file Contexto cannot read and that could set such a switch, no Codex key helper, or a Codex upstream that would be Anthropic's API |
| `run` | 127 | `claude` or `codex` is not on `PATH` (checked before anything starts), or cannot be executed |
| `run` | 130 | Ctrl+C during the version check or while waiting for another launch to choose its session, before the CLI started |
| `proxy` | 1 | Could not start: a port in use or not allowed (for example `--port 1`), an invalid upstream, or a `CONTEXTO_HOME` that cannot be written |
| `proxy` | 2 | `--port` outside 0 to 65535, an invalid `--session` id (`''` included), an id that only starts existing session ids, or a `CONTEXTO_HOME` that is a file |
| `mcp` | 0 | The CLI closed the server's input (the normal end) |
| `mcp` | 1 | An invalid or unknown session id (it serves existing sessions only) |
| `mcp` | 2 | Invalid `--search-cache-mb` |
| `sessions`, `recall`, `search`, `state` | 0 | Success (an empty session list, or a search with no match, is a success) |
| `sessions`, `recall`, `search`, `state` | 1 | Unknown, ambiguous or invalid session id, pages that cannot be read, an unknown or malformed page id, an offset past the end of the page, an unreadable ledger, or a `CONTEXTO_HOME` that is not a directory |
| `sessions prune` | 1 | A session could not be deleted, or the session named by `--session` was skipped (in use, active in the last 10 minutes, or unsafe to delete) |
| `sessions prune` | 2 | `--session` is not a session but the start of existing ids (prune needs the full id) |
| `project` | 0 | Success: `show`, or `forget` that did everything asked (a record or session already forgotten counts as done) |
| `project` | 1 | `--dir` (or the current directory) is not an existing directory, `CONTEXTO_HOME` is not a directory, or `forget` did not do something it was asked: no terminal to confirm on without `--yes`, or an answer other than `y` or `yes` (nothing is forgotten), or a record id that is not carried, a session id that is not a session of the project, a session pruned meanwhile, or a file that cannot be written (the rest is still done) |
| `project forget` | 2 | `--session` is not a session but the start of existing ids (forget needs the full id); nothing is forgotten |
| `project forget` | 130 | Ctrl+C at the question; nothing was forgotten |
| any | 2 | Unknown command or option, an invalid option value (for example `--bound abc`), or a missing argument (`contexto` with no command prints help and exits 2) |
