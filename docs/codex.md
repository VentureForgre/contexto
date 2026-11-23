# Codex (API-key mode)

[Contexto](../README.md) / [Documentation](README.md)

How to run Codex through Contexto with an API key: out-of-the-box `$OPENAI_API_KEY`, key helpers,
upstream resolution, user `~/.codex` preservation, in-turn paging, and WebSocket transport.

```sh
OPENAI_API_KEY=sk-... contexto run codex
contexto run --codex-key-helper 'my-key-command' --codex-upstream https://api.openai.com/v1 codex
```

## Authentication and upstream

In API-key mode (the default), Codex gets its credential from, in order:

1. `--codex-key-helper` or `$CONTEXTO_CODEX_KEY_HELPER`;
2. `apiKeyHelper` from your Claude Code settings (project `.claude/` files only in folders Claude
   Code has recorded as trusted);
3. `$OPENAI_API_KEY` in the environment, read natively by Codex via `env_key = "OPENAI_API_KEY"`
   (never when the upstream was derived from Claude Code's settings, so an OpenAI key cannot be sent
   to another gateway). Contexto never reads or stores the key value.

When a key helper is used, Codex runs it itself with `/bin/sh -c`, as Claude Code runs
`apiKeyHelper`, so `~`, `$VAR` and pipes work.

A helper or base URL from a project's `.claude/` files counts only when Claude Code has recorded the
folder as trusted (run `claude` there once and accept the trust dialog). Otherwise Contexto ignores
it and says which file; pass `--codex-key-helper` (or set `CONTEXTO_CODEX_KEY_HELPER`) and
`--codex-upstream` instead. With no helper or `$OPENAI_API_KEY` left the launch stops (exit 2) and
leaves no session behind. While `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is set (for example on a runner
that checks out content it does not trust), no folder counts as trusted, and no project `.claude/`
settings file is used for any CLI ([Troubleshooting](troubleshooting.md)).

Codex requests go to:

1. `--codex-upstream` (`codex-flag`), else
2. `$CONTEXTO_CODEX_UPSTREAM` (`codex-env`), else
3. (ChatGPT mode) the ChatGPT Codex backend (`chatgpt-default`), else
4. (API-key mode) Claude Code's upstream plus `/v1` when it is not Anthropic's API (such as a
   CLIProxyAPI root: `claude-<source>+/v1`), else
5. `https://api.openai.com/v1` (`openai-env`) when `$OPENAI_API_KEY` is set and no key helper was
   specified.

If Claude Code's upstream is Anthropic's API and `$OPENAI_API_KEY` is not set (or an explicit key
helper was given without an upstream), Contexto refuses (exit 2) rather than send the key there.
Contexto also refuses an upstream pointing at this Contexto home's `contexto serve` (exit 2),
matching `contexto run claude`. The session line names where the upstream came from, for example
`contexto: session <id> (bound 150,000, upstream openai-env, codex auth apikey, engine rewrite)`.

## Session `CODEX_HOME` and preserving `~/.codex`

`contexto run codex` sets `CODEX_HOME` to `$CONTEXTO_HOME/sessions/<id>/codex-home` (mode 0700) and
writes a `config.toml` there (mode 0600) that merges your existing `~/.codex/config.toml` (or
`$CODEX_HOME/config.toml` when set) with Contexto's required overrides:

- sets `model_provider = "contexto"` and adds `[model_providers.contexto]` with `base_url` pointed
  at the local proxy, `wire_api = "responses"`, `supports_websockets = false` (or `true` with
  `--codex-websocket` / `CONTEXTO_CODEX_WEBSOCKET=1`), and either `auth` (key helper) or
  `env_key = "OPENAI_API_KEY"`;
- turns request compression off (`features.enable_request_compression = false`), preserving your other
  feature flags;
- adds the MCP server `contexto` (`python -P -m contexto mcp --session=<id>` with the same Python)
  with `default_tools_approval_mode = "approve"`, so recall works in `codex exec` too, and
  `CONTEXTO_HOME` (plus `CONTEXTO_SEARCH_CACHE_MB` when set) in its `env` table, preserving your
  other MCP servers;
- symlinks non-secret assets (`AGENTS.md`, `instructions.md`, `skills`, `prompts`, `rules`, `agents`)
  from your `~/.codex` into the session `CODEX_HOME` (never `auth.json`, sessions, or databases).

Interactive Codex launches pass `--no-daemon` (Codex 0.156.0 and later, verified through 0.170.0), so
Codex's background app-server is not left running per session; `codex agents`, which needs that
server, is refused unless `--remote` sends it elsewhere. On exit Contexto deletes `config.toml`,
unlinks the temporary asset symlinks, and removes `packages/`, keeping the rest of the directory so
Codex can resume its rollouts: `contexto run --session <id> codex resume --last`. Install Contexto
(an editable install is fine) rather than running it from a checkout with `PYTHONPATH=src`, because
Codex starts the MCP server with a restricted environment.

## In-turn paging, project memory, and WebSocket transport

- **In-turn paging (`ResponsesInTurn`)**: long Codex tool loops and subagents stay under `--bound`
  the same way Claude Code does (stubbing older tool outputs and folding earlier rounds when needed,
  preserving `reasoning` and `*_call`/`*_output` pairing). Pass `--no-in-turn-paging` (or
  `CONTEXTO_IN_TURN_PAGING=0`) to turn it off.
- **Project memory (D69)**: both direct MCP calls and Codex code-mode `exec` calls invoking
  `tools.mcp__contexto__contexto_pin(...)` / `tools.mcp__contexto__contexto_close(...)` in the main
  conversation are confirmed and carried across sessions.
- **WebSocket transport (opt-in)**: HTTP stays the default (`supports_websockets = false`). Pass
  `--codex-websocket` (or set `CONTEXTO_CODEX_WEBSOCKET=1`) to allow OpenAI Responses WebSocket
  connections through the proxy; `response.create` frames are rewritten through the engine and
  `response.completed` frames report usage before tool calls reach Codex. If upstream answers with a
  non-101 HTTP status, the proxy forwards it so Codex falls back to HTTP.

ChatGPT login mode (`--codex-auth chatgpt`) uses your own `CODEX_HOME` with `-c` overrides instead. It
is tested only with synthetic credentials, never live. Follow your provider's terms for whatever
credentials you use with Contexto.
