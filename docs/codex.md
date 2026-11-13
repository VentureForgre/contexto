# Codex (API-key mode)

[Contexto](../README.md) / [Documentation](README.md)

How to run Codex through Contexto with an API key: the key helper, the upstream, and what Contexto
sets up for Codex.

```sh
contexto run codex
contexto run --codex-key-helper 'my-key-command' codex
```

Codex gets its key from a helper command that prints it: `--codex-key-helper`, else
`$CONTEXTO_CODEX_KEY_HELPER`, else the `apiKeyHelper` from your Claude Code settings. Codex runs the
helper itself; Contexto never reads or stores the key. Codex runs it with `/bin/sh -c`, as Claude
Code runs `apiKeyHelper`, so `~`, `$VAR` and pipes work.

A helper or base URL from a project's `.claude/` files counts only when Claude Code has recorded the
folder as trusted (run `claude` there once and accept the trust dialog). Otherwise Contexto ignores
it and says which file; pass `--codex-key-helper` (or set `CONTEXTO_CODEX_KEY_HELPER`) and
`--codex-upstream` instead. With no helper left the launch stops (exit 2) and leaves no session
behind. While `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is set (for example on a runner that checks out
content it does not trust), no folder counts as trusted, and no project `.claude/` settings file is
used for any CLI ([Troubleshooting](troubleshooting.md)).

Codex requests go to `--codex-upstream`, else `$CONTEXTO_CODEX_UPSTREAM`, else (API-key mode) the
upstream Claude Code would use plus `/v1`, which suits a CLIProxyAPI root, or (ChatGPT mode) the
ChatGPT Codex backend. **With an OpenAI API key, pass `--codex-upstream https://api.openai.com/v1`**
(or set `CONTEXTO_CODEX_UPSTREAM`). If Claude Code's upstream is Anthropic's API, Contexto refuses
(exit 2) rather than send the key there. The session line names where the upstream came from, for
example `contexto: session <id> (bound 150,000, upstream codex-env, codex auth apikey, engine rewrite)`.
The Codex labels are `codex-flag`, `codex-env`, `chatgpt-default`, or `claude-<source>+/v1` (for
example `claude-settings:project+/v1`).

`contexto run codex` sets `CODEX_HOME` to `$CONTEXTO_HOME/sessions/<id>/codex-home` (mode 0700) and
writes a `config.toml` there (mode 0600) that:

- adds a model provider `contexto` whose `base_url` is the proxy, with `wire_api = "responses"`,
  `supports_websockets = false`, and the key helper as its auth command;
- turns request compression off (`features.enable_request_compression = false`);
- adds the MCP server `contexto` (`python -P -m contexto mcp --session <id>` with the same Python)
  with `default_tools_approval_mode = "approve"`, so recall works in `codex exec` too, and
  `CONTEXTO_HOME` (plus `CONTEXTO_SEARCH_CACHE_MB` when set) in its `env` table, because Codex starts
  MCP servers with a restricted environment.

Interactive Codex launches pass `--no-daemon` (Codex 0.156.0 and later), so Codex's background
app-server is not left running per session; `codex agents`, which needs that server, is refused
unless `--remote` sends it elsewhere. On exit Contexto deletes `config.toml` and `packages/` and keeps
the rest of the directory, so Codex can resume its rollouts: `contexto run --session <id> codex
resume --last`.
Because `CODEX_HOME` is new, your own `~/.codex` configuration is not loaded in this mode. Install
Contexto (an editable install is fine) rather than running it from a checkout with
`PYTHONPATH=src`, because Codex starts the MCP server with a restricted environment.

ChatGPT login mode (`--codex-auth chatgpt`) uses your own `CODEX_HOME` with `-c` overrides instead. It
is tested only with synthetic credentials, never live. Follow your provider's terms for whatever
credentials you use with Contexto.
