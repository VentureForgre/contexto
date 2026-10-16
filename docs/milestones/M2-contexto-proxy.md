# Milestone 2: Contexto proxy (context virtual memory)

Status: contract, not implemented. Owner: intake.lead under D0. Date: 2026-10-16.
Why this design: `docs/research/M0-recommendation.md`. Decisions D16 to D21.

## Outcome

A user runs `contexto run claude [claude args]`. Claude Code works normally, but every model
request passes through a local Contexto proxy that keeps the upstream prompt under a fixed bound.
The session can run far past the bound with no compaction, old turns stay recallable word for
word, and accuracy matches a full-context baseline on the M0 fidelity probes. Codex follows the
same design after Claude Code works.

## Stack

- Python 3.12 or newer, `src/` layout, package `contexto`, console script `contexto`. Runtime
  standard library only. Tests with `pytest` via `uv run --with pytest pytest`. CI on `main` runs
  them (`.github/workflows/ci.yml`).
- Data lives under `$CONTEXTO_HOME` (default `~/.contexto`), one directory per session:
  `sessions/<session id>/`. Nothing in it is committed. Never edit the user's Claude or Codex
  configuration (D0, D16).
- The proxy listens on `127.0.0.1` only. It forwards auth headers upstream unchanged and never
  stores or logs them.

## Architecture

```
contexto run claude ...      (launcher)
  |-- starts proxy on 127.0.0.1:<free port> for session S, upstream = the URL Claude would use
  |-- starts nothing else; Claude Code spawns the MCP server itself from --mcp-config
  `-- execs: claude --settings <tmp file: env.ANTHROPIC_BASE_URL=proxy> \
                    --mcp-config <tmp file: contexto mcp --session S> [user args]

proxy (Anthropic Messages API)        mcp server (stdio, JSON-RPC)
  rewrite engine  ---- store ----       contexto_recall, contexto_pin,
                     (session S)        contexto_close, contexto_state
```

Upstream resolution: the launcher reads the base URL Claude Code would otherwise use (explicit
`--upstream`, else `ANTHROPIC_BASE_URL` in the environment, else the value from the settings
files Claude Code loads, including the main checkout's `settings.local.json` in a worktree, else
`https://api.anthropic.com`). Organization-managed settings still outrank `--settings`; document
that limit.

## Rewrite engine (normative)

Terms: the CLI sends its full local history `M[0..n)` on every request. The proxy keeps an epoch
state `E = (k, pinned_text, index_text, head_fingerprint)`: messages `M[0..k)` are paged out.

1. **Upstream request** = the CLI's `system` and `tools` byte for byte, plus messages:
   `merge(preamble(E), M[k]) , M[k+1..n)`, where `preamble` is a text block holding the pinned
   state and the page index, prepended to the first retained message (always a user message
   that starts a turn, so no `tool_result` is displaced). In the first epoch `k = 0` and the
   preamble is empty. All other request fields pass through unchanged, including
   `cache_control`, `anthropic-beta`, `stream`, and `thinking`.
2. **Thinking.** For every retained message that existed when `E` was created, strip all thinking
   and redacted-thinking blocks, deterministically, on every request, so the transformed prefix is
   byte-identical across requests. Messages added after the rotation pass through untouched.
3. **Rotation** happens only when the last message in `M` is a user message that starts a new turn
   (text, not a `tool_result`), and the estimated upstream input would exceed `bound - headroom`.
   Estimate = last upstream `input_tokens + cache_read + cache_creation` for this session plus the
   bytes added since, at 3.5 bytes per token, refined when real usage arrives. On rotation: choose
   the largest `k` at a turn start such that the retained window is at most `window_target`
   (default 50% of the bound) and the newest full turn is always retained; write `M[old k..k)` to
   the page store as one page per turn; rebuild `pinned_text` and `index_text` from the store;
   strip thinking per rule 2.
4. **Never rotate inside a tool loop** (D17). A turn that grows past the bound is sent as is and
   recorded as over-bound in the session log; the next turn boundary rotates.
5. **Head change.** If the CLI's `system` or `tools` change (byte fingerprint), start a new epoch at
   the next turn boundary; never rewrite them.
6. **Usage.** Return the upstream `usage` unchanged. The CLI therefore sees the bounded prompt's
   real size.
7. **Errors.** Forward upstream errors unchanged. Never retry by silently changing the request.
   Log every 4xx with the epoch state for diagnosis.
8. **Streaming.** Relay SSE events as they arrive; the proxy only reads `message_start` and
   `message_delta` usage. No hold-back is needed because recall is an MCP tool (D19).
9. **Matching history.** Identify `M[old k]` in later requests by a content hash of the canonical
   message, so CLI-side reordering or an unexpected local compaction is detected; on mismatch,
   start a fresh epoch at `k = 0` with a logged warning rather than guess.

