# Sessions

How to tell that Contexto is working, what a session stores on disk, and the commands that read and
delete sessions.

## Is it working?

- **At start**, Contexto prints a session line on stderr, for example
  `contexto: session 7f3a... (bound 150,000, upstream default)` for Claude Code, or
  `contexto: session 7f3a... (bound 150,000, upstream codex-flag, codex auth apikey, engine rewrite)`
  for Codex. The upstream is named by where it came from (for Claude Code `flag`, `env`, `default`,
  or a settings file; for Codex [the labels above](codex.md)), never by its URL.
- **In Claude Code**, `/mcp` lists the `contexto` server as connected.
- **Nothing else is visible until the prompt nears the bound**, about 125,000 tokens with the
  defaults. Then the proxy rotates for the first time.
- **After that**, `contexto sessions list` shows a PAGES count above 0 for the session, and
  `contexto sessions show <id>` gives the counts (pages, pinned records, over-bound turns, upstream
  4xx errors) and lists the paged-out turns. Over-bound turns should stay at 0. The upstream 4xx
  count says how many were on model requests: Claude Code's keyless `HEAD /api/hello` probe gets a
  401 at every launch, and that one is expected.
- **From inside the session**, `contexto state` and `contexto search WORDS` read the running
  session: `contexto run` sets `CONTEXTO_SESSION` for the CLI and its shell commands.
- **If something is wrong**, Contexto prints a `warning:` line, for example when the model has no
  Contexto tools or Claude Code refused them. See [Troubleshooting](troubleshooting.md).

## What is stored and where

Everything Contexto stores is under `~/.contexto`, or `$CONTEXTO_HOME` when set, one directory per
session in `sessions/<id>/`. Directories are mode 0700 and files 0600, so other users on the machine
cannot read them.

The store holds **raw conversation text**: your prompts, the model's replies, tool calls and
outputs, and any file contents or secrets that passed through the conversation. The paged-out turns
are in `pages/`. The recent turns still in view are in `retained.json`, the page index and pinned
state in `epoch.json`, and the pinned records in `ledger.jsonl`. For Codex sessions in API-key mode,
`codex-home/` holds Codex's own transcripts. `meta.json` records the working directory the session
started in and the Claude Code session ids it served. The session log, `log.jsonl`, holds event
names, sizes and counts, never prompt or response text. Auth headers and keys are never stored.

`$CONTEXTO_HOME` must be a directory, and `contexto run` refuses one that contains `${`. An existing home keeps its
permissions; Contexto creates `sessions/` as 0700. A session directory must be a real directory:
Contexto refuses a symlinked `sessions/<id>`. A FIFO or directory in place of a session file is
treated as damage, never waited on. Temporary files left by a crashed process are removed the next
time the session opens, once they are 10 minutes old.

