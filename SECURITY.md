# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (GitHub private vulnerability reporting, which creates a draft security
advisory visible only to you and the maintainers). Do not open a public issue or pull request for a
security problem.

Include the Contexto version (`contexto --version`; if it reports `0.0.0+checkout` with no commit
after it, the commit from `git rev-parse --short HEAD` in the checkout), the operating system and
Python version, the CLI and its version (Claude Code or Codex), and the steps that show the
problem. Leave real session data, API keys, and conversation text out of the report; a synthetic
reproduction is enough.

Fixes ship as a new release; the advisory names the first fixed version. Only the latest release
gets fixes.

## Scope

In scope:

- **The local proxy.** It listens on `127.0.0.1` only, on a free port by default (`contexto proxy
  --port N` lets the user pick the port). It answers only requests whose `Host` names `127.0.0.1`
  or `localhost` and that carry no `Origin` header, so a web page cannot use it; others get 403. It forwards the CLI's auth headers upstream unchanged
  and must never store or log them. Reports of the proxy binding to another interface, leaking
  auth headers or conversation text into its logs, or sending requests anywhere other than the
  configured upstream are in scope.
- **The local service** (`contexto serve`). It listens on `127.0.0.1` only, on a fixed port (7837
  unless `--port N`), and has no option to listen anywhere else. It requires a token on every
  request: 32 random bytes in `$CONTEXTO_HOME/serve.token`, created with mode 0600, carried as the
  first path segment of the base URL a client is given (`http://127.0.0.1:<port>/<token>`). The
  token protects the port: without it a request is not forwarded upstream, does not reach a session
  store, and is not answered by the service's own endpoints (`/contexto/health`,
  `/contexto/resolve`). Before the token, `Host` must name `127.0.0.1` or `localhost` and no
  `Origin` may be present; then the raw first path segment is compared in constant time.
  Everything the service sends before a request has passed these checks is the same 403, byte for
  byte, including the answer to a request it cannot parse: no echo of what was sent, no server or
  library version, and no `100 Continue`. Every refusal and every failure before the check is
  logged by its reason only, through one limit of one line a second with a count of the rest. A
  connection that has not passed the check has 5 seconds in total for its request head and for a
  refused body, and at most 64 such connections are open; one more takes the place of the oldest
  whose request line has not shown the token. The service removes the token before it forwards or
  logs a request, drops a request header whose name or value holds the token, and refuses to start
  if the token file is a symlink, is not a regular file, or has a group or other permission bit
  set. `contexto serve --rotate-token` replaces the token. One service runs per `$CONTEXTO_HOME`: it
  holds an exclusive lock on `serve.lock` while it runs. The commands that talk to it check, right
  before each request they send (a retry included), that a service holds that lock, and take the
  port from a `serve.json` that is the user's own 0600 file; a retry is sent only while
  `serve.json` and `serve.token` still give the port, process id and token of the first try, so a
  service that stops between tries gets no more requests and neither does whatever takes its port.
  The service writes `serve.json` once its port is listening and removes it before it closes the
  port. Claude Code's gateway hint headers, which the settings `--print-settings` prints turn on,
  are read by the engine and removed before a request is forwarded, unless the service was started
  with `--forward-hint-headers`. `serve.log` holds event names, reasons, counts,
  request paths without the token, status codes and whether the hint headers are forwarded, never
  header values, bodies, tool-use ids, the token, or the upstream URL. A client-supplied session id selects a store only through the ids
  recorded in the stores and never names a directory. Claude Code session ids are routing keys, not
  a boundary between holders of the token: a client with the token reaches any conversation's store
  by sending that conversation's session id or naming it as a parent. A tool-use id that two
  sessions registered resolves as unknown. Reports of the service answering without the token, the
  token reaching a log, stderr, an error message or the upstream, a request without the token
  reaching a store, a tool call resolved to a session that did not issue it, or the service
  listening off loopback are in scope. The token is as private as the files it is copied into: a
  settings file that holds the base URL holds the token, so keep it at mode 0600, and if a
  conversation reads such a file, the token reaches the model provider inside that conversation;
  rotate it then. Residual: another local user, without the token, who opens connections as fast
  as it can overflows the system's listen queue, and some of a client's connection attempts are
  reset: measured on macOS with a health request every 0.4 s, 30 of 40 were answered against two
  such processes (about 8,000 connections a second) and 10 of 40 against six (about 21,000);
  holding 64 connections open and reopening each one closed refused none (40 of 40). They cannot
  read, spend or write through the service.
- **The session store** under `$CONTEXTO_HOME` (default `~/.contexto`). Every directory Contexto
  creates there is mode 0700 and every file 0600, whatever the umask; an existing
  `$CONTEXTO_HOME` keeps its own mode, and `sessions/` is narrowed to 0700. The temporary
  `--settings` and `--mcp-config` files `contexto run claude` writes are 0600 and deleted on exit,
  on every signal the launcher can catch, and, if the launcher is killed with SIGKILL, by a
  watchdog process that also ends Claude Code. In API-key mode (the default), `contexto run codex`
  sets `CODEX_HOME` to `sessions/<id>/codex-home` (0700) and writes its `config.toml` there (0600);
  on exit `config.toml` and `packages/` are deleted and the rest is kept, so Codex can resume.
  `$CONTEXTO_HOME` may be a symlink. A session directory may not: every command refuses a
  symlinked `sessions/<id>`, and files inside a session are never opened through a symlink. ChatGPT login mode uses
  your own `CODEX_HOME` with `-c` overrides, and Contexto writes nothing there. Any path that
  creates store data with wider modes, follows a link out of the store, or lets a page id from
  outside read a file that is not a page is in scope.
