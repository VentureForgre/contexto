# M0 paging and pinning policy, with prior art

Status: phase 1 design for M0 question 4 and H5 (lane 2, packet L2b). Nothing here rewrites
requests yet; phase 2 builds it after the owner decides question 5. Author: build.builder-4.
Date: 2026-10-15. Brief: `docs/milestones/M0-infinite-session-study.md`. Executable form of this
policy, used by the fidelity simulation: `spikes/m0/fidelity/policy.py`. Test design and results:
`docs/research/M0-fidelity-test.md`.

Every claim is tagged **[measured]**, **[vendor-documented]**, or **[inferred]**. Simulation
results are **[measured, simulated]**: exact outputs of deterministic code, with no model involved.

## Summary

- **Request layout.** Each upstream request is rebuilt as a stable head, a page index of stubs, and a
  verbatim window of recent turns.
  - The stable head holds the system prompt, the tools, and a pinned state block.
  - The stubs carry keyed, content-addressed ids.
- **What is paged.** Paging is turn-granular, so a `tool_use` and its `tool_result` can never be
  split. Large tool outputs inside the window are paged by replacing only their content.
- **The recall tool.** A model fetches a page back verbatim, by id, with a `recall` tool that the
  proxy answers itself in extra upstream round trips the CLI never sees. There are no embeddings and
  no similarity search: pages are chosen by id from extractive headlines.
- **Window moves.** The window moves in steps (high and low watermarks), not every turn, so the
  prompt prefix changes at paging events only. In the simulation that was 13 times in 242 turns
  [measured, simulated].
- **Fidelity.** In the offline simulation the pinned block keeps facts, decisions, and obligations
  available, and recall brings back unpinned details, all at about 1/10 of the full-history request
  size [measured, simulated]. Whether a model uses them as well as the simulated reader does is a
  phase 2 measurement.

## 1. Request layout

```
system:   CLI system prompt (unchanged)
          + PINNED STATE block            <- changes only at epochs
tools:    CLI tools (unchanged) + recall  <- recall added only when pages exist
messages: [page index text block] + window turns, verbatim except stubbed large tool outputs
```

- **Order.** The order follows how prompt caching works. Anthropic caches "`tools`, `system`, and
  `messages` (in that order)", and "changes at each level invalidate that level and all subsequent
  levels" [vendor-documented, Anthropic prompt caching, accessed 2026-10-15]. Anything that changes
  often must therefore sit late. The pinned block lives in `system` and changes only at an **epoch**
  (see section 3).
- **Recall in `tools`.** Adding `recall` to `tools` changes the tool list, which invalidates the
  whole cache [vendor-documented]. The tool should therefore be added once, at the first paging
  event, and kept for the rest of the session [inferred].

## 2. Pinned state

**What is pinned**, from the ledger as of the epoch start:

1. **Open obligations, complete list.** Each line gives the id, the text, and the turn it opened.
   The header says the list is complete, so a model can conclude that an unlisted item is closed.
2. **Active decisions.** Current value only, with record id and turn. Superseded values are not
   shown; recall reaches them if needed.
3. **Verified facts with provenance.** Each line carries its source locator; facts are listed
   newest first.

**Size bound**
- Default 2,000 tokens. The simulation uses characters / 4 as its estimate; the prototype should
  count with the provider's token counter.
- Priority is the M1 checkpoint order: obligations, then conflicts, then decisions, then facts.
- Obligations are never dropped. If they alone exceed the bound, the proxy surfaces an error to the
  operator instead of truncating.
- Decisions and facts that do not fit are dropped oldest first. The block ends with
  `OMITTED: decision=<n> fact=<n>`, and those records stay reachable through recall.
- The tests check that the rendered block never exceeds the bound and never drops an open
  obligation [measured, simulated].

**Rendering**, with record ids added when the block is ledger-backed (section 7):

```
PINNED STATE (from the session ledger; authoritative over older turns)
OPEN OBLIGATIONS (complete list; an item not listed here is closed):
- ob01: write the rollback runbook [since turn 51]
ACTIVE DECISIONS:
- the chosen queue backend is harbor-2291 [turn 140]
VERIFIED FACTS:
- the staging database port is garnet-4821 [turn 3]
OMITTED: decision=0 fact=2        <- only when something was left out
```

## 3. Window size rule and epochs