To delete one session's text, delete its whole directory or run
`contexto sessions prune --session <id> --yes`. Deleting only `pages/` leaves copies in the other
files. Deleting `~/.contexto` removes everything. Contexto deletes nothing on its own; old sessions
stay until you prune them ([Removing old sessions](#removing-old-sessions)).

The full file list is in [SECURITY.md](../SECURITY.md#what-the-session-store-holds).

## Inspecting sessions

These commands read a session's store under `$CONTEXTO_HOME/sessions/<id>/` and never change it
(except `sessions prune`, below).
`recall`, `search`, and `state` pick the session from `--session ID`, else `CONTEXTO_SESSION`, else
the most recently active session that has pages or pinned records or that another process holds
open (else the most recent session). A session can be named by its full id (generated ids are 32 hex characters) or
any unique prefix, such as `7f3a`; an ambiguous prefix is an error that lists the matches. They print
the choice to stderr (for example `session 7f3a91c2... (most recent)`), so stdout carries only the
text. A mistyped `--session` is an error, never a new session.

- `contexto sessions list [--json]`: one row per session, most recent activity first: id, created
  time, page count, pinned record count (`?` if the ledger cannot be read), last activity in UTC,
  and size on disk, then the total size. `--json` gives the size as `size_bytes`.
- `contexto sessions show ID [--all] [--json]`: created and last activity times; page, record,
  active record and open obligation counts; session log event, over-bound turn and upstream 4xx
  counts; a counter that goes up at each rotation (`Last epoch`); whether the ledger verifies; then
  the paged-out turns, newest first: page id, turn number, and the opening user text (the 20 newest;
  `--all` for every page). The upstream 4xx count shows how many were on model requests: Claude
  Code's keyless `HEAD /api/hello` probe gets a 401 at every launch. `--json` adds event counts by
  type and `page_list`.
- `contexto search WORDS [--session ID] [--limit N]`: the paged-out turns holding every word, best
  match first (at most 10 unless `--limit`), with the same search the MCP `contexto_search` tool
  uses. Each hit shows its page id, turn and opening text, then its matching lines, each with the
  offset to pass to `contexto recall PAGE_ID --offset N`. It searches paged-out turns only. No match
  is a success (exit 0).
- `contexto recall PAGE_ID [--session ID] [--offset N]`: a paged-out turn as plain text, thinking
  omitted, exactly as the MCP `contexto_recall` tool renders it. Page ids (`pg_` and 16 hex digits,
  for example `pg_1cb04faa431985c5`) come from `contexto sessions show` or `contexto search`. Each chunk is at most
  32,000 characters and about a quarter of the session's window budget, or 12,000 characters until
  the proxy has recorded the window. When more remains, stderr says
  `[N more characters: rerun with --offset M; <how the chunk size was chosen>]`.
- `contexto state [--session ID] [--raw]`: the decisions, open obligations, and verified facts the
  model pinned, uncapped (or "No pinned records."), then how many turns are paged out. `--raw`
  prints the exact text the MCP `contexto_state` tool gives the model, including its instructions to
  the model. It hashes each file a record cites as provenance, inside the session's recorded working
  directory, to mark changed files `STALE`. It never writes there.

### Removing old sessions

Session directories hold the raw conversation and are never removed on their own.
`contexto sessions prune` deletes whole sessions. It is the one `sessions` command that changes the
store.

```
contexto sessions prune --older-than 30d          # list sessions idle for 30 days, with sizes
contexto sessions prune --older-than 30d --yes    # delete them
contexto sessions prune --keep 10 --yes           # keep the 10 most recently active, delete the rest
contexto sessions prune --session <full id> --yes # delete one session
```

- Without `--yes` nothing is deleted: it prints each selected session, its size, and the total.
- `--session` needs the full id. A prefix of existing ids exits 2 and names them, so a mistyped id
  never deletes another session.
- `--older-than` takes a positive whole number and a unit: `s`, `m`, `h`, `d`, or `w`. Age is the
  time since the last activity that `sessions list` shows. When several options are given, a session
  must match all of them.
- A session in use by a running `contexto run`, `contexto proxy`, or `contexto mcp` is skipped, and
  so is any session active in the last 10 minutes. Stop the session first. If you name it with
  `--session`, prune exits 1 with the reason on stderr.
- `sessions/` and each session must be real directories. Prune deletes through open directory
  handles and never follows a symlink, so it does not delete or create anything outside the session
  it checked, even if a path is swapped for a link while it runs.
- If one session cannot be deleted, prune reports it on stderr, goes on with the rest, and exits 1.
- `--json` prints `dry_run`, `total_bytes`, and each selected session with its `status`
  (`would_remove`, `removed`, `skipped`, or `failed`) and `reason`. If prune fails before selecting
  anything (for example an unknown `--session`), it prints `{"error": ...}` instead and exits 1.
- If you resume a session with `contexto run --session ID` while prune is deleting it, the resume
  waits, then starts the session empty and prints a warning.
- A launch that fails before the CLI starts removes the session it created, and `contexto mcp`
  serves existing sessions only, so neither leaves an empty session behind. A session whose CLI
  started but never sent a request is kept, since its log records the launch.
