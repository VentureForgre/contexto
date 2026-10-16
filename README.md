# Contexto

Contexto is a private project for managing very long-running AI sessions and their context. The repository is the source of truth for the product; the OpenRig team is the persistent development team that will design and build it.

## Mission

Make long-running sessions recoverable and useful after context-window turnover, runtime restarts, and work handoffs. A future implementation should preserve decisions, active obligations, provenance, and enough verified state for a successor to continue without treating a summary as a complete transcript. Keep raw logs, credentials, and private user data out of Git.

Initial design questions for the team:

- How should events, decisions, tasks, artifacts, and references be captured with stable identities and provenance?
- Which context should be retrieved for the next turn, and how is stale or conflicting context detected?
- How are bounded checkpoints built, validated against source material, and handed to a new session?
- How are handoff quality, retrieval precision, long-run continuity, and recovery after interruption tested?

These are product goals, not claims that the functionality is implemented yet.

## MCP server (`contexto mcp`)

`contexto run claude` gives Claude Code an MCP server for the session; you do not start it by hand.
Claude Code spawns `contexto mcp --session <id>` from the launcher's `--mcp-config`, and the server
talks MCP over stdio (one JSON-RPC message per line; only protocol messages on stdout, diagnostics
on stderr; it exits when stdin closes). It accepts both the `initialize` handshake and the newer
per-request `_meta` protocol version with `server/discover`.

| Tool | What it does |
|---|---|
| `contexto_recall(page_id, offset?)` | a paged-out turn, word for word, thinking omitted; 32,000 characters per call, continue with `offset` |
| `contexto_pin(kind, text, supersedes?, epistemic?, provenance?)` | record a decision, obligation, fact, artifact, or reference in the session ledger |
| `contexto_close(obligation_id, note)` | close an open obligation |
| `contexto_state()` | the full pinned state, no size cap |
| `contexto_search(query)` | page ids whose text contains the exact substring, newest first; no similarity search |

Data lives under `$CONTEXTO_HOME` (default `~/.contexto`); `--home` overrides it. Every tool call is
logged to the session log with names, ids, and sizes, never the text.

## Development rig

OpenRig rig: `contexto`. One native Claude Code intake lead (`intake.lead`), two middle-management seats (`management.manager-1`, `management.manager-2`), four builders (`build.builder-1` through `build.builder-4`), and two reviewers (`review.reviewer-1`, `review.reviewer-2`). All nine seats use `claude-opus-5-5` through the existing Claude proxy. Intake, managers, and reviewers use high reasoning; builders use medium. The proxy credential is held outside this repository.

Work flows from intake to managers, from managers to builders, and from builders to independent reviewers. Intake coordinates integration and reports verified outcomes. Each specialist has a separate Git worktree. The durable OpenRig spec lives outside this repository at `~/.openrig/workspace/specs/contexto/rig.yaml`.

```sh
rig ps --nodes --rig contexto --json
rig send 'intake-lead@contexto' 'Describe the next milestone and delegate it through the managers.'
# Attach to the existing OpenRig-owned intake session:
# tmux attach -t 'intake-lead@contexto'
```

Do not commit proxy keys, tokens, local Claude settings, raw private transcripts, or generated runtime state. Contributions should go through pull requests targeting `main`. GitHub branch-rule enforcement depends on the account plan and is verified separately from this repository policy.

## Install and use

Contexto is a Python 3.12+ command-line tool with no runtime dependencies.

```sh
uv tool install .            # or: pip install .
contexto --version
```

Commands for milestone M2 (`docs/milestones/M2-contexto-proxy.md`). `contexto run` and
`contexto proxy` are documented with the proxy, `contexto mcp` in "MCP server" above, and the rest
in "Inspecting sessions" below. Every command accepts `--help`.

| Command | Purpose |
|---|---|
| `contexto run claude [args]` | Run Claude Code through the Contexto proxy |
| `contexto run codex [args]` | Run Codex through the Contexto proxy |
| `contexto proxy` | Run the proxy alone |
| `contexto mcp --session S` | MCP server with recall, search, pin, close, and state tools |
| `contexto sessions list`, `contexto sessions show <id>` | List and inspect sessions |
| `contexto recall <page id>` | Print a paged-out turn |
| `contexto state` | Print the session's pinned state |

Session data lives under `$CONTEXTO_HOME` (default `~/.contexto`) and is never committed.

## Inspecting sessions (`contexto sessions`, `contexto recall`, `contexto state`)

These commands read a session's store under `$CONTEXTO_HOME/sessions/<id>/`. They never create a
session or change its pages, ledger, or log.

**Session selection** for `recall` and `state`: `--session ID`, else the `CONTEXTO_SESSION`
environment variable, else the session with the most recent activity. The chosen session and how
it was chosen are printed to stderr, for example `session 7f3a (most recent)`, so stdout carries
only the page or state text.

### `contexto sessions list [--json]`

One row per session, most recent activity first (ties broken by id): id, created time, page count,
ledger record count (`?` if the ledger cannot be read), and last activity. Times are UTC. `--json`
prints a list of objects with the keys `id`, `created`, `pages`, `records`, `last_activity`.

### `contexto sessions show ID [--json]`

A summary of one session:

- created and last activity times;
- page count, ledger record count, active records, and open obligations;
- how many session-log events, over-bound turns, and upstream 4xx responses were recorded;
- the last epoch state the proxy logged;
- whether the ledger verifies.

`--json` adds the event counts by type.

### `contexto recall PAGE_ID [--session ID] [--offset N]`

Prints a paged-out turn as plain text with thinking omitted, rendered exactly as the MCP
`contexto_recall` tool renders it, 32,000 characters at a time. When more remains, stderr says
`[N more characters: rerun with --offset M]`.

### `contexto state [--session ID]`

Prints the session's full pinned state without the budget cap: open obligations, active
decisions, and verified facts with their record ids. This is the same text as the MCP
`contexto_state` tool. File sources are checked against the session's recorded working
directory; if none is recorded, the output says staleness was not checked.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (an empty session list is a success) |
| 1 | Unknown or invalid session id, unknown page id, an offset past the end of the page, or an unreadable ledger |
| 2 | Usage error: unknown command or option, or a missing argument (`contexto` with no command prints help and exits 2) |