- **Turn units.** A turn starts at a user message that contains no `tool_result` block, and runs
  until the next such message. Only whole turns are paged.
- **Hysteresis.** The window holds turns `[first, now)`. When its size exceeds `window_high`, the
  proxy pages whole turns from its start until the size is at most `window_low`. The newest turn is
  always kept, even if it alone exceeds the bound. Defaults: high 24,000 and low 12,000 tokens;
  phase 2 should tune them with lane 2's cost model.
- **Epochs.** Each move of `first` is an epoch boundary. The pinned block and page index are
  re-rendered only then, so between epochs the request prefix is byte-stable and stays cacheable.
  The CLI's own appends land after the cached prefix. In the simulation, `first` moved 13 times
  over 242 turns [measured, simulated].
- **Cache economics.** Anthropic charges 1.25x base input for 5-minute cache writes, 2x for 1-hour
  writes, and 0.1x for reads (0.05x on Opus 5.5 and Sonnet 5.5) [vendor-documented].
  - An epoch therefore costs about one cache write of the head plus window. A turn inside an epoch
    costs a cache read plus the new tokens [inferred].
  - A wider gap between high and low means fewer epochs but larger requests. That is the knob lane
    2's cost model should price.

## 4. What gets paged, and the stub format

**Paged**
- **Whole turns before the window start**, in fixed groups of 8 turns (turns 0 to 7, 8 to 15, and
  so on). Fixed boundaries mean a completed group always has the same id, and the trailing partial
  group is its own page.
- **Tool outputs inside the window larger than 1,500 tokens.** Only the `content` of the
  `tool_result` block is replaced; the block and its `tool_use_id` stay.

**Never paged**
- The current turn, which holds any active tool loop and its signed thinking blocks.
- The system prompt, the tool definitions, and the pinned block.

**Page id**
- `pg_` + the first 16 hex characters of HMAC-SHA256(store key, canonical JSON of the page payload).
- The store key is random, generated once per store, and kept beside the pages, outside Git.
- Keyed, as the brief requires, so an id cannot be checked against guessed content.
- Same content under the same store, same id, across requests and restarts. The tests check this
  [measured, simulated].

**Stub grammar**

```
turn page:   [paged pg_<16 hex> | turns <a>-<b> | <n> msgs | ~<tokens> tokens | <h_a> / <h_a+1> / ... / <h_b>]
tool output: [paged pg_<16 hex> | tool_result of <tool>(<canonical input JSON>) | ~<tokens> tokens | "<headline>"]
```

- **Headlines.** Each `<h>` is that turn's opening user text and `<headline>` is the tool output's
  first text. Both are whitespace-collapsed and cut to 40 characters at a word boundary, with
  ` ...` appended. Cutting at a word boundary means a partial value never appears.
- **Purpose of headlines.** They are extractive, not model-written, and they let a reader choose a
  page by its words.
- **Page index.** The index is one text block prepended to the window's first user message:
  `PAGED HISTORY (use the recall tool with an id to read a page verbatim):` followed by one stub per
  line. It is rebuilt only at epochs.
- **Index bound** [inferred, designed, not simulated]. The index grows with history, about 100
  tokens per 8-turn page. Above an index budget (proposed 4,000 tokens), the oldest stubs fold into
  epoch stubs, `[paged-epoch pe_<16 hex> | turns <a>-<b> | <k> pages]`. Recalling an epoch id
  returns its stub list, so any turn stays reachable in at most two recalls.

## 5. Recall tool contract

**Definition** (added to `tools` by the proxy, never by the CLI):

```json
{
  "name": "recall",
  "description": "Fetch a paged-out part of this conversation by its exact id (pg_...). Ids appear in [paged ...] stubs. Returns the original text verbatim, at most 32000 characters per call; pass next_offset to continue.",
  "input_schema": {
    "type": "object",
    "properties": {
      "id": {"type": "string", "pattern": "^pg_[0-9a-f]{16}$"},
      "offset": {"type": "integer", "minimum": 0, "default": 0}
    },
    "required": ["id"],
    "additionalProperties": false
  }
}
```

**Output**
- A JSON `tool_result`: `{"id", "turns": [a, b], "offset", "text", "next_offset"}`.
- `text` is the verbatim page, as `[turn N] role: content` lines for turn pages or the raw output
  for tool pages, sliced to at most 32,000 characters (about 8k tokens).
- `next_offset` is null at the end.

