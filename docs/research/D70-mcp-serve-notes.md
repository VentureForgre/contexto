# D70, the MCP side: `contexto mcp --serve`

Written 2026-11-13 (UTC) on branch `claude/mcp-serve`, for the maintainer who combines this branch
with the `contexto serve` branch. Two texts below are meant to be folded in as they stand: a section
for the D70 entry of `docs/DECISIONS.md` and a paragraph for the MCP server item of `SECURITY.md`.
The rest says what the service side must match and what is not confirmed. The measurements this
relies on are in [D70-serve-probes.md](D70-serve-probes.md) (Claude Code 2.1.322, macOS arm64, zero
spend); the code is `src/contexto/serveclient.py`, `ServeServer` in `src/contexto/mcp.py` and
`src/contexto/commands/mcp.py`; the tests are `tests/test_mcp_serve.py` and
`tests/test_mcp_serve_real_client.py`.

## For the D70 entry of docs/DECISIONS.md

  - **The MCP server for the service's clients: `contexto mcp --serve`.** A client `contexto serve`
    serves is not launched by Contexto, so its MCP server is registered once in the user's Claude Code
    configuration and shared by every conversation: it cannot be started with `--session S`.
    `contexto mcp` now takes exactly one of `--session S` (unchanged) and `--serve`; both or neither
    is a usage error. With `--serve` the server starts with no session, and answers `initialize`,
    `server/discover`, `ping` and `tools/list` with no session and with no service running, since a
    CLI that cannot list a server's tools reports nothing: the same five tools always list.
  - **The session of a call comes from its tool-use id.** Measured on Claude Code 2.1.322: every
    `tools/call` carries `params._meta["claudecode/toolUseId"]`, exactly the `tool_use` id of the
    model response, for main and subagent calls, two calls in one turn, after `/clear` and after
    `--resume`. The service saw that response, so it knows the session. The server takes the id (a
    string of 1 to 256 characters), reads `serve.json` and `serve.token` in `$CONTEXTO_HOME`, and
    sends `POST /<token>/contexto/resolve` with `{"tool_use_id": ...}` to `127.0.0.1:<port>`. The
    session id in the answer must be a valid session id naming an existing session directory that is
    not a symlink; the server opens that store and runs the existing tool code against it, so replies,
    recall budgets and the session log are those of `--session` mode. A 404 is asked again five times
    within 2 seconds, because Claude Code can run a tool while the service is still reading the end of
    the response that carried the call; nothing else is retried.
  - **Why not the alternatives.** `CLAUDE_CODE_SESSION_ID` is in the server's environment, but it is
    set once at spawn: `/clear` starts a new session id in the same Claude Code process, the server
    process survives it and is told nothing, so the variable then names the conversation before the
    `/clear`. `initialize` carries no session id. A hook does get the current id (`SessionStart`
    fires again on `/clear`, `PreToolUse` carries `session_id` and `tool_use_id`), but a hook is a
    second thing to register in the user's settings, no hook runs with `--bare`, what a `SessionStart`
    hook writes to `CLAUDE_ENV_FILE` never reaches the MCP server's environment, and the id a hook
    gets is Claude Code's, which still has to be mapped to a Contexto session by the service. An HTTP
    MCP endpoint on the service gets no more: over HTTP the Claude Code session id is in no header and
    no body, `${CLAUDE_CODE_SESSION_ID}` in a configured header is not expanded, `headersHelper` runs
    once per Claude Code process and not after `/clear`, and `tools/call` carries the same two `_meta`
    keys. It would need the same tool-use id lookup, would put recalled conversation text and ledger
    writes on a listening socket, and would list no tools while the service is down.
  - **It fails closed.** A call whose session cannot be established gets a tool error (`isError`
    true) and no store is opened, read or written. There is no fallback to another session, to
    `CLAUDE_CODE_SESSION_ID`, or to the newest session: a pin written to another conversation's
    ledger stays in that conversation's pinned state, which is worse than a tool that says it did not
    run. The errors are distinct and say what to do: the call carries no tool-use id (the client
    must send it; measured on Claude Code 2.1.322); the service is not running or its two files
    cannot be read (start `contexto serve`); the service does not know the id (the model request may
    not have gone through the service: check `ANTHROPIC_BASE_URL`, and when one of
    `CLAUDE_CODE_USE_BEDROCK`, `_VERTEX`, `_FOUNDRY`, `_MANTLE`, `_ANTHROPIC_AWS` is set in the
    server's environment the message names it, since Claude Code then sends nothing to
    `ANTHROPIC_BASE_URL`); the service refused the token; the service's answer is outside the
    contract (another version, or another program on the port); the session it named cannot be
    opened. A session whose `live.lock` is damaged gets the D59 reply naming the file.
  - **Open stores.** The server keeps the 8 most recently used stores open, each with the search
    text budget `--session` mode gives its one store, and releases an evicted one, which drops its
    `live.lock` hold so `contexto sessions prune` sees the session as idle again. A kept store is
    checked at every call, so a session deleted meanwhile is refused and not recreated.
  - **The `tools/list` record.** `--session` mode records `mcp_listed_at` in the store when it
    answers `tools/list`, and the missing-tools check words its warning by it. In `--serve` mode no
    store is known at `tools/list`, so the record is written when a store is opened for its first
    resolved call. It is later than the listing but never wrong: a client that calls a tool has the
    tools. Until a conversation's first Contexto call the check finds no record.
  - **Tests.** 89 tests against a fake service on 127.0.0.1 and real `SessionStore`s
    (`tests/test_mcp_serve.py`), with the retry's clock and sleep injected, and 2 against the real
    Claude Code 2.1.322 at zero spend (`tests/test_mcp_serve_real_client.py`, skipped in CI): the
    client lists the five tools from `contexto mcp --serve` with no service and no Contexto home at
    all; and with a stub upstream that answers with a `contexto_pin` call, Claude Code hands the
    server that call's id, the fake service is asked for exactly that id once, and the pin lands in
    the session it named and in no other.

