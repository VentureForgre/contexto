# `contexto run` reference

[Contexto](../README.md) / [Documentation](README.md)

The options of `contexto run` and what the launcher does, `contexto proxy`, the environment
variables Contexto reads, and the upstreams it supports.

## `contexto run`

```
contexto run [options] claude [claude arguments]
contexto run [options] codex [codex arguments]
```

| Option | Default | Meaning |
|---|---|---|
| `--bound N` | 150,000 | The bound, in tokens. Above 170,000 needs `--allow-large-bound`. |
| `--allow-large-bound` | off | Allow `--bound` above 170,000 (Claude Code may then compact on its own). |
| `--window-fraction F` | 0.5 | Share of the window budget kept as recent turns after a rotation. |
| `--headroom-floor N` | 25,000 | The smallest headroom below the bound; a request within it triggers a rotation. |
| `--session ID` | see [Resuming](claude-code.md) | Full session id to resume or create. Without it, `--resume <id>` and `--continue` reuse the session that served that conversation; anything else gets a new id. A prefix of existing ids is refused (exit 2). |
| `--upstream URL` | resolved | Upstream base URL for Claude Code requests. |
| `--observe-thinking` | off | Adds the `thinking-binding-controls-2026-08-01` beta so dropped thinking is logged. This changes the request by one beta flag. |
| `--codex-auth MODE` | `apikey` | `apikey` or `chatgpt` (see [Codex](codex.md)). |
| `--codex-key-helper CMD` | see [Codex](codex.md) | Command that prints the API key (Codex, API-key mode). |
| `--codex-upstream URL` | see [Codex](codex.md) | Upstream for Codex requests, including its version path. |
| `--no-version-check` | off | Skip the check of `claude --version` or `codex --version` against the versions Contexto was tested with. |
| `--quiet` | off | Do not print the summary when the CLI exits; warnings are still printed. Also `CONTEXTO_QUIET=1`. See [Stats](stats.md). |
| `--no-statusline` | off | Claude Code only: do not add Contexto's status line. It is added only when no settings file Claude Code reads has a `statusLine` of its own. Also `CONTEXTO_NO_STATUSLINE=1`. See [Status line](statusline.md). |
| `--no-project-memory` | off | Keep this session out of [project memory](project-memory.md): it carries nothing from the earlier sessions of its project (a resumed session stops sending what it carried: at once before its first rotation; after it, from its next rotation) and gives nothing to later ones. The session records it, so a resume stays off without the option. Put it before `claude` or `codex`: after `codex` it goes to Codex unchanged. |

For Claude Code, the launcher:

1. resolves the upstream Claude Code would otherwise use (see [Troubleshooting](troubleshooting.md));
2. picks the session (`--session`, else the one a `--resume <id>` or `--continue` names, else a new
   one), opens it under `$CONTEXTO_HOME/sessions/<id>/` and starts the proxy on
   `127.0.0.1:<free port>`;
3. writes two temporary files (mode 0600, deleted on exit): a `--settings` file that sets
   `env.ANTHROPIC_BASE_URL` to the proxy and the hint-header variables below (only
   `CONTEXTO_HINT_HEADERS` when you have turned hint headers off), allows Contexto's
   five MCP tools by name (`permissions.allow`; see [Troubleshooting](troubleshooting.md)), and adds
   Contexto's `statusLine` only when none of your settings files has one and it is not turned off (see
   [Status line](statusline.md)), and an `--mcp-config` file that
   starts Contexto's MCP server (`python -P -m contexto mcp --session=<id>` with the same Python, and
   `CONTEXTO_HOME`, plus `CONTEXTO_SEARCH_CACHE_MB` when set, in its environment; `-P` keeps your
   project directory off the import path);
4. runs `claude --settings=<file> --mcp-config=<file> [your args]` in your terminal, with
   `CONTEXTO_SESSION=<id>` in its environment, and returns its exit code. Each flag and its file are
   one argument, so a prompt right after `claude` (`contexto run claude "fix the tests"`) stays a
   prompt.
5. after Claude Code exits, prints any warnings raised while it ran (printing them mid-run would land
   in Claude Code's input box), a short summary of the run (see [Stats](stats.md); `--quiet` drops
   it), then the command that resumes the conversation through Contexto.

`contexto run claude --version` (or `-v`, `--help`, `-h` alone) runs Claude Code without a session or
proxy. SIGTERM, SIGHUP and SIGQUIT are forwarded to Claude Code; SIGUSR1, SIGUSR2 and SIGALRM end it
with SIGTERM. If Contexto itself is killed with SIGKILL, a watchdog process ends Claude Code and
removes the temporary files; the session log then has no `exit` event. A launch that cannot start
(for example the proxy fails) removes the session it created; a resumed `--session` is kept. One
exception: a `claude` that is on `PATH` but cannot be executed (a bad `#!` line, for example) fails
after the session opens, exits 127, and leaves that empty session behind.

**Your own `--settings` is refused** (exit 2), because Claude Code accepts only one and the launcher
needs it to route requests through the proxy. Put those settings in your Claude settings files
instead. Details are under [Troubleshooting](troubleshooting.md).

At start, `contexto run` warns on stderr when the bound leaves a window budget under 25,000 tokens
and suggests a larger `--bound`. It assumes a fixed overhead of 28,000 tokens for Claude Code
(measured 18,641 to 27,925 on 2.1.314 with tool search on) and 19,000 for Codex. It warns again,
once, if the measured overhead does the same.