**Errors**
- Returned as a `tool_result` with `is_error: true` and a code, so the model can recover:
  - `bad_id`: the input is not shaped like a `pg_` id.
  - `unknown_id`: no such page.
  - `bad_offset`: offset outside 0 to the page length.
  - `recall_limit`: more than 8 recall calls in one user turn.
- The limits bound the hidden round trips per turn (section 6).
- Ids are exact; there is no fuzzy matching. Choosing a page is the model's job, using the headlines.

**Extension, ledger-backed** [inferred]: accept M1 record ids (`rec_...`) too, returning the full
record with its provenance. Pinned lines omitted for space then stay reachable by id.

## 6. What a proxy-added tool requires

The CLI never declared `recall`, so it must never see a `recall` call. All of this is [inferred]
until lane 1's interception evidence for question 1 confirms the CLI's streaming and request
behavior.

1. **Buffer before forwarding.** Responses arrive as a stream. When the model emits a `recall`
   `tool_use`, the proxy must not forward that part of the response to the CLI. It must hold the
   streamed message until it knows whether the message contains a `recall` call. The simplest safe
   rule is to hold the content blocks of a streamed message and release them once the message ends
   without a `recall` call. This costs streaming latency on tool-calling turns, and lane 1 should
   measure it.
2. **Hidden round trips.** For each `recall`, the proxy executes it against its store, appends the
   assistant message and a user message with the matching `tool_result`, and calls upstream again
   with the same head and window. The prefix is unchanged, so these calls read from cache. It
   repeats until the model stops calling `recall`, or the 8-call limit, after which `recall_limit`
   errors push the model to answer. Only the final response is streamed to the CLI.
3. **Mixed calls.** If one assistant message calls both `recall` and a CLI tool, every `tool_use`
   in it must be answered in the next user message (pairing rule). The proxy answers `recall`
   normally and answers each CLI tool call in that hidden exchange with `is_error: true` and "call
   this again after recall finishes". The model then re-issues the CLI call in a later message
   without `recall`, which the proxy forwards. The CLI never sees the aborted call.
   - The alternative, `disable_parallel_tool_use`, would change the CLI's behavior on every turn.
4. **History stays clean.** The CLI's next request will not contain the hidden exchange. The proxy
   does not reinsert it: the recalled text is still addressable, and reinsertion would undo the bound.
5. **Usage reporting.** The hidden calls are billed but not visible to the CLI. The proxy should
   report the final call's usage to the CLI unchanged and log the hidden calls' usage itself.
   Question 2 decides whether the reported counts may differ from the bounded prompt's.
6. **Thinking blocks.** Signed thinking blocks in the current turn's tool loop pass through
   untouched, because the current turn is never paged. Whole turns paged with their thinking leave
   the prompt entirely rather than being edited. That is consistent with the API dropping earlier
   thinking on 5.x models (see compaction notes below), but lane 1 should confirm for both CLIs.

## 7. Tool-use and tool-result pairing survives paging

- **Turn-granular cuts.** A turn begins at a user message with no `tool_result`. Every
  `tool_result` therefore sits in the same turn as the assistant `tool_use` that precedes it, and
  cutting between turns cannot separate a pair.
- **In-window stubs keep the block.** Paging a tool output replaces the block's `content` only; its
  `type` and `tool_use_id` remain.
- **Hidden recall pairs stay hidden.** Recall pairs exist only in hidden round trips and are always
  complete there (section 6).
- **The page index keeps roles alternating.** The index rides as an extra text block inside the
  window's first user message, so no new message is inserted.
- **Tested.** `check_protocol` validates role alternation and that every `tool_use` is answered by
  the next message and every `tool_result` follows its `tool_use`. It passed for all four conditions
  on every turn of the 242-turn default session and the 120-turn test session [measured, simulated].

## 8. Backing the pinned state with the M1 ledger

M1 (`docs/milestones/M1-ledger-and-recovery.md` on `main`; its implementation PRs #2, #3, #4 are
paused) already has most of the pinned-state machinery.

| Pinned element | M1 source |
|---|---|
| Open obligations | `contexto.state.derive(...).open_obligations` |
| Active decisions | active `decision` records; same-topic conflicts become explicit `CONFLICT` items |
| Verified facts | active records with `epistemic: verified`, which M1 requires to carry provenance |
| Stale warning | `state.stale` (cited file changed or missing), rendered under a stale heading |
| Bounded rendering | `contexto.checkpoint.build` already uses this priority order and never drops an obligation |
| Epoch identity | the ledger `head()` hash: re-render the block at an epoch only if the head changed |

