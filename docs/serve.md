# `contexto serve`

[Contexto](../README.md) / [Documentation](README.md)

`contexto run claude` starts Claude Code itself, so it can point that one process at a proxy it
started for it. Some clients are started by something else: the VS Code and JetBrains extensions,
the desktop app, the Claude Agent SDK, the GitHub Action, `claude -p` in a CI job. Each of them
accepts `ANTHROPIC_BASE_URL`. `contexto serve` runs the same engine as a service on a fixed local
port, so any of them can be pointed at it.

The service keeps every Claude Code session under the token bound, each in its own session store,
exactly as `contexto run claude` does for the session it launches.

## Start it

```sh
contexto serve --upstream https://api.anthropic.com
```

It listens on `127.0.0.1:7837` and prints one line on stderr once it is listening:

```
contexto serve: listening on 127.0.0.1:7837 (bound 150,000, upstream flag)
```

It serves until it gets SIGINT (Ctrl+C), SIGTERM or SIGHUP. On any of them it stops accepting
connections, waits up to 2 seconds for requests in flight, closes every session and exits 0. A
second signal ends that wait. Started under `nohup`, it keeps ignoring SIGHUP, so it outlives its
terminal.

Options:

| Option | Meaning |
|---|---|
| `--port N` | Port on `127.0.0.1` (default 7837; 0 picks a free one). There is no `--host`. |
| `--upstream URL` | Where requests are forwarded. See [Upstream](#upstream). |
| `--bound N`, `--allow-large-bound`, `--window-fraction F`, `--headroom-floor N`, `--observe-thinking` | The engine options of `contexto run`, with the same meaning and the same checks. They apply to every session. |
| `--max-sessions N` | Sessions open at once (default 64). |
| `--idle-minutes M` | Close a session idle this long (default 60). Its next request reopens it. |

The service forwards each client's own credentials (`x-api-key` or `Authorization`) unchanged. It
has none of its own and stores none.

### Upstream

Without `--upstream`, the service uses the base URL Claude Code would use from the directory it is
started in: the settings files, then `ANTHROPIC_BASE_URL` in the environment, then
`https://api.anthropic.com`. Once you have set a client up for the service, that base URL is the
service's own address. The service refuses to start when its upstream is a loopback address on its
own port or a URL that carries its token, and tells you to pass `--upstream URL`. It never picks
another upstream for you.

## The token

`serve` requires a token on every request. Without the token a request is not forwarded upstream,
does not reach a session store, and gets no answer from the service's own endpoints: it gets a 403.

The token is 32 random bytes, written as 43 URL-safe characters to `$CONTEXTO_HOME/serve.token`
(default `~/.contexto/serve.token`) with mode 0600 the first time any `contexto serve` command
needs it. It is the first path segment of the base URL a client is given:

```
http://127.0.0.1:7837/<token>
```

Claude Code keeps that prefix on every request it sends, so nothing else needs configuring. The
service removes the prefix before it forwards or logs anything: the upstream never sees the token,
and neither does any log.

The token is as private as the file or environment it is copied into. A settings file that holds
the base URL holds the token, so keep it at mode 0600. Prefer a file to a command-line argument or
an inline `ANTHROPIC_BASE_URL=... claude`: a command line ends up in shell history, and its
arguments are visible in the process list.

The service refuses to start if `serve.token` is a symlink, is not a regular file, or has any
group or other permission bit set. The message names the fix:

```sh
contexto serve --rotate-token
```

That replaces the token. A running service keeps the old one until it is restarted, and every
client then needs the new URL.

## Set up a client

`--print-settings` prints a Claude Code settings file for a client. It creates the token if there
is none, and names the port of a running service (or `--port`, or 7837).

```sh
umask 077
contexto serve --print-settings > ~/.contexto/serve-settings.json
claude --settings ~/.contexto/serve-settings.json
```

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:7837/<token>",
    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"
  },
  "permissions": {
    "allow": [
      "mcp__contexto__contexto_recall",
      "mcp__contexto__contexto_pin",
      "mcp__contexto__contexto_close",
      "mcp__contexto__contexto_state",
      "mcp__contexto__contexto_search"
    ]
  }
}
```

A client that takes no `--settings` flag reads the same keys from Claude Code's user settings
(`~/.claude/settings.json`): merge the `env` and `permissions.allow` entries into it. Claude Code
takes the base URL from a `--settings` file first, then its settings files, then the environment
(measured on 2.1.322).

`--print-mcp-config` prints the MCP config that gives the client Contexto's tools
(`contexto_search`, `contexto_recall` and the ledger tools). It holds no token:

```sh
contexto serve --print-mcp-config > ~/.contexto/serve-mcp.json
claude --settings ~/.contexto/serve-settings.json --mcp-config ~/.contexto/serve-mcp.json
```

The server it starts, `contexto mcp --serve`, reads the token file itself and asks the service
which session each tool call belongs to. Without the MCP server the engine still keeps the prompt
under the bound, but the model cannot read back what was paged out; the service warns about it on
stderr, once per session.

`--status` says whether the service is running, with its port and the number of open sessions. It
exits 0 when it is and 1 when it is not.

## How requests are routed

Claude Code sends its session id on every model request: the `X-Claude-Code-Session-Id` header,
and `session_id` inside `metadata.user_id` on `/v1/messages`. The service routes on it.

- **One session id, one store.** The first `POST /v1/messages` of a session id opens a Contexto
  session for it. If a Contexto session already served that Claude Code session, the service
  reuses it. That includes a session `contexto run claude` made, so a conversation keeps its pages
  and pins when you move between `contexto run claude --resume <id>` and a `serve` client.
  Otherwise the service makes a new session with a random id. A string the client sent is never
  used as a directory name.
- **Subagents and side requests** carry their main conversation's session id, so they reach the
  same store, and the engine treats them as it does under `contexto run`.
- **Agent-team teammates** that run as their own process have their own session id and name the
  lead's in `parent_session_id`. Their requests go to the lead's store.
- **Requests without a usable session id** are forwarded unchanged and logged in `serve.log`.
  They never reach a store. That covers `HEAD /api/hello`, `GET /v1/models`, any other path, a
  request whose header and body name different sessions, and a `count_tokens` for a session id no
  store has served.
- **Each session has its own lock.** A long rewrite in one session does not delay another.

To see which store a conversation is in, `contexto sessions list` shows them, and each session's
`meta.json` records the Claude Code session ids it served.

At most `--max-sessions` sessions are open at once. When one more is needed, the least recently
used session is closed; a session idle for `--idle-minutes` is closed too. Closing loses nothing: a
session's state is on disk, and its next request reopens it. A session with a request in flight is
never closed. If every open session is busy, a request of one more session is forwarded unchanged
(so its prompt is not bounded), the service log records `session_cap`, and stderr says to raise
`--max-sessions`.

Warnings that `contexto run` prints for its one session (the bound is too small, Contexto's MCP
tools are missing, a tool call was refused) are printed on the service's stderr with the Contexto
session id in front.

## Endpoints of the service itself

Two paths under the token prefix are answered by the service and never forwarded:

- `GET /<token>/contexto/health` returns `{"ok": true, "version": "...", "port": 7837,
  "sessions": N}`, where `sessions` is the number open.
- `POST /<token>/contexto/resolve` with `{"tool_use_id": "<id>"}` returns
  `{"session": "<Contexto session id>"}`, or 404 `{"error": "unknown_tool_use"}`. The MCP server
  uses it: Claude Code tells an MCP server the model's `tool_use` id with each call and nothing
  else about the session. The service remembers the newest 8,192 ids, and learns each one before
  the client receives the tool call.

Bodies are JSON and at most 64 KiB. Every other path under `/<token>/contexto/` is a 404.

## Files

All under `$CONTEXTO_HOME`, all mode 0600:

| File | Holds |
|---|---|
| `serve.token` | The token: one line of ASCII. |
| `serve.json` | `{"port": N, "pid": N}`, written once the service listens and removed on a clean shutdown. A killed service leaves it behind; `--status` checks that the pid still runs. |
| `serve.log`, `serve.log.1` | The service log: JSON lines with event names, reasons, counts, request paths without the token, and status codes. Never header values, request or response bodies, the token, or the upstream URL. At 5 MB `serve.log` is renamed to `serve.log.1`, replacing the last one. |
| `sessions/<id>/` | One session store per conversation, the same files as under `contexto run`. |

A refused request is logged by its reason only (`origin`, `host` or `token`), never its path, and
at most once a second; the ones in between are counted on the next line.

## Guards

- **Port taken.** One line and exit 1. If the port is held by a `contexto serve` that is already
  running, the line says so.
- **Upstream is the service itself.** Refused, as described under [Upstream](#upstream).
- **Cloud-provider switches.** Claude Code with `CLAUDE_CODE_USE_BEDROCK`,
  `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_MANTLE` or
  `CLAUDE_CODE_USE_ANTHROPIC_AWS` set sends nothing to `ANTHROPIC_BASE_URL` (measured on 2.1.322),
  so such a client never reaches the service. The service cannot see its clients' environments. It
  warns when its own environment sets one of them, and so does `--print-settings`.
- **Host and Origin.** As for the proxy of `contexto run`: a request must name `127.0.0.1` or
  `localhost` in `Host` and carry no `Origin`, or it gets the 403.

## Limits

- Loopback only, for one user. The service is not meant to be shared between accounts on a machine
  or reached from another machine.
- Claude Code's Messages API only. A Codex `/responses` request, a Chat Completions request, and
  Bedrock or Vertex request paths are forwarded unchanged if they arrive; they are not bounded.
- The token is sent to whatever listens on the port. If the service is down and another local
  process takes port 7837, a client that still has the URL sends that process the token, its
  requests and its API credentials. Stop clients before you stop the service for long, or rotate
  the token afterwards.
- A new Claude Code session id is a new Contexto session. `/clear` starts a new session id, so
  what was paged out or pinned before it stays in the earlier store (`contexto search --session
  <id>`, `contexto state --session <id>`), where under `contexto run` the launch's one store keeps
  serving. `/compact` and `--resume <id>` keep the id.
- A session the service makes records no working directory: the service does not run in the
  client's directory. Pinned file sources of such a session are not checked for staleness, and
  `contexto run claude --continue` does not find it by directory. `contexto run claude --resume
  <id>` does find it.
- Measured with API-key clients on Claude Code 2.1.322 on macOS. Not run: a claude.ai (OAuth)
  login, and the IDE extensions and desktop app themselves.

## Recipes

Step-by-step setups for each client (the IDE extensions, the desktop app, the Agent SDK, the
GitHub Action, CI) are in a separate document.