- **The MCP server** (`contexto mcp`), which the CLI starts over stdin and stdout. It listens on no
  socket. With `--session` it opens none and serves one session's store. With `--serve` it serves the
  sessions of a running `contexto serve`: for each tool call it checks that a service holds the
  home's `serve.lock`, reads `serve.json` and `serve.token` in `$CONTEXTO_HOME` (each the user's own
  0600 regular file, never through a symlink), and sends one request to the service on `127.0.0.1`
  that carries the call's tool-use id and no conversation text; the token goes in that request's
  path and is never written to stderr, a tool result or a log. With no service holding the lock it
  sends nothing, and it checks the lock and the service's files again before each retry of a call. A call whose session the service does not name, or names but that is not an
  existing real session directory, is refused: the server never falls back to another session and
  never creates one, nor fills in a session directory that `contexto sessions prune` has emptied. A
  token file that is not one token is not sent. Both modes serve
  `contexto_search`, `contexto_recall`, and the ledger tools (`contexto_pin`, `contexto_close`,
  `contexto_state`). Reports of the server reading or writing outside the session of the call,
  connecting anywhere but the service on `127.0.0.1`, disclosing the token, or crashing on crafted
  input in a way that affects the store, are in scope.
- The launchers (`contexto run claude`, `contexto run codex`), including what they pass to the CLI
  and which Python runs the MCP server.

Out of scope: the upstream model API and any proxy in front of it, Claude Code and Codex
themselves, other local processes running as the same user (they can already read that user's
files, the `serve` token included), and Windows, which is not supported. For `contexto serve`
also: a token the user copied into a file, environment or command line that others can read; a
process that takes the service's port while the service is not running (a client that still has
the URL then sends it the token and its requests); and use by more than one user or from another
machine, which the service is not built for.

## What the session store holds

The store keeps **raw conversation text**: your prompts, the model's replies, tool calls, tool
outputs, and any file contents or secrets that passed through the conversation. These files under
`sessions/<id>/` hold conversation text:

- `pages/`: every turn the proxy paged out, word for word, one file per turn.
- `retained.json`: the recent turns still in the model's view, word for word, so a restarted proxy
  can resume.
- `epoch.json`: the page index (the opening text of each paged-out turn) and the pinned state.
- `ledger.jsonl`: the decisions, obligations and other records the model pinned.
- `quarantine/`: copies of damaged pages and ledger files that Contexto moved aside. They hold the
  same text as the files they came from.
- `codex-home/` (Codex sessions in API-key mode): Codex's own rollout transcripts. Codex writes
  them itself with its own file modes, not Contexto's 0600; only the 0700 directories above them
  keep other users out.

These hold no conversation text:

- `meta.json`: the absolute path of the working directory the session started in, request
  counters, and the Claude Code session ids the session served (`cli_sessions`). Those ids name
  Claude Code's own transcript files, so they link a Contexto store to those transcripts.
- `log.jsonl`: the session log. It records sizes, counts, request paths, status codes, error
  names, hashes, event names, and the upstream source, never prompts, replies or the upstream URL.
  It also records three labels from fixed word lists: a request's class (`request_class`), what
  triggered a compaction (`compaction`, `context_compacted`), and what the launch does with Claude
  Code's hint headers (`hint_headers`: `stripped`, `forwarded` or `off`).
- `page.key`: a random key that page ids are derived from.
- `live.lock`, `meta.lock`, `ledger.lock`: empty lock files.

`$CONTEXTO_HOME` itself holds `affinity.salt`, a random value that the `X-Session-Affinity` header
is hashed with, so the header does not reveal the session id, and `choose.lock`, an empty lock file
that lets one launch at a time pick a session automatically. `contexto serve` adds `serve.token`
(the token), `serve.lock` (empty; held by the running service), `serve.json` (the running service's
port and process id, removed on a clean shutdown) and `serve.log` with one rotated copy,
`serve.log.1` (at most 5 MB each); all are mode 0600 and none holds conversation text. Every
conversation a `serve` client starts adds a session directory, and the service deletes none:
`contexto sessions prune` does.

Treat `$CONTEXTO_HOME` like your shell history or the CLI's own transcripts. File modes protect it
from other users on the machine, not from processes running as you, backups, or disk images.
To delete a session's text, delete the whole session directory, not only `pages/`: `retained.json`,
`epoch.json` and `ledger.jsonl` hold copies. `contexto sessions prune` deletes whole session
directories; Contexto deletes nothing else on its own. Deleting `$CONTEXTO_HOME` removes every
session and the affinity salt.