**Tip for Haiku 5.5:** on Haiku 5.5, `--bound 125000` keeps requests at turn start below the 100K price
tier, above which every rate is 5 times higher. Replaying a recorded acceptance session at that bound,
with the model's behavior held fixed, every request stayed under it, for a modeled 45.7% lower
cost. Long tool loops can still cross it, and accuracy at that bound is not yet measured. See
[docs/research/M2-haiku-100k-tier.md](research/M2-haiku-100k-tier.md).
The [Quickstart](../README.md#quickstart)'s `--bound 120000` is also below the tier at turn start: with the default headroom floor
(25,000) and minimum turn room (12,000), any bound up to 125000 is.

## `contexto proxy`

`contexto proxy [--session ID] [--port N] [--upstream URL] [--observe-thinking]` runs the proxy alone,
mainly for tests. `--port` defaults to 0 (a free port). It prints
`listening on 127.0.0.1:<port> session <id> upstream <source>` and forwards requests unchanged, with
no paging. `--session` takes a full id; a prefix of existing ids exits 2. A proxy that cannot start
removes the session it created.

The proxy, here and under `contexto run`, answers only the CLI on this machine: a request whose
`Host` does not name `127.0.0.1` or `localhost` (any port, or none), or that carries an `Origin`
header (any web page), gets 403 `permission_error` and is logged as `request_refused`. If you point
another local client at the proxy, use one of those two names. When the CLI goes away while a
request is in flight (Esc), the proxy stops the upstream response at once and logs the request with
`client_aborted: true`.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `CONTEXTO_HOME` | all commands | Data directory (default `~/.contexto`), resolved to an absolute path. `contexto run` passes it to the MCP server for both Claude Code and Codex. `contexto mcp --home` overrides it. |
| `CONTEXTO_SESSION` | `recall`, `search`, `state`, `stats`, `statusline` | Session to read when `--session` is not given. `contexto run` sets it for the CLI it runs. `statusline` uses it only under the rule in [Status line](statusline.md#calling-it-from-your-own-status-line). |
| `CONTEXTO_QUIET` | `run` | Any value but empty, `0`, `false` or `no` drops the summary at exit, like `--quiet`. |
| `CONTEXTO_NO_STATUSLINE` | `run claude` | Any value but empty, `0`, `false` or `no` leaves Claude Code's status line alone, like `--no-statusline`. |
| `CONTEXTO_CODEX_KEY_HELPER` | `run codex` | Key helper command when `--codex-key-helper` is not given. |
| `CONTEXTO_CODEX_UPSTREAM` | `run codex` | Codex upstream when `--codex-upstream` is not given. |
| `CONTEXTO_SEARCH_CACHE_MB` | `mcp` | Page text the MCP server holds in memory for `contexto_search`, in MB (default 64; `0` holds none). See [MCP tools](mcp-tools.md). |
| `CONTEXTO_RECALL_VARIANT` | new sessions | `default` or `legacy` recall guidance text. `legacy` is kept for diagnosis. A session keeps the variant it started with. |
| `ANTHROPIC_BASE_URL` | `run`, `proxy` | Upstream, when no settings file sets one (see [Troubleshooting](troubleshooting.md)). |
| `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` | `run`, `proxy` | Read, never set, by Contexto: when truthy, no project `.claude/` settings file is used, as Claude Code does in `-p` mode (see [Troubleshooting](troubleshooting.md)). |
| `CONTEXTO_NO_VERSION_CHECK` | `run` | `1` skips the CLI version check, like `--no-version-check`. |
| `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_MANTLE`, `CLAUDE_CODE_USE_ANTHROPIC_AWS`, `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD` | `run claude` | Read, never set: when one is on in the environment or in a Claude Code settings file (including the managed settings file), Claude Code sends nothing to Contexto, so `contexto run claude` refuses to start (exit 2). It also refuses when a settings file it cannot read (not a regular file, over 4 MiB, or not JSON) could set one. |
| `CLAUDE_CODE_GATEWAY_HINT_HEADERS` | `run claude` | `contexto run claude` sets it to `1` for Claude Code so the engine can read Claude Code's request-class headers, and removes those headers before forwarding unless you had turned them on yourself. Set it to `0`, `false`, `no` or `off` in your environment or user settings to keep them off; the engine then uses its older text signatures. |
| `CONTEXTO_HINT_HEADERS` | `run claude` | Set by `contexto run claude` for the CLI it runs (`stripped`, `forwarded` or `off`), so a `contexto run claude` started inside that session can tell Contexto's `1` from yours. Not meant to be set by hand. |
| `CONTEXTO_PROJECT_MEMORY` | `run` | `0`, `false`, `off` or `no` (any case) turns [project memory](project-memory.md) off for the launch, like `--no-project-memory`. `1`, `true`, `on`, `yes` or unset leaves it on; any other value leaves it on and prints a warning. |

## Upstreams and CLIProxyAPI

If you run Contexto yourself, the supported upstreams are the vendor's API with your own API key, or
a [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) instance reached with one of its client
keys. Subscription logins live in CLIProxyAPI or the vendor's own CLI, never in Contexto, and you
must follow your providers' terms. For CLIProxyAPI, set Claude Code's `ANTHROPIC_BASE_URL` to its
root (for example `http://127.0.0.1:8317`, no `/v1`) and its `apiKeyHelper` to a command that prints
the client key; `contexto run claude` and `contexto run codex` then both go through it. With the key
in `ANTHROPIC_AUTH_TOKEN` instead, Codex also needs `--codex-key-helper`. Setup, checks, and what was
tested: [docs/cliproxyapi.md](cliproxyapi.md).