## For the MCP server item of SECURITY.md

Suggested replacement for the item (the first sentence changes: with `--serve` the server does open
a socket, as a client):

- **The MCP server** (`contexto mcp`), which the CLI starts over stdin and stdout. It listens on no
  socket. With `--session` it opens none and serves one session's store. With `--serve` it serves the
  sessions of a running `contexto serve`: for each tool call it reads `serve.json` and `serve.token`
  in `$CONTEXTO_HOME` (never through a symlink) and sends one request to the service on `127.0.0.1`
  that carries the call's tool-use id and no conversation text; the service requires the token, which
  goes in that request's path and is never written to stderr, a tool result or a log. A call whose
  session the service does not name, or names but that is not an existing real session directory, is
  refused: the server never falls back to another session and never creates one. A token file that
  is not one line of URL-safe ASCII is not sent, and nothing is sent when `serve.json` names a
  process that is no longer running. Both modes serve `contexto_search`, `contexto_recall`, and the
  ledger tools (`contexto_pin`, `contexto_close`, `contexto_state`). Reports of the server reading or
  writing outside the session of the call, connecting anywhere but the service on `127.0.0.1`,
  disclosing the token, or crashing on crafted input in a way that affects the store, are in scope.

`serve.json` and `serve.token` would also belong in the paragraph that lists what `$CONTEXTO_HOME`
itself holds; that is the service's text to write.

## What the service side must match

`src/contexto/serveclient.py` holds the client's half of the contract. To reconcile when the
branches are combined:

- Names: `SERVE_FILE = "serve.json"`, `TOKEN_FILE = "serve.token"`, `RESOLVE_PATH =
  "/contexto/resolve"`. The service should import these or define the same values.
