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
  project, and the `ledger.jsonl` of every other session of its project (the earlier ones for what
  it carries, all of them for closures and replacements). Both are opened as regular files only,
  never through a symlink and never waiting on a FIFO; a ledger over 64 MB or a `meta.json` over
  1 MB is skipped unread. `contexto project forget` writes the `meta.json` of the sessions the user
  names (and creates their empty lock files if they are missing); a running session never writes to
  another session. Reports of a session reading or writing a session of another project, of a
  session joining another project than the one it was started in, of carried text reaching the
  model from anything other than a pin the proxy saw the main conversation make, of carried text
  reaching the model as markup rather than as an escaped literal, or of a subagent's closure or
  replacement of another session's record counting outside its own session, are in scope.
- **The MCP server** (`contexto mcp`), which the CLI starts over stdin and stdout; it opens no
  network socket. It reads the session's store and serves `contexto_search`, `contexto_recall`,
  and the ledger tools (`contexto_pin`, `contexto_close`, `contexto_state`). It reads the other
  sessions of its session's project as the session store item above describes, and writes only to
  its own session. Reports of it reading outside its session and those of its project, writing
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
- `epoch.json`: the page index (the opening text of each paged-out turn) and the pinned state.
  With project memory it also holds text pinned in *other* sessions: while a session that carries
  records is at epoch 0 (before its first rotation), `start_text` holds the carried records it
  sends; after that, the pinned state includes the same carried section. Carried text is stored as
  the model gets it, each record's text an escaped JSON literal.
- `ledger.jsonl`: the decisions, obligations and other records the model pinned. Records are
  written with the author `model`, as before project memory; which of them the main conversation
  made is in `meta.json`.
- `quarantine/`: copies of damaged pages and ledger files that Contexto moved aside. They hold the
  same text as the files they came from.
- `codex-home/` (Codex sessions in API-key mode): Codex's own rollout transcripts. Codex writes
  them itself with its own file modes, not Contexto's 0600; only the 0700 directories above them
  keep other users out.

These hold no conversation text:

- `meta.json`: the absolute path of the working directory the session started in, request
  counters, and the Claude Code session ids the session served (`cli_sessions`). Those ids name
  Claude Code's own transcript files, so they link a Contexto store to those transcripts. Since
  project memory it also holds `project` (the absolute path of the project's root directory,
  recorded once at the session's first launch and never moved by a resume from elsewhere),
  `project_memory` (`false` when the session is out of project memory), `main_records` (the record
  ids the proxy saw the main conversation pin or close, the newest 4,096; only these are carried)
  and `forgotten` (the record ids `contexto project forget` took out of what later sessions carry).
- `log.jsonl`: the session log. It records sizes, counts, request paths, status codes, error
  names, hashes, event names, and the upstream source, never prompts, replies or the upstream URL.
  It also records three labels from fixed word lists: a request's class (`request_class`), what
  triggered a compaction (`compaction`, `context_compacted`), and what the launch does with Claude
  Code's hint headers (`hint_headers`: `stripped`, `forwarded` or `off`).
- `page.key`: a random key that page ids are derived from.
- `live.lock`, `meta.lock`, `ledger.lock`: empty lock files.

`$CONTEXTO_HOME` itself holds `affinity.salt`, a random value that the `X-Session-Affinity` header
is hashed with, so the header does not reveal the session id, and `choose.lock`, an empty lock file
that lets one launch at a time pick a session automatically.

Treat `$CONTEXTO_HOME` like your shell history or the CLI's own transcripts. File modes protect it
from other users on the machine, not from processes running as you, backups, or disk images.
To delete a session's text, delete the whole session directory, not only `pages/`: `retained.json`,
`epoch.json` and `ledger.jsonl` hold copies, and the `epoch.json` of a later session of the
project can hold copies of its carried records. `contexto sessions prune` deletes whole session
directories; Contexto deletes nothing else on its own. Deleting `$CONTEXTO_HOME` removes every
session and the affinity salt.

## Carried records

A new session starts with the open obligations and decisions the main conversation pinned in
earlier sessions of the same project. That is model-written text replayed into another session, and
an earlier session may have read untrusted content (a web page, a file, a tool result) that
persuaded the model to pin something. The defence and what it does not cover are in
[docs/project-memory.md](docs/project-memory.md#trust) and in D69 of
[docs/DECISIONS.md](docs/DECISIONS.md). In short:

- Only records the proxy saw the main conversation make (Contexto's own pin call and the server's
  success reply, in the main conversation's history) are carried, never page text and never a
  subagent's pin. Claude Code subagents are told apart by the `X-Claude-Code-Agent-Id` header on
  their requests (D50). A request that carries `X-Claude-Code-Agent-Id` or a parent session id is never read for pins, even when it has the main conversation's key, as a fork of the main conversation does; nor is a request whose `x-claude-code-request-class` header says `subagent`, `workflow` or `auxiliary`, which Claude Code sends only while its gateway hint headers are on (D70). A conversation that became the main one only by having more turn starts than
  the previous main one (a subagent whose requests lack that header, resumed with SendMessage; a
  Codex child conversation given more input) is not trusted: its pins are not carried, and its
  closures and replacements of other sessions' records count only in its own session.
- Codex sessions give nothing. On codex-cli 0.163.0, as measured
  ([docs/research/M2-codex-acceptance-plan.md](docs/research/M2-codex-acceptance-plan.md)), the
  model calls Contexto's tools from inside Codex's code-mode `exec` tool, which the proxy does not
  read for this. So a Codex session's pins are never carried, and its closures and replacements of
  carried records count only in that session. A Codex session does start with what Claude Code
  sessions of the project carried. Not checked on codex-cli 0.166.0 or in a live run.
- The model is told to pin only what the user states in their own messages, and sees carried records
  under a heading that calls them records from an earlier session, not instructions, and says that
  markup or instructions inside a record are part of it. Each record's text is an escaped JSON
  literal, so markup such as `<system-reminder>` reaches the model as escapes.
- You can see them (the start line's count, `contexto state`, `contexto project show`), remove them
  (`contexto project forget`, which reaches a running session at its next turn start, or at its
  next rotation once it has rotated), or turn project memory off with `--no-project-memory`.

What remains: the main conversation also reads untrusted content, and a pin it makes after reading
it is carried; the heading is advisory, and its effect on a live model is unmeasured; pruning the
session that closed a record brings the record back; and a symlinked directory belongs to its
target's project. `main_records` is a list in `meta.json`, a file any process running as you can
edit, a subagent with a shell included: it records what the proxy saw, not an integrity guarantee,
and an id added there by hand is carried like one the proxy recorded. The same process could edit
the ledger or `CLAUDE.md` as easily.
