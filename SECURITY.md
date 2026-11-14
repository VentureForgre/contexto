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
  With project memory (D69) a session also reads other sessions, without a lock and without
  changing them: the `meta.json` of every session under `sessions/`, to find the sessions of its
  project, and the `ledger.jsonl` of its project's sessions created before it. Both are opened as
  regular files only, never through a symlink. `contexto project forget` writes the `meta.json` of
  the sessions the user names (and creates their empty lock files if they are missing); a running
  session never writes to another session. Reports of a session reading or writing a session
  of another project, or of carried text reaching the model from anything other than a pin made in
  the main conversation, are in scope.
- **The MCP server** (`contexto mcp`), which the CLI starts over stdin and stdout; it opens no
  network socket. It reads the session's store and serves `contexto_search`, `contexto_recall`,
  and the ledger tools (`contexto_pin`, `contexto_close`, `contexto_state`). It reads the earlier
  sessions of its session's project as the session store item above describes, and writes only to
  its own session. Reports of it reading outside its session and those earlier sessions, writing
  outside its session, or crashing on crafted input in a way that affects the store, are in scope.
- The launchers (`contexto run claude`, `contexto run codex`), including what they pass to the CLI
  and which Python runs the MCP server.

Out of scope: the upstream model API and any proxy in front of it, Claude Code and Codex
themselves, other local processes running as the same user (they can already read that user's
files), and Windows, which is not supported.

## What the session store holds

The store keeps **raw conversation text**: your prompts, the model's replies, tool calls, tool
outputs, and any file contents or secrets that passed through the conversation. These files under
`sessions/<id>/` hold conversation text:

- `pages/`: every turn the proxy paged out, word for word, one file per turn.
- `retained.json`: the recent turns still in the model's view, word for word, so a restarted proxy
  can resume.
- `epoch.json`: the page index (the opening text of each paged-out turn) and the pinned state, and
  in a new session the records it carries from earlier sessions of its project.
- `ledger.jsonl`: the decisions, obligations and other records the model pinned. Each record has
  an author mark: `model:main` for a pin made while only the main conversation could call a tool,
  `model` otherwise. Only `model:main` records are carried into later sessions.
- `quarantine/`: copies of damaged pages and ledger files that Contexto moved aside. They hold the
  same text as the files they came from.
- `codex-home/` (Codex sessions in API-key mode): Codex's own rollout transcripts. Codex writes
  them itself with its own file modes, not Contexto's 0600; only the 0700 directories above them
  keep other users out.

These hold no conversation text:

- `meta.json`: the absolute path of the working directory the session started in, request
  counters, and the Claude Code session ids the session served (`cli_sessions`). Those ids name
  Claude Code's own transcript files, so they link a Contexto store to those transcripts. Since
  project memory it also holds `project` (the absolute path of the project's root directory),
  `project_memory` (`false` when the session is out of project memory) and `forgotten` (the record
  ids `contexto project forget` took out of what later sessions carry).
- `log.jsonl`: the session log. It records sizes, counts, request paths, status codes, error
  names, hashes, event names, and the upstream source, never prompts, replies or the upstream URL.
- `page.key`: a random key that page ids are derived from.
- `live.lock`, `meta.lock`, `ledger.lock`: empty lock files.

`$CONTEXTO_HOME` itself holds `affinity.salt`, a random value that the `X-Session-Affinity` header
is hashed with, so the header does not reveal the session id, and `choose.lock`, an empty lock file
that lets one launch at a time pick a session automatically.

Treat `$CONTEXTO_HOME` like your shell history or the CLI's own transcripts. File modes protect it
from other users on the machine, not from processes running as you, backups, or disk images.
To delete a session's text, delete the whole session directory, not only `pages/`: `retained.json`,
`epoch.json` and `ledger.jsonl` hold copies. `contexto sessions prune` deletes whole session
directories; Contexto deletes nothing else on its own. Deleting `$CONTEXTO_HOME` removes every
session and the affinity salt.

## Carried records

A new session starts with the open obligations and decisions the model pinned in earlier sessions
of the same project. That is model-written text replayed into another session, and an earlier
session may have read untrusted content (a web page, a file, a tool result) that persuaded the
model to pin something. The defence and what it does not cover are in
[docs/project-memory.md](docs/project-memory.md#trust) and in D69 of
[docs/DECISIONS.md](docs/DECISIONS.md). In short: carried records are shown to the model as records
from an earlier session, not as instructions; only records pinned in the main conversation are
carried, never page text; and you can see them (`contexto project show`) and remove them
(`contexto project forget`), or turn project memory off with `--no-project-memory`.