- `serve.json` must be a JSON object with an integer `port` (1 to 65535), at most 4096 bytes. `pid`
  is optional to the client; when it is a positive integer and no such process runs, the client
  treats the service as not running and sends nothing. A service that writes a `pid` other than its
  own listening process's (a supervisor's, say) would be reported as not running if that process
  exits.
- `serve.token` must be one line of `[A-Za-z0-9._~-]`, 1 to 512 characters, with an optional final
  newline, at most 4096 bytes. `secrets.token_urlsafe` and hex tokens fit. Anything else is refused
  unsent, so that the token is always one path segment.
- The 404 body must be a JSON object whose `error` is `"unknown_tool_use"`. A 404 with any other
  body is reported as an answer outside the contract and is not retried, because a stale `serve.json`
  whose port another HTTP server took over answers 404 too.
- The 200 body must be a JSON object with a non-empty string `session`, at most 65536 bytes.
- The request has `Host: 127.0.0.1:<port>`, `Content-Type: application/json`, `Connection: close`,
  and no `Origin`. Timeout 2 seconds for the connection and each read.
- The service should resolve a `tool_use` id as soon as it has read the block's start from the
  upstream response, before forwarding it, so the lookup does not depend on the retry. The retry
  covers 1.9 seconds of waiting at most.
- Whether the service clears `mcp_listed_at` when it starts a session's engine, as `contexto run`
  does, is the service's choice; the record this server writes is always newer than the response
  that carried the call.

## Measured, and on what

- Claude Code 2.1.322, macOS arm64, API-key mode against stub upstreams, 2026-11-12 and 13: the
  facts quoted above from the probe note (sections 1 to 4, 6, 7 and 9 of its MCP half).
- This branch, same client and machine, zero spend: `claude -p --bare --strict-mcp-config` with
  `contexto mcp --serve` as its only server lists the five tools with no service running; a
  `contexto_pin` call from the stub upstream reaches the server with its tool-use id, and the model
  request that follows carries the call's `structuredContent` (`{"record_id": ...}`) as the tool
  result.
- The import cost for `--session` mode is unchanged in kind: `contexto.mcp` imports
  `contexto.serveclient`, which imports `http.client` only when it sends a request.

## Not confirmed

- The whole path with the real `contexto serve`: this branch has only a fake of the resolve
  contract. In particular the size of the race between the service reading a response and Claude
  Code running its tool was not measured, so whether the retry is ever needed is unknown.
- Clients other than the `claude` CLI: the VS Code extension and the Agent SDK were not run. They
  are expected to send the same `_meta` key because they run the same engine; that is an inference.
  What Codex sends in `_meta` was not measured; the key is Claude Code's own, so a Codex call is
  expected to get the "no tool-use id" error, and `--serve` is documented for Claude Code only.
- `claudecode/toolUseId` is reported from behaviour on one version. No documentation was found that
  makes it a stable interface. If a later version drops or renames it, every call fails with the
  "no tool-use id" error (closed, and the message names the version it was measured on);
  `docs/cli-updates.md` should gain a check for it.
- Not run, as in the probe note: `/resume` inside a running interactive session, `--continue`,
  background subagents, subagents in interactive mode, claude.ai login sessions, Linux.
- Uniqueness of tool-use ids across conversations is the service's assumption to check. Anthropic's
  ids are random; another provider's gateway or a stub can repeat them, and the service then has to
  decide which session an id names, or refuse.
- The provider switch is read from the server's own environment. A switch set only in
  organization-managed settings, or the host-managed provider mode
  (`CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`), was not measured, so the message may fail to name the
  cause there; it still names `ANTHROPIC_BASE_URL`.
- Tool calls are answered one at a time, so while one call waits out its 404 retries (up to 1.9
  seconds) the other calls of the same turn wait behind it.
- With 8 stores open the search text held in memory can reach 8 times the per-store budget (64 MB
  by default). No workload was measured against that.
- As in `--session` mode, a `contexto sessions prune` that deletes a session between the server's
  existence check and its open is not excluded: `SessionStore` then recreates the directory empty
  and logs `pruned_while_opening`.
