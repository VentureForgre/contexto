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
