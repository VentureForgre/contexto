# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (GitHub private vulnerability reporting, which creates a draft security
advisory visible only to you and the maintainers). Do not open a public issue or pull request for a
security problem.

Include the Contexto version (`contexto --version`), the operating system and Python version, the
CLI and its version (Claude Code or Codex), and the steps that show the problem. Leave real session
data, API keys, and conversation text out of the report; a synthetic reproduction is enough.

Fixes ship as a new release; the advisory names the first fixed version. Only the latest release
gets fixes.

## Scope

In scope:

- **The local proxy.** It listens on `127.0.0.1` only, on a free port chosen at start. It forwards
  the CLI's auth headers upstream unchanged and must never store or log them. Reports of the proxy
  binding to another interface, leaking auth headers or conversation text into its logs, or sending
  requests anywhere other than the configured upstream are in scope.
- **The session store** under `$CONTEXTO_HOME` (default `~/.contexto`). Every directory Contexto
  creates there is mode 0700 and every file 0600, whatever the umask. The temporary `--settings`
  and `--mcp-config` files the launcher writes are 0600, the temporary Codex home is 0700, and both
  are deleted on exit. Any path that creates store data with wider modes, follows a link out of the
  store, or lets a page id from outside read a file that is not a page is in scope.
- **The MCP server** (`contexto mcp`), which the CLI starts over stdin and stdout. It reads the
  session's store and serves `contexto_search`, `contexto_recall`, and the ledger tools
  (`contexto_pin`, `contexto_close`, `contexto_state`). Reports of it reading or writing outside its
  session, or crashing on crafted input in a way that affects the store, are in scope.
- The launchers (`contexto run claude`, `contexto run codex`), including what they pass to the CLI
  and which Python runs the MCP server.

Out of scope: the upstream model API and any proxy in front of it, Claude Code and Codex
themselves, other local processes running as the same user (they can already read that user's
files), and Windows, which is not supported.

## What the session store holds

The store keeps **raw conversation text**. Every turn the proxy pages out is written word for word
to `sessions/<id>/pages/`, including your prompts, the model's replies, tool calls, tool outputs,
and any file contents or secrets that passed through the conversation. The ledger
(`ledger.jsonl`) holds the decisions and obligations the model pinned. Only the session log
(`log.jsonl`) is content-free: it records sizes, counts, and event names.

Treat `$CONTEXTO_HOME` like your shell history or the CLI's own transcripts. File modes protect it
from other users on the machine, not from processes running as you, backups, or disk images.
Delete a session's directory to delete its history; Contexto never deletes it for you.
