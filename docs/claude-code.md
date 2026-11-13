# Claude Code

`contexto run claude` starts Claude Code through Contexto; the [README](../README.md#quickstart) has
the commands. This page covers resuming a conversation and Claude Code's `/clear`, `/resume` and
`/compact`.

**Resuming.** `contexto run claude --resume <id>` (also `-r <id>` or `-r<id>`) and
`contexto run claude --continue` (also `-c`, `-pc`) reuse the Contexto session that served that
conversation (for `--continue`, the last one used in this directory), so pages, pins and
obligations carry over. `--session ID` always wins. When Claude Code exits, Contexto prints the
command that resumes through it; Claude Code's own hint (`claude --resume <id>`) bypasses Contexto.
`--resume` without an id opens Claude Code's picker, which Contexto cannot follow, so that launch
gets a new session. A session another launch has open is never picked automatically. A
`--fork-session` fork keeps the original's session: both branches share its pages, pins and recall
room, and switching between them re-rotates. Pass `--session NEW` to give the fork its own store.

`/clear`, `/resume` and `/compact` (and auto-compaction) work inside `contexto run`, interactive
and headless. After `/clear` the new conversation is the main one from its first prompt, so pins,
search and recall work there, and `/resume` of the earlier conversation makes that one main again.
This relies on Claude Code's session id, measured on 2.1.302 to 2.1.318.