**What M1 lacks for this use** [inferred]:
- **Capture.** Nothing writes ledger records from a live conversation, and transcript ingestion is
  out of M1's scope. Someone must append: the model through a proxy-added tool such as `record`,
  the operator, or a hook. Pinned state is only as good as capture, and the fidelity simulation
  assumes perfect capture.
- **Page store.** M1 has no store for verbatim turn pages, no keyed page ids, and no recall. Pages
  need their own append-only store, which could share M1's hash-chain and quarantine conventions.
- **Record-to-page links.** A record's provenance should cite the page that holds the original
  words. M1's `transcript` source type has a free-form `locator`, which could carry `pg_` ids
  without a schema change; this needs pinning in a contract.
- **Token budgets.** The M1 checkpoint budget is in characters; the pinned block needs a token
  budget. This is a small change.
- **No "fact" kind.** Verified facts map to `event`, `artifact`, or `reference` records with
  `verified`. That is workable, but the renderer needs a rule for which kinds count as facts.

## 9. Prior art and H5 (provider-native features)

Sources were read on 2026-10-15 unless a date is given. Tags apply per sentence.

### MemGPT (paper) and Letta

- **What it does.**
  - MemGPT splits the prompt into "system instructions, working context, and FIFO Queue"
    [vendor-documented, Packer et al., arXiv 2310.08560 v2, 2024-02-12].
  - The head of the queue is "a recursive summary of messages that have been evicted"
    [vendor-documented].
  - Evicted messages are "stored indefinitely in recall storage and readable via MemGPT function
    calls", with paginated retrieval [vendor-documented].
  - On deep memory retrieval, GPT-4 went from 32.1% to 92.5% accuracy with MemGPT [measured, by the
    authors, Table 2].
  - Letta, the successor framework, persists all messages in a database and compacts with
    `compaction_settings.mode` (default `sliding_window`, `sliding_window_percentage` 0.3)
    [vendor-documented, docs.letta.com, accessed].
  - Letta's archival memory is "a semantically searchable database" [vendor-documented].
- **Trigger.**
  - MemGPT warns at a "warning token count (e.g. 70% of the context window)" and evicts at the
    flush count [vendor-documented].
  - Letta compacts when history "grows too long to fit" [vendor-documented].
- **Why it falls short.**
  - It is the closest prior art: pinned working context, out-of-context storage, retrieval tools
    [inferred].
  - Retrieval is by search, and archival search uses embeddings, which the owner ruled out.
    Evicted turns surface as a lossy recursive summary rather than as addressable stubs [inferred].
  - It is a whole agent framework that owns the loop, not a transparent layer under an existing CLI
    [inferred].

### Anthropic API: context editing, memory tool, compaction, prompt caching

- **Context editing.**
  - Beta `context-management-2025-06-27`, strategy `clear_tool_uses_20250919`: `trigger` defaults
    to 100,000 input tokens and `keep` to 3 tool uses. Cleared results are replaced "with
    placeholder text" [vendor-documented, platform.claude.com context-editing, accessed;
    announced 2025-09-29].
  - Editing happens server-side while "your client application maintains the full, unmodified
    conversation history", and clearing "invalidates cached prompt prefixes" [vendor-documented].
  - Anthropic reports a 29% gain from context editing alone, and an 84% token reduction in a
    100-turn eval [measured, by Anthropic, blog 2025-09-29].
- **Memory tool.** `memory_20250818` is client-side: "your application executes" `view`,
  `create`, `str_replace`, `insert`, `delete`, and `rename` under `/memories` [vendor-documented].
- **Server-side compaction.**
  - Threshold mode: `compact-2026-01-12`, `trigger` default 150,000.
  - On-demand mode: `compact-2026-09-04`. It returns a signed `compaction` block; editing it fails
    with `compaction_signature_invalid`.
  - "The API automatically drops all content blocks prior to the `compaction` block"
    [vendor-documented, compaction docs, accessed].
