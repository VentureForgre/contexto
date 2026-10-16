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
  `sessions/<session id>/`. Nothing in it is committed. Everything under `$CONTEXTO_HOME` is
  private to the user: directories `0700`, files `0600`, created atomically (secrets such as the
  page key are written to a temporary file and renamed, never left empty by a race). Never edit the user's Claude or Codex
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

Upstream resolution: the launcher computes the base URL Claude Code would otherwise use, in this
order (measured in M0, PR #16 findings 1 and 1a: a settings-file `env.ANTHROPIC_BASE_URL`
overrides the process environment, and in a git worktree the main checkout's
`settings.local.json` wins): explicit `--upstream`; else the first settings file that sets
`env.ANTHROPIC_BASE_URL`, in Claude Code's precedence (in a worktree the main checkout's
`.claude/settings.local.json` first, then the local, project, and user settings files); else
`ANTHROPIC_BASE_URL` in the process environment; else `https://api.anthropic.com`. The launcher's
own flag-level `--settings` outranks all of these, which is what makes the proxy attachment work.
Organization-managed settings still outrank `--settings`; document that limit.

## Rewrite engine (normative)

Terms. The CLI sends its full local history `M[0..n)` on every request. Measured by
reviewer-2 with zero-spend stubs on Claude Code 2.1.301 (PR #20 review comments):
`role: "system"` messages appear inside `messages`, including as the last message of a new-turn
request, and can carry `cache_control` (their kinds, such as tool additions and effort changes,
are vendor-documented); injected context blocks (CLAUDE.md and similar) ride in a turn-start user
message, `messages[0]` in a fresh session but the resumed turn's user message after `--resume`;
and a system message with empty `content` can appear. Lane A resolved that last item (rule 12; measured by builder-2, reported by manager-1, pending
review, evidence to land in the A2 PR):
it is a settings-only message, `{role: system, content: [], output_config: {effort: ...}}`, seen at
`messages[1]` only when a session file lacking the CLI's own system record is resumed; with real
CLI-written history, including a tool loop and two resumes, the original system message is never
emptied, only its form changes (one text block with `cache_control` becomes a plain string).

- A **turn-start message** is a user message whose content includes user-authored text and no
  `tool_result` block.
- A **turn** is a turn-start message plus every following assistant, `tool_result` user, and
  `role: "system"` message up to the next turn-start message.
- **Carried content** is never paged, except under the bounded fallback below: the injected
  context blocks of any turn-start message (everything but the user's own prompt text), and the
  `role: "system"` messages that the classification below marks as carried. All carried content is
  forwarded in the CLI's current form, never restored from a stored copy.
- **Classification of `role: "system"` messages**, deterministic and in this order:
  1. **Session-start**: content and settings fields such as `output_config` together (measured:
     the session-start message has `output_config`, hook messages do not; position is not used,
     so resumes of hand-written session files classify correctly). Carried.
  2. **Settings-only**: empty content plus settings fields. Carried, latest per settings key
     (rule 13).
  3. **Hook output**: text-only content matching the hook-output signature that lane A documents
     with stub evidence in the A2 PR. Per-turn: paged with the turn it sits in, wherever it sits
     in that turn (measured by reviewer-2 at zero spend in the PR #30 review: `UserPromptSubmit`
     output arrives after the new prompt, and `PostToolUse` `additionalContext` right after the
     `tool_result`).
  4. **Other text-only content**: carried, because it may be a standing instruction (the vendor
     documents output-style changes and plan mode as instructions delivered as a message that
     keep applying). Before A2 merges, lane A measures with zero-spend stubs how an output-style
     change, a plan-mode toggle, and a session start with no effort setting arrive; any of them
     that lacks a settings field falls here and is carried.
  5. **Tool-addition messages and anything with non-text blocks**: carried; non-text ones are
     logged as `unclassified_system_message`. Tool-addition messages are vendor-documented but
     not observed on Claude Code 2.1.301 (builder-2 measured that adding or removing an MCP
     server on resume changes `tools`, a head change under rule 6, and adds no system message).
  Carrying is the safe failure, since paging a message that must persist would lose state.
  Bounded fallback: if carried content grows more than 8,000 tokens over its size at the
  session's first request, the oldest step 4 messages are paged at the next rotation, each listed
  in the index with its first line and recallable, and the proxy logs `carried_paged`. Lane A
  pins down, with stub evidence, exactly which blocks count as injected context.
- The proxy keeps an epoch state `E = (k, carried, pinned_text, index_text, head_fingerprint)`:
  the turns before turn index `k` are paged out.

1. **Upstream request** = the CLI's `system` and `tools` byte for byte, plus the retained turns as
   sent, where the first retained turn-start message gets, ahead of its own content, the carried
   context blocks in their original order and then a preamble text block (pinned state and page
   index). Carried system messages from paged turns are placed immediately after that first
   retained turn-start message, in their original order, matching Claude Code's own
   user-then-system shape. Measured by builder-2, reported by manager-1, pending review, evidence to land in the A2 PR
   (341 tokens, haiku via the owner's proxy): a
   `role: "system"` message before the first retained turn-start message returns 400; after it,
   or merged into it as text, returns 200. No `tool_result` is displaced. In the first epoch `k = 0`, nothing is carried separately, and the
   preamble is empty. Every other request field passes through unchanged, including
   `cache_control`, `anthropic-beta`, `stream`, and `thinking`.
2. **Thinking.** For every retained message that existed when `E` was created, strip thinking and
   redacted-thinking blocks deterministically on every request, so the transformed prefix is
   byte-identical across requests. An assistant message left with no content is given a single
   text block `[thinking omitted]`. Messages added after the rotation pass through untouched.
3. **Rotation trigger.** Rotate only at the start of a turn: the request's newest turn contains no
   assistant message yet (only its turn-start message and any system messages). Rotate when the
   estimated upstream input would exceed `bound - headroom`. Estimate = last upstream
   `input_tokens + cache_read + cache_creation` for this session plus the bytes added since.
   Until the first real usage arrives, count `tool_result` content at 1.5 bytes per token and
   other text at 3.5 (lane 2 measured numeric and log-heavy content near 0.55 tokens per
   character, and M0 saw log-heavy tool output near 1.8 bytes per token, so 1.5 errs high); after
   that, use the session's measured bytes-per-token ratio. After every response, compare real
   usage with the bound and the estimate: log `over_bound` from real usage when it exceeds the
   bound, and `estimate_error` when real usage exceeds the estimate by more than 10%.
4. **Rotation.** Fixed overhead `F` = system + tools + carried content + preamble (pinned and
   index), in tokens, taken from measured usage once available (the first request's input is the
   head) and from the estimator before that. The window budget is `W = bound - headroom - F`, and
   the retained window after rotation targets `window_fraction * W` (default 0.5), so the window
   can grow before the next trigger. Choose the smallest `k` (retain as many recent turns as fit)
   within that target, always retaining the newest complete turn and the new turn, and never below
   the current `E.k` (a rotation never un-pages a turn already in the store). If the chosen `k`
   equals `E.k`, the rotation is a no-op: nothing is paged and no thinking is stripped. Write the
   newly paged turns to the page store, one page per turn, minus carried content; add any newly
   paged system messages to `carried`; rebuild `pinned_text` and `index_text`; strip thinking per
   rule 2. If `W` is not positive or cannot hold the newest complete turn, log
   `bound_too_small` and send the request unrotated; the launcher warns at start when the measured
   head leaves less than 25,000 tokens of window budget and suggests a larger `--bound`. Example
   from M0 numbers: a throwaway head near 54.5k with pins leaves `W` about 70k at a 150k bound; an
   OpenRig seat head near 92k leaves about 33k, so about 16.5k of growth room: rotations and
   thinking strips will be frequent at 150k for large heads (see O3).
5. **Never rotate inside a tool loop** (D17). A turn that grows past the bound is sent as is and
   recorded as over-bound in the session log; the next turn start rotates.
6. **Head change.** If the CLI's `system` or `tools` change (byte fingerprint), start a new epoch at
   the next turn start; never rewrite them.
7. **Usage.** Return the upstream `usage` unchanged. The CLI therefore sees the bounded prompt's
   real size.
8. **Errors.** Forward upstream errors unchanged. Never retry by silently changing the request.
   Log every 4xx with the epoch state for diagnosis.
9. **Streaming.** Relay SSE events as they arrive; the proxy only reads `message_start` and
   `message_delta` usage. No hold-back is needed because recall is an MCP tool (D19).
10. **Matching history.** Recognise each retained message in later requests by a hash of its
    canonical JSON with every `cache_control` field removed (the CLI moves `cache_control` along
    the tail), and with content normalised so that a plain string and a single text block holding
    the same text hash equal (the CLI converts one to the other along the history). On a mismatch, resynchronise: find the longest prefix of the stored messages that
    still matches and continue from there; if paged turns themselves changed (for example the CLI
    compacted locally), start a new epoch at the current turn start with everything before it
    paged. Never fall back to sending the full history unbounded.
11. **Other endpoints.** `/v1/messages/count_tokens` is transformed exactly like `/v1/messages`, so
    the CLI's own counts match what is sent. `HEAD /api/hello` and any other endpoint pass through
    unchanged.
12. **Settings-only system messages.** A system message with empty `content` and only settings
    fields (such as `output_config`) is carried like any other system message and forwarded as the
    CLI sends it. Lane A measured that Claude Code does not empty earlier system messages, so no
    client-side edit precedes kept thinking (zero-spend stubs; measured by builder-2, reported by
    manager-1, pending review; evidence to land in the A2 PR under `spikes/m2/carried/`). If a
    later CLI version starts emptying them, re-run that check before relying on kept thinking.
13. **Carried growth.** Measured by builder-2 without hooks (zero spend, real CLI-written history,
    one fresh start and five resumes; reported by manager-1, pending review): system messages
    accrue per settings change; each effort change adds one settings-only message of about 66
    bytes after the new prompt. Measured by reviewer-2 with a context-injecting `UserPromptSubmit`
    hook (a single zero-spend probe of three real CLI-written turns, PR #30 review; builder-2's
    later probe agrees: about 70 characters per later turn, no `output_config`): one content system message per
    turn, so OpenRig seats and other hook users add one every turn. Hence the classification in the
    Terms: identified hook output pages with its turn, and everything else is carried, with the
    bounded 8,000-token fallback. At rotation only (deterministic, and
    safe for thinking because rotation strips it), keep the latest settings-only message per
    settings key. If carried content grows by more than 4,000 tokens over its size at the
    session's first request, log `carried_over_budget` in the session log.

Defaults (configurable flags and env): `bound` 150,000 tokens until L2f sets O3; `headroom` the
online running max of per-turn growth plus 25%, floor 25,000; `window_fraction` 0.5 of the window budget `W` (rule 4).
Constraint: `bound` plus the largest expected over-bound turn must stay below both Claude Code's
idle-compaction minimum (200,000 by default, `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`) and the model's
compaction trigger, or the CLI may compact; the launcher refuses a `bound` above 170,000 unless
`--allow-large-bound` is given. That this keeps idle compaction away assumes it counts the bounded reported
usage, which is inferred (INT section 3); acceptance run 1 verifies it.

## Store (normative)

- **Pages:** `pages/<page id>.json`, one per paged-out turn, holding the exact message JSON of that
  turn (thinking included as received), its turn number, byte size (canonical JSON, summed per
  content block) and token estimate, and a stub: first 200 characters of the user text, tool names
  used, and at most 20 file paths mentioned.
  Page id: `pg_` + first 16 hex of HMAC-SHA256 over the canonical JSON of `{turn, messages}`
  only (not the stub or token estimate, so ids stay stable when those rules change), keyed with a
  random per-store key kept in the store and never sent upstream (ids appear in prompts, and short turns
  must not be guessable from their id). Pages are immutable.
- **Ledger:** the M1 record model (kinds `event`, `decision`, `obligation`, `artifact`,
  `reference`, `closure`; epistemic `verified`, `reported`, `summary`, `hypothesis`; provenance;
  supersession; open obligations) harvested from PR #2 (record model), PR #3 (state derivation),
  and builder-2's unpushed ledger work at commit 1ab5f29 (readable from the shared object store),
  append-only JSONL with a hash chain and crash-truncation repair. File sources require `sha256`;
  a missing file counts as stale. Records may cite pages as provenance (`type: page`).
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
  rewrite engine (rules 1 to 13), upstream resolution, session log; then the Codex adapter
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
proxy as upstream (D12). Token caps: the harness reports its exact estimate for the H1 run and the
full-context baseline before anything runs; intake approves it as its own budget line. On the
owner's proxy replayed haiku thinking is dropped on some requests (O6), so the baseline is labeled
a no-replayed-thinking baseline.

1. **Long run:** a scripted session whose local history reaches at least 2.5 times the bound
   (bound 150,000, the shipped default, so the run tests the product as shipped and leaves each
   epoch room to show cache reads; at 100,000 the window budget `W` would be about 20,500 tokens
   (retained window about 10,000) and every turn would rotate, per lane B's dry run) shows zero compaction events, every
   upstream request under the bound except logged over-bound turns, and zero 4xx responses.
2. **Fidelity:** the L2f harder probes, in the chain-heavy mix (deep supersession chains are the
   only probe type that failed at small contexts in the two scored pilots, measured by builder-4
   in PR #22: deep chains 5/8 at about 12k, and 29/30 overall at 15,002 counted tokens with the
   one miss in a depth-8 chain; pilot 2 overran with no score), asked at the end of that run, scored against the
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
