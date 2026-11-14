# `contexto serve`

[Contexto](../README.md) / [Documentation](README.md)

`contexto run claude` starts Claude Code itself, so it can point that one process at a proxy it
started for it. Some clients are started by something else: the VS Code and JetBrains extensions,
the desktop app, the Claude Agent SDK, the GitHub Action, `claude -p` in a CI job. Each of them
accepts `ANTHROPIC_BASE_URL`. `contexto serve` runs the same engine as a service on a fixed local
port, so any of them can be pointed at it.

The service keeps every Claude Code session under the token bound, each in its own session store,
exactly as `contexto run claude` does for the session it launches. Its MCP server, `contexto mcp
--serve`, gives those clients Contexto's tools.

## Start it

```sh
contexto serve --upstream https://api.anthropic.com
```

It listens on `127.0.0.1:7837` and prints one line on stderr once it is listening:

```
contexto serve: listening on 127.0.0.1:7837 (bound 150,000, upstream flag)
```

It serves until it gets SIGINT (Ctrl+C), SIGTERM or SIGHUP. On any of them it stops routing (a
request that arrives on a connection already open gets a 503, "contexto serve is stopping", and
that connection is closed), removes `serve.json`, closes its port, waits up to 2 seconds for the
requests already in flight, closes every session and exits 0. A second signal ends that wait.
Because `serve.json` goes before the port, `--status` and the MCP server never send the token to a
process that binds the port while the service is still finishing, and an MCP server call that is
being asked again stops at its next try. Started under `nohup`, it keeps ignoring SIGHUP, so it outlives its
terminal.

One service runs per Contexto home. A second `contexto serve` on the same `$CONTEXTO_HOME` is
refused whatever its port, with one line naming the port and process id of the one that runs.

Options:

| Option | Meaning |
|---|---|
| `--port N` | Port on `127.0.0.1` (default 7837; 0 picks a free one). There is no `--host`. |
| `--upstream URL` | Where requests are forwarded. See [Upstream](#upstream). |
| `--bound N`, `--allow-large-bound`, `--window-fraction F`, `--headroom-floor N`, `--observe-thinking` | The engine options of `contexto run`, with the same meaning and the same checks. They apply to every session. |
| `--max-sessions N` | Sessions open at once (default 64). |
| `--idle-minutes M` | Close a session idle this long (default 60). Its next request reopens it. |
| `--forward-hint-headers` | Pass Claude Code's gateway hint headers on to the upstream. By default they are removed. See [Gateway hint headers](#gateway-hint-headers). |

The service forwards each client's own credentials (`x-api-key` or `Authorization`) unchanged. It
has none of its own and stores none.

### Upstream

Without `--upstream`, the service uses the base URL Claude Code would use from the directory it is
started in: the settings files, then `ANTHROPIC_BASE_URL` in the environment, then
`https://api.anthropic.com`. Once the service's URL is in your Claude Code settings, that base URL
is the service's own address, so from then on `contexto serve` needs `--upstream URL`. Without it
the service refuses to start, because its upstream would be a loopback address on its own port or a
URL that carries its token, and says to pass `--upstream URL`. It never picks another upstream for
you.

The same setting reaches the other Contexto commands. `contexto run claude` and `contexto proxy`
refuse an upstream that is this home's service (a loopback URL whose first path segment is the
token) with a usage error that says to pass `--upstream URL`, instead of sending every request
through two engines that open the same store. The error does not print the URL.

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
and neither does any log. A request header whose value holds the token (a `Referer` that quotes the
base URL, say) is dropped before the request is forwarded; the service log records how many, never
which.

The token is as private as the files it is copied into. If a conversation reads such a file (a
settings file in a project, say), the token reaches the model provider inside that conversation;
rotate it then. Prefer a file to a command-line argument or an inline `ANTHROPIC_BASE_URL=...
claude`: a command line ends up in shell history, and its arguments are visible in the process
list.

The service refuses to start if `serve.token` is a symlink, is not a regular file, or has any
group or other permission bit set. The message names the fix:

```sh
contexto serve --rotate-token
```

That replaces the token. A running service keeps the old one until it is restarted, and every
client then needs the new URL.

## Set up a client

`--print-settings` prints a Claude Code settings file for a client. It creates the token if there
is none, and names the port of the service that runs for this home (or `--port`, or 7837). The
file holds the token, so it is written at mode 0600 (`umask 077`):

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
(`~/.claude/settings.json`): merge the `env` and `permissions.allow` entries into it, then keep
that file at mode 0600 (`chmod 600 ~/.claude/settings.json`), since it now holds the token. That
file is often 0644. Whether Claude Code keeps the mode when it rewrites the file was not measured:
check it after you change a setting from inside Claude Code. Claude Code takes the base URL from a
`--settings` file first, then its settings files, then the environment (measured on 2.1.322).

`--print-mcp-config` prints the MCP config that gives the client Contexto's tools
(`contexto_search`, `contexto_recall` and the ledger tools `contexto_pin`, `contexto_close` and
`contexto_state`). It holds no token:

```sh
contexto serve --print-mcp-config > ~/.contexto/serve-mcp.json
claude --settings ~/.contexto/serve-settings.json --mcp-config ~/.contexto/serve-mcp.json
```

The server it starts, `contexto mcp --serve`, is one server for every conversation of a Claude Code
process. It lists the five tools whether or not the service is running. For each tool call it asks
the service which session the call belongs to (see [The MCP server](#the-mcp-server)) and runs the
tool against that session's store. Without the MCP server the engine still keeps the prompt under
the bound, but the model cannot read back what was paged out; the service warns about it on
stderr, once per session.

`--status` says whether the service is running, with its port and the number of open sessions. It
exits 0 when it is and 1 when it is not. It creates nothing, and it sends a request only to a
service that holds the home's lock (see [Files](#files)).

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
  lead's in `parent_session_id`. A `/v1/messages` that names a parent goes to the parent's session,
  and the store it opens or makes is recorded under the parent's id, never the teammate's, so the
  lead finds the same store whichever of them came first. The service remembers the teammate's
  parent in memory only, for the teammate's `count_tokens` requests, which cannot name a parent.
  A `/v1/messages` of that session id that names no parent is its own conversation and goes to its
  own session.
- **Requests without a usable session id** are forwarded unchanged and logged in `serve.log`.
  They never reach a store. That covers `HEAD /api/hello`, `GET /v1/models`, any other path, a
  request whose header and body name different sessions, an id that is not a plain id, and a
  `count_tokens` for a session id no store has served. A `/v1/messages` forwarded this way is not
  bounded, so `serve.log` records it as `unrouted` with its reason (`no_session_id`,
  `session_id_mismatch`, `invalid_session_id`, `session_cap`, `session_open_failed`), and stderr
  gets a warning at most once per reason per minute, with how many there were.
- **Each session has its own lock.** A long rewrite in one session does not delay another.

To see which store a conversation is in, `contexto sessions list` shows them, and each session's
`meta.json` records the Claude Code session ids it served.

At most `--max-sessions` sessions are open at once. When one more is needed, the least recently
used session is closed; a session idle for `--idle-minutes` is closed too. Closing loses nothing: a
session's state is on disk, and its next request reopens it. A session with a request in flight is
never closed. If every open session is busy, a request of one more session waits for one to finish
until 0.3 seconds after it arrived (a client has its whole response a moment before the service has
finished with it); requests of one conversation that arrive together share that wait rather than
take turns at it. If none finishes, the request is forwarded unchanged (so its prompt is not bounded), `serve.log`
records `session_cap`, and stderr says to raise `--max-sessions`.

A session store that cannot be opened leaves no new directory behind. The failure is remembered
for 5 seconds, so the next requests of that conversation are forwarded unchanged
(`session_open_failed`) without trying again at once.

Warnings that `contexto run` prints for its one session (the bound is too small, Contexto's MCP
tools are missing, a tool call was refused) are printed on the service's stderr with the Contexto
session id in front, once per session however often the session is closed and reopened.

### Gateway hint headers

`CLAUDE_CODE_GATEWAY_HINT_HEADERS` turns on the headers Claude Code documents for a gateway (off by
default with a custom base URL): `x-claude-code-request-class`, `x-claude-code-compaction`,
`x-claude-code-context-compacted`, `x-claude-code-prompt-id`, `x-claude-code-agent-type` and
`x-claude-code-prev-tool-durations`. The engine reads the request class and the compaction headers
in preference to its own signatures, and falls back to the signatures for a request without them,
as `contexto run claude` does. The service removes all six before it forwards a request, so the
upstream gets none of them, no prompt id included, just as without the setting.
`--forward-hint-headers` passes them on, for an upstream gateway that reads them. Leave the
variable out of the settings and the engine reads its signatures alone.

Beside the hint setting, `--print-settings` writes `CONTEXTO_HINT_HEADERS=stripped`, the marker
`contexto run claude` writes in its own settings file: it says the `1` is Contexto's, not yours. A
`contexto run claude` started inside a session that uses the printed file inherits both and strips
the hints, and so does one that finds both in a settings file the printed settings were merged into.
To have `contexto run claude` forward the hints to your own gateway, turn them on in your
environment or in a settings file without the marker.

Keep these settings in their own file passed with `--settings`, as above, where you can. Merged into
Claude Code's user settings, they reach every Claude Code you start, and they replace a hint
setting you had there: if you had turned the hints on in that file for your gateway,
`contexto run claude` strips them from then on, until you turn them on in one of the other places.

Session directories accumulate: every new conversation, every `claude -p` and every `/clear` adds
one under `$CONTEXTO_HOME/sessions/`. The service never deletes one. Remove old ones with
`contexto sessions prune`.

## The MCP server

`contexto mcp --serve` has no session of its own. Claude Code 2.1.322 gives an MCP server the
model's `tool_use` id with every call (`_meta["claudecode/toolUseId"]`) and nothing else that names
the conversation: the server's `CLAUDE_CODE_SESSION_ID` is set once when Claude Code starts it, and
after `/clear` it still names the conversation before the `/clear`. The service saw the response
that carried the call, so it knows the session. For each call the server:

1. takes the tool-use id from the call;
2. checks that a service holds the home's lock, reads `serve.json` and `serve.token`, and sends
   `POST /<token>/contexto/resolve` to `127.0.0.1` with that id. After a 404 it asks again (see
   below), each time only after the same check finds the same service: the lock held, and the
   port, process id and token of the first try. If the service stopped or changed, the call ends
   with the "not running" error and nothing more is sent;
3. opens the session the service names, which must be an existing session directory that is not a
   symlink, and runs the tool there. It never creates a session, and refuses an empty session
   directory (what `contexto sessions prune` leaves for a moment before it removes one).

It fails closed. When a step fails, the call gets a tool error saying which step and what to do,
and no store is opened, read or written. There is no fallback to another session. The errors: the
call carries no tool-use id; the service is not running (start `contexto serve`); the service has
no record of the call (the model request did not go through the service: check
`ANTHROPIC_BASE_URL`; the message also names a cloud-provider switch that is on in the server's
environment or in a settings file Claude Code reads from the server's directory, or a settings
file that cannot be read, by the same check `contexto run claude` refuses a launch by); the service refused the token (restart the service after `--rotate-token`); the
answer is outside the contract (another version, or another program on the port); the session
named cannot be opened.

The server keeps the 8 most recently used stores open and releases the others. It answers `tools/list`
with no service running, since a client that cannot list a server's tools reports nothing.

## Endpoints of the service itself

Two paths under the token prefix are answered by the service and never forwarded:

- `GET /<token>/contexto/health` returns `{"ok": true, "version": "...", "port": 7837,
  "sessions": N, "resolve": {"found": N, "unknown": N}}`: the sessions open, and how many resolve
  requests named a session and how many were answered unknown.
- `POST /<token>/contexto/resolve` with `{"tool_use_id": "<id>"}` returns
  `{"session": "<Contexto session id>"}`, or 404 `{"error": "unknown_tool_use"}`.

The service remembers the newest 8,192 tool-use ids. It learns the id of each client tool call
while it relays the response, before it writes the bytes that carry the call to the client:
whether the response is a stream it holds or a stream with a `Content-Length` (`hold_skipped` in
the session log), and whether or not it carries `usage`. A JSON body is read only once it has ended,
so the service holds back what completes it (its last chunk, and any whitespace after that) until
it has the ids, with a `Content-Length` or chunked alike. Whitespace before the body goes through at
once, so a gateway that keeps a slow request alive with whitespace still reaches the client. So the id resolves before the client can
run the call, with one exception: a response the upstream content-encoded although the proxy asked
for identity is read from a decoded copy, which can trail what the client has decoded. The MCP
server covers that gap by asking again after a 404: six tries in all, the last one started within 2
seconds of the first. Each try has 2 seconds in total to connect and read the whole answer, so a
tool call waits at most 4 seconds for the service. A response
the service cannot read (an encoding it cannot decode, a JSON body over its limit) registers
nothing, and neither does the response to a request forwarded unrouted; those calls resolve as
unknown.

An id that two sessions registered is ambiguous: it resolves as unknown from then on, and
`serve.log` records `tool_use_ambiguous` with a count, never the id. Anthropic's ids are random; an
upstream that repeats ids makes those calls fail rather than reach another conversation's store.

Bodies are JSON and at most 64 KiB. Every other path under `/<token>/contexto/` is a 404.

## Files

All under `$CONTEXTO_HOME`, all mode 0600:

| File | Holds |
|---|---|
| `serve.token` | The token: one line of ASCII. |
| `serve.lock` | Empty. The service holds an exclusive lock on it from before it binds its port until it exits, however it exits. "Running" means this lock is held: `--status`, `--print-settings`, `--rotate-token` and the MCP server decide by it, and send nothing when it is free. |
| `serve.json` | `{"port": N, "pid": N}`, written once the service listens and removed before it closes its port. A killed service leaves it behind; it is believed only while the lock is held, and the next service removes it before it binds. It is read only if it is the user's own regular file with no group or other permission bit. |
| `serve.log`, `serve.log.1` | The service log: JSON lines with event names, reasons, counts, request paths without the token, and status codes. Never header values, request or response bodies, the token, or the upstream URL. At 5 MB `serve.log` is renamed to `serve.log.1`, replacing the last one. |
| `sessions/<id>/` | One session store per conversation, the same files as under `contexto run`. |

## Connections before the token check

Everything the service answers before a request has passed its checks is one 403, byte for byte:
a request it cannot parse (a malformed or over-long request line, no HTTP version, an unknown
version, too many headers, an unsupported method) gets it too, with nothing of what was sent echoed
back and no server or library version. `Expect: 100-continue` is answered only once the checks
pass.

Every refusal and every failure before the check is logged through one limit: by its reason only
(`origin`, `host`, `token`, `malformed`, `error`, `deadline`, `connection_cap`,
`connection_error`, `accept_error`), never its path, and at most once a second; the ones in between
are counted on the next line.

A connection that has not passed the token check has 5 seconds in total to send its request head,
and to send the body of a refused request, which is read and dropped; then the service stops
reading from it. At most 64 such connections are open at once. One more takes the place of the
oldest of them whose request line has not shown the token, which is closed unanswered; a client
sends its request line as soon as it connects, and the service checks it for the token without
reading it, so a connection whose request line has arrived with the token is not the one closed.
One whose request line arrives in pieces, or late, can be (see [Limits](#limits)). A connection that passed
the check is outside both limits for the rest of its life, so a long streamed response is not cut.

## Guards

- **Port taken.** One line and exit 1.
- **A service already runs for this home.** One line naming its port and process id, exit 1.
- **Upstream is the service itself.** Refused, as described under [Upstream](#upstream).
- **Cloud-provider switches.** Claude Code with `CLAUDE_CODE_USE_BEDROCK`,
  `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_MANTLE` or
  `CLAUDE_CODE_USE_ANTHROPIC_AWS` set sends nothing to `ANTHROPIC_BASE_URL` (measured on 2.1.322),
  so such a client never reaches the service. The service cannot see its clients' environments. It
  warns, at start and in `--print-settings`, when the check `contexto run claude` refuses a launch
  by finds one of them on: in its own environment or in a Claude Code settings file read from the
  directory it runs in (the user's, the project's, the managed one), with a settings file it
  cannot read counted as on. It warns and does not refuse, since its clients start elsewhere.
- **Host and Origin.** A request must name `127.0.0.1` or `localhost` in `Host` and carry no
  `Origin`, or it gets the 403, before the token is looked at.
- **Upstream addresses.** The check that the upstream is not the service itself knows the other
  spellings of a loopback address (`127.1`, `2130706433`, `0x7f.1`, `[::1]`, `localhost`). A name
  that only DNS maps to loopback is not looked up.

## Limits

- Loopback only, for one user. The service is not meant to be shared between accounts on a machine
  or reached from another machine.
- Claude Code's Messages API only. A Codex `/responses` request, a Chat Completions request, and
  Bedrock or Vertex request paths are forwarded unchanged if they arrive; they are not bounded.
- Claude Code session ids are routing keys, not a boundary. A client with the token reaches any
  conversation's store by sending that conversation's session id, or by naming it as a parent.
- The token is sent to whatever listens on the port. If the service is down and another local
  process takes port 7837, a client that still has the URL sends that process the token, its
  requests and its API credentials. Stop clients before you stop the service for long, or rotate
  the token afterwards.
- Another local user, without the token, cannot keep clients out by holding connections open: a
  process that held 64 silent connections and reopened each one the service closed got 40 of 40 of
  a token holder's health requests answered (median 0.4 ms; macOS arm64, 2026-11-13). Opening
  connections as fast as it can still costs clients attempts: the system's listen queue (128 on
  macOS) overflows and resets some new connections before the service sees them, and a client whose
  process does not send its request line before 64 newer connections arrive, or sends it in pieces,
  can be closed to make room (with the cap held full, a gap of 2 ms between the pieces was answered
  25 of 25 times, 5 ms 15 of 25, 30 ms 1 of 23). Measured with a health request every 0.4 s for 16 s: one flooding process (about 6,000
  connections a second), 40 of 40 answered; two (about 8,000 a second), 30 of 40; six (about 21,000
  a second), 10 of 40. A client sees a reset connection and can retry. They cannot read, spend or
  write through the service.
- A new Claude Code session id is a new Contexto session. `/clear` starts a new session id, so
  what was paged out or pinned before it stays in the earlier store (`contexto search --session
  <id>`, `contexto state --session <id>`), where under `contexto run` the launch's one store keeps
  serving. `/compact` and `--resume <id>` keep the id.
- A session the service makes records no working directory when the service makes it: the service
  does not run in the client's directory. Pinned file sources of such a session are not checked for
  staleness, and `contexto run claude --continue` does not find it by directory. `contexto run claude
  --resume <id>` does find it, and records the directory it runs in.
- A session the service makes records `project_memory: false`, so project memory
  ([project-memory.md](project-memory.md)) carries nothing into it and nothing it pins into a later
  session, also after `contexto run claude --resume <id>`: the service routes by session id and
  cannot know a client's directory. A session the launcher made keeps its own setting when the
  service serves it.
- `contexto mcp --serve` relies on `_meta["claudecode/toolUseId"]`, measured on Claude Code 2.1.322
  and not documented as a stable interface. A client that does not send it gets the "no tool-use
  id" error on every call. Codex does not send it.
- Measured with API-key clients on macOS: `claude -p` on Claude Code 2.1.322, and the Python Agent
  SDK 0.2.182 with the Claude Code it bundles (2.1.325), against a fake upstream (see
  [Recipes](#recipes)). Not run: a claude.ai (OAuth) login, the IDE extensions, the desktop app,
  the TypeScript Agent SDK, and the GitHub Action.

## Recipes

Each recipe below is run as written by `tests/test_serve_recipes.py`, which takes it from this
page, at zero spend: a fake upstream on `127.0.0.1` answers with a `contexto_pin` tool call, and the
test checks that every request reached the store of its own Claude Code session id and that the
pin landed in that store and no other. What each test does not cover is said under its recipe.

### The Claude Agent SDK (Python)

The SDK starts its own Claude Code process for every `query()`. Give that process the two files
`--print-settings` and `--print-mcp-config` print, with the service running:

<!-- recipe: sdk-files -->
```sh
umask 077
contexto serve --print-settings > ~/.contexto/serve-settings.json
contexto serve --print-mcp-config > ~/.contexto/serve-mcp.json
```

Then pass them as `settings` (Claude Code's `--settings`: the base URL with the token, the hint
headers, the allow rules for Contexto's tools) and `mcp_servers` (`--mcp-config`: `contexto mcp
--serve`):

<!-- recipe: sdk-program -->
```python
import asyncio
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

CONTEXTO = Path.home() / ".contexto"  # or your $CONTEXTO_HOME

options = ClaudeAgentOptions(
    settings=str(CONTEXTO / "serve-settings.json"),
    mcp_servers=CONTEXTO / "serve-mcp.json",
)


async def main() -> None:
    async for message in query(prompt="Summarize the README of this repository.", options=options):
        if isinstance(message, ResultMessage):
            print(message.result)


asyncio.run(main())
```

The token stays in the settings file, out of the program's source. Each `query()` is a new
conversation with its own session id, so it gets its own store. The SDK's requests name the entrypoint
`sdk-py` in their `User-Agent`, and the engine treats them as it treats `claude -p`.

Tested with `claude-agent-sdk` 0.2.182 and the Claude Code it bundles (2.1.325), on macOS: two
runs of the program, two stores, each with its own pin. Not run: the TypeScript SDK, and an SDK
pointed at another Claude Code with `cli_path`.

### The VS Code extension

The extension has no `--settings` flag. Its Claude Code reads the user settings file
(`~/.claude/settings.json`, which the extension shares with the CLI) and the MCP servers saved at
user scope. Close VS Code, start the service, then merge the printed settings into that file
(keeping what is there and making it mode 0600, since it now holds the token) and add the MCP
server at user scope. The user settings file applies to every Claude Code on the machine, so this
also turns on Claude Code's gateway hint headers for a plain `claude` run outside the service: in a
repository whose own settings name another base URL, that `claude` sends the hints there with no
Contexto proxy to remove them. Undo the merge (below) if that matters to you.

<!-- recipe: vscode-setup -->
```sh
umask 077
contexto serve --print-settings | python3 -c '
import json, os, pathlib, sys
add = json.load(sys.stdin)
path = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude") / "settings.json"
settings = json.loads(path.read_text()) if path.exists() else {}
settings.setdefault("env", {}).update(add["env"])
allow = settings.setdefault("permissions", {}).setdefault("allow", [])
allow += [rule for rule in add["permissions"]["allow"] if rule not in allow]
path.parent.mkdir(parents=True, exist_ok=True)
path.touch(mode=0o600)
path.chmod(0o600)
path.write_text(json.dumps(settings, indent=2) + "\n")
'
claude mcp remove --scope user contexto 2> /dev/null || true
claude mcp add-json --scope user contexto "$(contexto serve --print-mcp-config |
  python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)["mcpServers"]["contexto"]))')"
```

Running it again changes nothing. It needs the standalone `claude` on `PATH` (the extension's own
copy is not), and it replaces a user-scope MCP server already named `contexto`. Make sure neither
the extension's `claudeCode.environmentVariables` setting nor the environment VS Code starts in
sets `ANTHROPIC_BASE_URL`, so nothing competes with the settings file.

From then on every Claude Code that reads these files goes through the service, the terminal's
`claude` included, and fails to connect while the service is down. `contexto run claude` then needs
`--upstream URL` (see [Upstream](#upstream)). To undo it, remove the `ANTHROPIC_BASE_URL`,
`CLAUDE_CODE_GATEWAY_HINT_HEADERS` and `CONTEXTO_HINT_HEADERS` entries and the five
`mcp__contexto__` allow rules from the settings file, and run
`claude mcp remove --scope user contexto`.

Not tested in VS Code. The test runs the commands above and then the real `claude -p` with nothing
but the user's files (no `--settings`, no `--mcp-config`), and checks that its requests and its pin
reach their own store. The extension starts its own copy of Claude Code, whose version is the
extension's, so check it once by hand:

1. Start the service (`contexto serve --upstream https://api.anthropic.com`) and run the commands
   above with VS Code closed.
2. Open VS Code and a new conversation in the Claude Code panel. Type `/mcp`: `contexto` is listed
   as connected.
3. Send a message. `contexto serve --status` shows one open session, and `contexto sessions list`
   a new session. `~/.contexto/serve.log` has no `unrouted` line for it.
4. Ask the model to pin a decision (for example "pin the decision: we use tabs") and allow the call.
   `contexto state --session <the new session>` shows the pin, and the service's stderr shows no
   warning that Contexto's MCP tools are missing.

### `claude -p` in CI

A GitHub Actions job that starts the service, runs `claude -p` through it and stops it, all in one
step so the service never outlives the step:

<!-- recipe: ci-workflow -->
```yaml
jobs:
  review:
    runs-on: ubuntu-24.04
    env:
      CONTEXTO_REF: main  # pin a tag or commit that has contexto serve
    steps:
      - uses: actions/checkout@v4
      - name: Install Contexto and Claude Code
        run: |
          pipx install "git+https://github.com/VentureForgre/contexto@${CONTEXTO_REF}"
          npm install --global @anthropic-ai/claude-code@2.1.322
      - name: Run claude -p through contexto serve
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          export CONTEXTO_HOME="$RUNNER_TEMP/contexto"
          umask 077
          contexto serve --port 0 --upstream https://api.anthropic.com 2> "$RUNNER_TEMP/contexto-serve.log" &
          serve_pid=$!
          trap 'kill -TERM "$serve_pid" 2> /dev/null && wait "$serve_pid"' EXIT
          for _ in $(seq 50); do
            contexto serve --status > /dev/null && break
            sleep 0.2
          done
          contexto serve --status
          contexto serve --print-settings > "$RUNNER_TEMP/serve-settings.json"
          contexto serve --print-mcp-config > "$RUNNER_TEMP/serve-mcp.json"
          claude -p --settings "$RUNNER_TEMP/serve-settings.json" \
            --mcp-config "$RUNNER_TEMP/serve-mcp.json" --strict-mcp-config \
            "Review the changes on this branch and list the risks."
```

`CONTEXTO_HOME` under `$RUNNER_TEMP` gives each job its own home, token and stores, removed with the
job's temporary directory, so jobs that share a self-hosted runner never meet. `--port 0` takes a
free port, and `--print-settings` names the port of the service that runs for this home. The loop
waits until the service answers; the `trap` stops it with SIGTERM when the script ends, whether
`claude -p` succeeded or not, and the step fails if `claude -p` does. `--upstream` is explicit
because a checked-out repository's `.claude/settings.json` could name another base URL. The
service's stderr is in `$RUNNER_TEMP/contexto-serve.log`. Do not upload `$CONTEXTO_HOME` as an
artifact: it holds the token, and the stores hold the conversation. Set `CONTEXTO_REF` to a tag or
commit, not a branch, once a release has `contexto serve`. Do not run the step on code from an
untrusted pull request: Claude Code runs with the API key in that checkout.

The test runs the step's `run` script with the real `claude` (2.1.322) under the shell GitHub uses
(`bash --noprofile --norc -eo pipefail`), with the upstream pointed at the fake upstream and a stub
for the secret, and checks that the service stopped and removed `serve.json`. The install step and
GitHub's runner were not run.