- **Prompt caching (H5).**
  - Explicit or automatic `cache_control`, at most 4 breakpoints, a "5-minute lifetime" refreshed
    on use, or `"ttl": "1h"`.
  - Writes cost 1.25x (5 min) or 2x (1 h), reads 0.1x (0.05x on Opus 5.5 and Sonnet 5.5).
  - Minimum 512 tokens on 5.x models. "Cache hits require 100% identical prompt segments"
    [vendor-documented, platform.claude.com prompt-caching, accessed].
- **Why it falls short.**
  - Cleared tool results become placeholders with no id and no way to fetch them back.
    Compaction is a lossy summary that is signed, so it can be neither audited nor edited.
    [vendor-documented for the mechanics; inferred for the conclusion]
  - The memory tool is addressable, but the model chooses what to save, so verbatim history is not
    guaranteed [inferred].
  - Caching makes the full history cheaper per token but never bounds it. Every turn still resends
    everything, and the context limit still forces compaction [inferred].
  - All of these are provider-specific.

### OpenAI: conversation state, truncation, compaction, prompt caching

- **Conversation state.**
  - With `previous_response_id`, "all previous input tokens for responses in the chain are billed
    as input tokens" [vendor-documented, conversation-state guide, accessed].
  - The Conversations API stores items durably [vendor-documented].
- **Truncation.** `truncation: auto` drops "items from the beginning of the conversation"; the
  default `disabled` fails with a 400 [vendor-documented].
- **Compaction.**
  - `/responses/compact` (changelog 2025-12-11), and server-side compaction via
    `context_management: [{type: "compaction", compact_threshold: N}]` (changelog 2026-02-10).
  - The compaction item is "encrypted" and "opaque and not intended to be human-interpretable"
    [vendor-documented].
  - The Agents SDK has session backends, including `OpenAIResponsesCompactionSession`
    [vendor-documented].
- **Prompt caching (H5).**
  - Automatic, "enabled by default for supported OpenAI models", at 1,024 tokens minimum for
    GPT-5.6 and later; "Cache reuse requires the entire rendered prefix to match".
  - Retention is a 30-minute TTL on GPT-5.6 and later, or `in_memory`/`24h` on earlier models.
  - Reads cost 0.1x uncached on GPT-5.6 and later, and `prompt_cache_key` helps routing
    [vendor-documented, prompt-caching guide, accessed].
- **Why it falls short.**
  - Truncation is permanent loss, and the compacted state is opaque [vendor-documented].
  - Full history is retained server-side, but a proxied session has no `recall(id)` into it
    [inferred].
  - Caching has the same limit as Anthropic's: cheaper, not bounded [inferred].

### Claude Code compaction

- **What it does.**
  - Automatic compaction "clears older tool outputs first, then summarizes the conversation if
    needed" [vendor-documented, code.claude.com context-window, accessed].
  - The summary keeps "requests and intent, key technical concepts, files examined or modified...,
    errors..., pending tasks, and current work". Full tool outputs and intermediate reasoning "are
    gone" [vendor-documented].
  - CLAUDE.md, auto memory, and up to five recently modified files are re-injected
    [vendor-documented].
- **Trigger.**
  - Near the model's limit: about 967K tokens for native 1M-window models, 200K otherwise
    [vendor-documented].
  - Tunable with `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100000 to 1000000) or
    `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (lowers only), and can be disabled with
    `DISABLE_AUTO_COMPACT` [vendor-documented].
  - Manual: `/compact [instructions]`. Hooks: `PreCompact` can block, `PostCompact` receives the
    summary, and `SessionStart` with matcher `compact` can re-inject context [vendor-documented].
- **Our observations** (CLI 2.1.301, `claude-haiku-5-5`, headless).
  - `/compact` shrank a 29.7k-token context to 4.3k and kept all 37 planted values [measured,
    fidelity E2].
  - Its own model call reported zero `usage` in JSON output. The session's cumulative `modelUsage`
    grew by 34,358 tokens [measured].
- **Why it falls short.**
  - The live context keeps only a lossy summary. The full transcript survives on disk at
    `~/.claude/projects/<project>/<session>.jsonl`, but the model has no addressable way back to it
    [vendor-documented, inferred].
  - Each compaction re-reads the whole context once, at a cost that per-turn usage does not show
    [measured, as above].
  - Whether Claude Code sizes its trigger from API-reported usage decides what a proxy that shrinks
    the prompt does to it. That is question 2, lane 1 [inferred].

### Codex CLI compaction

