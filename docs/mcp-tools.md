# MCP tools the model gets

[Contexto](../README.md) / [Documentation](README.md)

The five tools Contexto gives the model for reading paged-out turns and keeping pinned state. The
terms used here (bound, headroom, window budget) are defined in the README's
[How it works](../README.md#how-it-works).

`contexto run` registers `contexto mcp --session <id>` with the CLI, which starts it over stdio. You do
not start it by hand.

| Tool | What it does |
|---|---|
| `contexto_search(query)` | Finds turns that contain every word of the query (details below) |
| `contexto_recall(page_id, offset?)` | A paged-out turn word for word, thinking omitted. Each call returns at most 32,000 characters and about a quarter of the window budget (12,000 characters until the proxy has recorded the window), and at most half of the turn's remaining recall budget; continue with `offset` |
| `contexto_pin(kind, text, supersedes?, epistemic?, provenance?)` | Record an event, decision, obligation, artifact, or reference in the session ledger. `epistemic` is `verified`, `reported`, `summary`, or `hypothesis`. Decisions and obligations always show in the pinned state; an event, artifact, or reference shows there only when verified with a provenance source, and the reply says so when it will not |
| `contexto_close(obligation_id, note)` | Close an open obligation, including one carried from an earlier session of the project |
| `contexto_state()` | The full pinned state, with no pin-budget cap, then the records carried from earlier sessions of the project. It is charged against the turn's recall budget and cut at a whole line, with a trailer, when the budget runs out |

**Carried records.** A new session starts with the open obligations and active decisions the model
pinned in the main conversation of earlier sessions of the same project
([Project memory](project-memory.md)). `contexto_close` closes a carried obligation, and
`contexto_pin` with `supersedes` replaces a carried decision; `supersedes` may name a record of an
earlier session only while it is carried. The closure or the new record is written to the current
session's ledger, and later sessions find the record closed or replaced. A pin made while a subagent
or side request has a live recall budget is not carried into later sessions.

**Search.** `contexto_search` finds turns holding every word of the query, in any case and any
order. Words match as substrings, so identifiers and paths work; there is no stemming and no fuzzy
or similarity matching. A query with no words finds nothing. Contexto's own search, recall and
state calls (a turn's earlier search replies, for example) are kept for recall but not searched,
since they copy older pages and would outrank the turns they quote.

- Turns holding the whole query rank first, then by relevance (BM25), with newer turns preferred
  among similar ones. A word counts once per text of a turn (a reply, a tool input, a tool output),
  at most 3 times per turn, and a turn's length does not count, so a long turn or a repeated tool
  dump weighs less. Paged context blocks (`<system-reminder>` text paged on its own) rank after
  every turn.
- The reply starts with matching lines from the conversation still in view (at most 3, newest
  first), skipping the current turn's own user message.
- Then the 3 best matching paged-out turns, newest first (the newest matching turn is always one of
  them), each with its last 3 matching lines in page order and their offsets.
- Then up to 50 more matching turns, newest first, as page id and the offsets of their first and
  last matches (`@first..last`, one offset when they are the same).
- In both parts, paged context blocks come after the turns, under their own line, because a block's
  turn number is when it was paged, not when it was sent. Each reply ends with a note on what was
  searched.

The MCP server holds search text in memory up to `CONTEXTO_SEARCH_CACHE_MB` (64 MB by default): the
newest pages' text as it is and older pages' text compressed. Pages that do not fit are read from the
store at each search, so a larger value makes search in a large store faster and a smaller one uses
less memory. Results do not change.

**Recall budget.** Each turn has a recall budget: the bound minus the tokens the turn already uses
minus the headroom floor. At a turn start the proxy rotates when needed to leave at least 12,000
tokens of it. Search, recall and state replies are charged against it. A subagent or side request
(a request the CLI makes outside the main conversation, such as a title or summary request) in a
tool loop has its own budget, measured from its own request. Because the MCP server cannot tell
which conversation made a call, each call gets the smallest live budget and is charged against all
of them. A subagent's budget closes early when the main conversation's history shows the subagent
stopped, on tested Claude Code versions only; otherwise it expires 15 minutes after its last
response. Tool calls are logged with names, ids, sizes and character ranges, never the text.