Defaults (configurable flags and env): `bound` 200,000 tokens until L2f sets O3; `headroom`
the online running max of per-turn growth plus 25%, floor 25,000; `window_target` 50% of the bound.

## Store (normative)

- **Pages:** `pages/<page id>.json`, one per paged-out turn, holding the exact message JSON of that
  turn (thinking included as received), its turn number, byte and token estimates, and a stub:
  first 200 characters of the user text, tool names used, and file paths mentioned.
  Page id: `pg_` + first 16 hex of SHA-256 over the canonical JSON. Pages are immutable.
- **Ledger:** the M1 record model (kinds `event`, `decision`, `obligation`, `artifact`,
  `reference`, `closure`; epistemic `verified`, `reported`, `summary`, `hypothesis`; provenance;
  supersession; open obligations) harvested from PRs #2 and #3, append-only JSONL with a hash
  chain and crash-truncation repair. Records may cite pages as provenance (`type: page`).
- **Pinned text** = open obligations, active decisions, then verified facts, newest first, each
  with its id, capped at `pin_budget` (default 6,000 tokens). Over budget: list the newest, then a
  count of omitted items and a pointer to `contexto_state`. It always starts with one line telling
  the model that earlier turns are paged out and how to recall them.
- **Index text** = one line per page (id, turn number, stub), newest first, capped at
  `index_budget` (default 4,000 tokens), with a count of older pages omitted.

## MCP server

`contexto mcp --session S`, stdio JSON-RPC per the MCP spec, tools:

- `contexto_recall(page_id)` returns the page's messages rendered as plain text (thinking omitted),
  exactly, up to 32,000 characters per call with `offset` for more.
- `contexto_pin(kind, text, supersedes?, epistemic?, provenance?)` appends a ledger record.
- `contexto_close(obligation_id, note)` appends a closure.
- `contexto_state()` returns the full pinned state without the budget cap.
- `contexto_search(query)`: exact substring search over page stubs and text, returning page ids.
  No embeddings, no similarity ranking (owner: not RAG).

The preamble tells the model to pin decisions and obligations as it makes them and to recall
before relying on a paged-out detail.

## CLI

`contexto run claude [args]`, `contexto run codex [args]` (lane A step 2), `contexto proxy` (run
the proxy alone, for tests), `contexto mcp`, `contexto sessions list|show <id>`,
`contexto recall <page id>`, `contexto state`. Exit codes and `--help` documented in README.

## Lanes

- **Lane A, management.manager-1 (builder-1, builder-2):** launcher, proxy server and SSE relay,
  rewrite engine (rules 1 to 9), upstream resolution, session log; then the Codex adapter
  (Responses API over HTTP through a temporary `CODEX_HOME` whose provider uses `auth.command`;
  force HTTP or handle WebSocket deltas, decided with evidence).
- **Lane B, management.manager-2 (builder-3, builder-4):** package scaffolding first (harvest PR #2),
  store (pages, ledger from PRs #2 and #3, pinned and index text), MCP server, CLI subcommands other
  than `run`, and the acceptance harness below.
- **Interface lane A depends on**, owned by lane B, landed first as a small PR:
  ```python
  class SessionStore:
      def __init__(self, home: Path, session_id: str): ...
      def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]: ...  # page ids
      def pinned_text(self, budget_tokens: int) -> str: ...
      def index_text(self, budget_tokens: int) -> str: ...
      def log(self, event: dict) -> None: ...  # session log, JSONL
  ```
  Until it lands, lane A codes against a stub with the same signatures.

## Acceptance (done means)

All through `contexto run claude` with `--model claude-haiku-5-5` pinned, synthetic tasks,
throwaway processes in a non-git scratch directory, default Claude Code settings, the owner's
proxy as upstream (D12). Token caps: each run states its budget first; intake approves.

1. **Long run:** a scripted session whose local history reaches at least 2.5 times the bound
   (bound set to 100,000 for this run to keep spend down) shows zero compaction events, every
   upstream request under the bound except logged over-bound turns, and zero 4xx responses.
2. **Fidelity:** the L2f harder probes asked at the end of that run, scored against the
   full-context baseline at the same session content. Pass: within 2 probes of baseline overall
   and no category more than 1 probe below.
3. **Recall:** at least one probe answerable only from a paged-out turn is answered correctly,
   and the session log shows the `contexto_recall` call.
4. **Cache:** within an epoch, requests read the head and window prefix from cache, except
   misses explained by the route (report the rate).
5. **Tests:** unit tests for every rewrite rule, the store, and the MCP server; CI green.
6. Intake runs the acceptance on integrated `main` and records the numbers in `STATUS.md`. Then
   intake cuts v0.1.0 (O5).

## Out of scope for M2

Embeddings or similarity search, model-written summaries of paged turns, multi-host sync, any UI,
publishing outside this repository (D0).