- **Trigger.**
  - `model_auto_compact_token_limit` ("unset uses model defaults"), capped in source at 90% of the
    context window [vendor-documented, Codex config reference, accessed; `openai_models.rs` at
    openai/codex main 16e578dd1d, 2026-10-15].
  - Also `/compact`.
- **What it does.**
  - Local mode builds new history from the initial context, recent user messages up to
    `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`, and a summary written from a "CONTEXT CHECKPOINT
    COMPACTION" prompt. Assistant turns and tool outputs are dropped, except client-marked retained
    tool outputs [vendor-documented, `compact.rs`].
  - A provider capability `remote_compaction = "v2"` sends compaction through the Responses API
    [vendor-documented, config reference; `compact_remote_v2.rs`, last commit 2026-10-13].
  - `tool_output_token_limit` caps each stored tool output [vendor-documented].
- **Why it falls short.**
  - Local mode keeps only user messages and a summary; remote mode keeps opaque encrypted items
    [vendor-documented].
  - Truncated tool output is gone, and there is no recall path [inferred].
  - Which compaction mode is the default for the OpenAI provider is not verified.

### What none of them provide

**Gap in prior art** [inferred]: none keeps the full history outside the model, under the user's
control, behind stable ids the model can dereference, while bounding every upstream request below
the point where compaction would trigger. MemGPT/Letta comes closest, but uses search and
summaries rather than ids. The provider features (context editing, compaction, caching) either
discard, summarize opaquely, or make the full prompt cheaper without bounding it.

**Not verified in this pass**:
- The placeholder text Anthropic uses for cleared tool results.
- Whether Claude Code calls Anthropic's server-side compaction.
- The parameters of Claude Code's tool-output clearing.
- Codex's default `remote_compaction` for the OpenAI provider.
- The implementation of Letta's `conversation_search`.

### Sources

- MemGPT, arXiv 2310.08560 (v1 2023-10-12, v2 2024-02-12): https://arxiv.org/abs/2310.08560
- Letta docs:
  - compaction: https://docs.letta.com/v1-sdk/messages/compaction
  - archival memory: https://docs.letta.com/v1-sdk/memory/archival-memory/index.md
  - context engineering: https://docs.letta.com/guides/agents/context-engineering
- Anthropic, "Managing context on the Claude Developer Platform", 2025-09-29: https://claude.com/blog/context-management
- Anthropic docs:
  - context editing: https://platform.claude.com/docs/en/build-with-claude/context-editing
  - compaction: https://platform.claude.com/docs/en/build-with-claude/compaction
  - threshold compaction: https://platform.claude.com/docs/en/build-with-claude/compaction-threshold
  - on-demand compaction: https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand
  - memory tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
  - prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- OpenAI docs:
  - conversation state: https://developers.openai.com/api/docs/guides/conversation-state
  - compaction: https://developers.openai.com/api/docs/guides/compaction
  - prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching
  - Responses reference: https://developers.openai.com/api/reference/resources/responses/methods/create
  - changelog (2025-12-11, 2026-02-10): https://developers.openai.com/api/docs/changelog
  - Agents SDK sessions: https://openai.github.io/openai-agents-python/sessions/
- Claude Code docs:
  - https://code.claude.com/docs/en/context-window
  - https://code.claude.com/docs/en/env-vars
  - https://code.claude.com/docs/en/hooks
  - https://code.claude.com/docs/en/settings-reference
- Codex:
  - config reference: https://learn.chatgpt.com/docs/config-file/config-reference
  - source at openai/codex main 16e578dd1d (2026-10-15): `codex-rs/core/src/compact.rs`,
    `compact_remote_v2.rs`, `codex-rs/protocol/src/openai_models.rs`

All pages without a visible date were accessed 2026-10-15.

## Open questions

1. **Capture.** Who writes ledger records during a live session: a proxy-added `record` tool, the
   operator, or hooks? This decides whether pinned state is trustworthy, and it is outside
   question 4.
2. **Window and pinned settings.** The defaults (window 24k/12k, pinned 2k, tool pages over 1.5k)
   are unpriced. Lane 2's cost model (L2a) should set them, and phase 2 should check fidelity at
   the chosen values.
3. **Proxy interception details.** Streaming hold-back latency, mixed-call handling, and
   usage-count reporting depend on lane 1's question 1 and 2 evidence for each CLI.
4. **Index folding.** The index bound and epoch-stub folding are designed but not simulated.
