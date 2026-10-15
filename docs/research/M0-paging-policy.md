# M0 paging and pinning policy, with prior art

Status: phase 1 design for M0 question 4 and H5 (lane 2, packet L2b). Nothing here rewrites
requests yet; phase 2 builds it after the owner decides question 5. Author: build.builder-4.
Date: 2026-10-15, revised the same day for review findings B1 to B4. Brief:
`docs/milestones/M0-infinite-session-study.md`. Executable form of this policy, used by the
fidelity simulation: `spikes/m0/fidelity/policy.py`. Test design and results:
`docs/research/M0-fidelity-test.md`. Quality-versus-size probe: `docs/research/M0-quality-vs-context.md`.

**Claim tags.**
- **[measured]**: we ran it.
- **[measured, simulated]**: exact output of deterministic code, with no model involved.
- **[vendor-documented]**: stated by the vendor. For a paper, the authors' own description.
- **[inferred]**: our reasoning.

## Owner principle and how approaches are ranked

**The principle** (intake, 2026-10-15 23:12Z, binding):
- The compaction threshold exists because model quality already degrades as context grows.
- Contexto's goal is very long sessions in which compaction never happens, with working context kept
  well below the threshold at all times. Compaction is a failure mode to avoid, not a mechanism to
  optimize.

**Success bar** for any design:
- no compaction ever, **and**
- recall and task accuracy equal to the full-context baseline on the fidelity probes.

The baseline at each working-context size is measured by the L2d probe. The window bound below
should be set from where that probe shows quality starting to slip.

**Ranking.** Approaches are ranked first by how small they keep working context, second by how
stable quality stays, and only then by token cost [inferred, pending L2d and phase 2 measurements].

| Rank | Approach | Working context | Quality stability | Cost |
|---|---|---|---|---|
| 1 | Hypothesis: bounded window, pinned state, recall (this doc) | bounded by design, constant over the session | depends on capture and recall selection; reasoning lost at each epoch (section 7) | lowest per turn; one cache rebuild per epoch |
| 2 | H4 supervisor rollover | bounded, but grows to the rollover point each cycle | each rollover hands off a summary, which is lossy | full context near each rollover |
| 3 | H3 session-file rebirth | resets to the curated state, then grows | same as H4 | as H4 |
| 4 | H2 hooks that inject state | grows until the CLI compacts | injected state helps, but compaction still happens | full context every turn |
| 5 | H5 provider features (caching, context editing, built-in compaction) | caching does not bound context; editing trims tool output only; compaction is the failure mode | compaction loses detail | cheapest per token with caching, unbounded overall |

## Summary

- **Request layout.** Each upstream request is a stable head, a page index of stubs, and a verbatim
  window of recent turns.
  - The stable head holds the system prompt, the tools, and a pinned state block.
  - The stubs carry keyed, content-addressed ids.
- **Epochs.** Every rewrite of earlier content happens at an **epoch**, when the window start moves.
  Epochs happen only at turn boundaries, never inside a turn. Between epochs each request only
  appends to the previous one.
  - At an epoch, the proxy strips all earlier thinking blocks. That is the edit the vendor allows
    when history changes (section 7).
  - In the simulation, edit points equal window moves: 17 in 242 turns, with no request over the
    bound [measured, simulated].
- **Paging.** Paging is turn-granular, so a `tool_use` and its `tool_result` are never split.
- **Recall.** A model fetches a page back verbatim, by id, with a `recall` tool that the proxy
  answers itself. There are no embeddings and no similarity search.
- **Fidelity.** In the offline simulation:
  - the pinned block keeps facts, decisions, and obligations available at every probe point;
  - recall brings back unpinned details;
  - both at about a tenth of the full-history request size [measured, simulated].
  These scores follow largely from how the simulation is built (section 2 of the fidelity doc). The
  measurement that matters is phase 2's model run against the L2d baseline.

## 1. Request layout

```
system:   CLI system prompt (unchanged)
          + PINNED STATE block            <- re-rendered only at epochs
tools:    CLI tools (unchanged) + recall  <- recall declared from the first request, never changed after
messages: [page index text block] + window turns; earlier content changes only at epochs
```

- **Order.** Anthropic caches "`tools`, `system`, and `messages` (in that order)", and "changes at
  each level invalidate that level and all subsequent levels" [vendor-documented, Anthropic prompt
  caching, accessed 2026-10-15]. So the frequently changing part sits late.
- **Preserved thinking.** The same three parts form the prefix that preserved thinking checks
  (section 7): `system` and `tools` must stay fixed between epochs.
- **Recall is declared from the first request.** At that point no thinking block exists yet, so
  adding the tool costs nothing. Adding a tool later would invalidate both the cache and every
  thinking block [vendor-documented].

## 2. Pinned state

**What is pinned**, from the ledger as of the turn before the window starts:

1. **Open obligations.** The header names the turn it is complete as of:
   `OPEN OBLIGATIONS as of turn N (complete as of that turn; the turns after it may open or close items):`.
   An item the list omits was closed by turn N, but a turn inside the window may reopen it. A
   window turn may also open a new item that the list cannot show.
   - The simulation checks this header against ground truth on every request.
   - An earlier wording ("complete list; an item not listed here is closed") was false in 72 of the
     242 default-session requests, while an obligation opened inside the window. Review found it,
     and the check now prevents it [measured, simulated].
2. **Active decisions.** Current value only, with the turn it was stated in. Superseded values are
   not shown; recall reaches them if needed.
3. **Verified facts.** Each line carries the turn it was stated in. A ledger-backed block also
   carries the record id and its provenance locator (section 9). Facts are listed newest first.

**Size bound**
- Default 2,000 tokens. The simulation uses characters / 4 as its estimate; the prototype should
  count with the provider's token counter.
- Priority is the M1 checkpoint order: obligations, then conflicts, then decisions, then facts.
- Obligations are never dropped. If they alone exceed the bound, the proxy surfaces an error to the
  operator instead of truncating.
- Decisions and facts that do not fit are dropped oldest first. The block ends with
  `OMITTED: decision=<n> fact=<n>`, and those records stay reachable by recall.
- The tests check the bound and that no open obligation is dropped [measured, simulated].

**Rendering:**

```
PINNED STATE (from the session ledger; authoritative over older turns)
OPEN OBLIGATIONS as of turn 215 (complete as of that turn; the turns after it may open or close items):
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
- **Epochs only at turn boundaries, with headroom.** The window holds turns `[first, now)`. Before
  a new turn starts, the proxy checks the completed turns in the window plus a headroom `H`.
  - If that sum would exceed `window_high`, the proxy pages whole completed turns from the start
    until they are at most `min(window_low, window_high - H)`. Any coming turn up to `H` tokens
    therefore fits under the bound.
  - Defaults: high 24,000, low 12,000, and `H` 3,000 tokens. In the synthetic session that `H`
    covers every turn: p50 701, p90 1,859, p99 2,720, max 3,100 estimated tokens [measured,
    simulated].
  - For real sessions, `H` should cover the workload's p99 turn growth. PR #8 states that figure
    from builder-3's profile and tool-heavy stress cases.
  - The bound itself should come from L2d's measured quality-versus-size curve, well below any size
    where quality slips.
- **The epoch.** Each move of `first` is an epoch, and it is the only time earlier content changes.
  At an epoch the proxy:
  - drops the turns before `first`;
  - re-renders the pinned block and the page index;
  - stubs large tool outputs in turns before the current one;
  - strips every thinking block from the request (section 7).
- **Between epochs.** Each request is the previous request plus appended messages. The simulation
  checks this property on every request: edit points equal window moves in every condition
  [measured, simulated].
- **Cache economics.**
  - An epoch costs one cache write of head plus window. A turn inside an epoch costs a cache read
    plus its new tokens [inferred].
  - Anthropic charges 1.25x base input for 5-minute cache writes, 2x for 1-hour writes, and 0.1x for
    reads, with reads at 0.05x on Opus 5.5 and Sonnet 5.5 and 0.025x on Fable 5.1 and Mythos 5.1
    [vendor-documented].
  - The epoch's thinking strip coincides with that cache restart, so it adds no extra cache break
    [vendor-documented: "the edits that invalidate thinking are the edits that restart the cache"].
- **No client-side paging inside an active tool-use turn.**
  - The rule [vendor-documented, https://platform.claude.com/docs/en/build-with-claude/thinking,
    accessed 2026-10-15]: "within a tool-use turn, pass thinking blocks back", "complete and
    unmodified". The preserved-thinking check also invalidates any thinking kept after an earlier
    edit (section 7).
  - So there is no valid client-side form of paging mid-turn. Stripping the turn's own thinking
    breaks the first rule. Editing earlier content while keeping it breaks the second. Any variant
    that keeps thinking across a prefix edit is **invalid**.
  - The likely consequence of trying anyway is that "the API doesn't error" but may "disable
    thinking", silently, for the rest of the turn [rule vendor-documented; consequence inferred].
    The tool-loop case is **untested**: our conformance probe had no tool loop.
  - An earlier version of this section proposed an "in-turn epoch" that did exactly this. It is
    withdrawn.
- **A turn that still crosses the bound.**
  - First, the headroom rule makes this rare: a crossing needs a turn larger than `H`.
  - **Fallback, labeled lossy:** server-side `clear_tool_uses_20250919` context editing. It is
    vendor-documented, not compaction, and exempt from the prefix check because it edits the
    server's copy [vendor-documented]. Its costs:
    - it restarts the cache from the first cleared block;
    - it is **lossy**: cleared results become placeholders with no id, and the proxy can bring them
      back only by paging those outputs itself at the next epoch.
    - It therefore fails the owner bar of accuracy equal to the full-context baseline, and is a
      last resort, not part of the design.
  - Otherwise the proxy lets the turn run over the bound and **reports it as over bound** in its
    log. The simulation counts such requests; there were 0 with the defaults [measured,
    simulated].
  - This wording is shared with PR #8 (build.builder-3).

## 4. What gets paged, and the stub format

**Paged**
- **Whole turns before the window start**, in fixed groups of 8 turns (0 to 7, 8 to 15, and so on).
  A completed group always has the same id; the trailing partial group is its own page.
- **Tool outputs larger than 1,500 tokens**, in window turns before the current one, at an epoch
  only. Only the `content` of the `tool_result` block is replaced; the block and its `tool_use_id`
  stay. Between epochs the set of stubbed outputs does not change.

**Never paged**
- Anything in the current turn.
- The system prompt, the tool definitions, and the pinned block.

The tests check that the current turn's tool outputs are never stubbed, and that epochs happen only
at turn boundaries.

**Page id**
- `pg_` + the first 16 hex characters of HMAC-SHA256(store key, canonical JSON of the page payload).
- The store key is random, generated once per store, and kept beside the pages, outside Git.
- Keyed, so an id cannot be checked against guessed content. Same content under the same store,
  same id [measured, simulated].

**Stub grammar**

```
turn page:   [paged pg_<16 hex> | turns <a>-<b> | <n> msgs | ~<tokens> tokens | <h_a> / <h_a+1> / ... / <h_b>]
tool output: [paged pg_<16 hex> | tool_result of <tool>(<canonical input JSON>) | ~<tokens> tokens | "<headline>"]
```

- **Headlines.** Each `<h>` is that turn's opening user text and `<headline>` is the tool output's
  first text. Both are whitespace-collapsed and cut to 40 characters at a word boundary, with
  ` ...` appended, so a partial value never appears.
- **Page index.** One text block at the start of the window's first user message:
  `PAGED HISTORY (use the recall tool with an id to read a page verbatim):` followed by one stub per
  line. It is rebuilt only at epochs.
- **Index bound** [inferred, designed, not simulated]. Above an index budget (proposed 4,000
  tokens), the oldest stubs fold into epoch stubs `[paged-epoch pe_<16 hex> | turns <a>-<b> | <k> pages]`.
  Recalling one returns its stub list.

## 5. Recall tool contract

**Definition** (declared by the proxy from the first request, never by the CLI):

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
- `text` is the verbatim page, sliced to at most 32,000 characters. `next_offset` is null at the end.

**Errors**, returned as a `tool_result` with `is_error: true`:
- `bad_id`: the input is not shaped like a `pg_` id.
- `unknown_id`: no such page.
- `bad_offset`: offset outside the page.
- `recall_limit`: more than 8 recall calls in one user turn.

**Extension, ledger-backed** [inferred]: accept M1 record ids (`rec_...`), returning the full record
with its provenance.

## 6. What a proxy-added tool requires

The CLI never declared `recall`, so it must never see a `recall` call. These requirements are
[inferred] until lane 1's question 1 evidence (PR #9) confirms streaming and request behavior per CLI.

1. **Buffer before forwarding.** The proxy holds a streamed assistant message until it knows
   whether the message calls `recall`. That costs streaming latency on tool-calling turns, and lane
   1 should measure it.
2. **Hidden round trips.** For each `recall` the proxy executes it, appends the assistant message
   and a user message with the matching `tool_result`, and calls upstream again. The prefix only
   grows, so these calls read from cache and keep thinking valid. The proxy repeats until the
   model stops calling `recall` or the limit forces `recall_limit` errors. Only the final response
   is streamed to the CLI.
3. **Mixed calls.** If one assistant message calls both `recall` and a CLI tool, every `tool_use` in
   it must be answered in the next message. Inside the hidden exchange, the proxy answers each CLI
   call with `is_error: true` and "call this again after recall finishes". The model re-issues it
   in a later message, which the proxy forwards.
4. **Reinsert the hidden exchange byte for byte until the next epoch.** The CLI's next request will
   not contain the hidden exchange, but the model's later thinking was produced with it in place.
   Leaving it out would edit earlier history and invalidate that thinking [vendor-documented rule,
   section 7]. So the proxy:
   - stores each exchange;
   - splices it back, unchanged, at the same position in every later request of the epoch;
   - counts it toward the window size.
   At the next epoch it is paged out with the rest of its turn, and thinking is stripped as usual.
5. **Usage reporting.** The hidden calls are billed but not visible to the CLI. The proxy logs them
   itself; question 2 decides what usage the CLI is shown.
6. **Thinking.** Earlier thinking blocks are preserved and billed by default on Fable 5.1, Opus 5.5,
   Sonnet 5.5, and Haiku 5.5 [vendor-documented]. An earlier version of this section said the API
   drops them, which was wrong. Section 7 states the constraint and the strategy.

## 7. Preserved thinking: the constraint and the chosen strategy

**The rule** [vendor-documented, https://platform.claude.com/docs/en/build-with-claude/preserved-thinking,
accessed 2026-10-15]:
- On Fable 5.1, Opus 5.5, Sonnet 5.5, and Haiku 5.5, "a thinking block stays valid only while
  everything you sent before it is unchanged". The checked prefix is the top-level `system`, the
  set of `tools`, and every earlier message.
- Invalid edits include:
  - editing, reordering, or deleting any earlier message;
  - clearing or shortening an earlier `tool_result`;
  - adding a text block to an earlier user turn;
  - changing `system`;
  - adding, removing, or editing a tool.
- Valid: appending messages; removing thinking blocks "from the start of the history, from the
  end, or all of them"; server-side compaction or context editing.
- The page names this policy's pattern directly. "Client-side trimming" (dropping the oldest turns
  and keeping recent ones verbatim) fails, because the kept turns' thinking was produced with the
  removed history in place.
- On failure, accounts created on or after 2026-08-31 get a 400 by default. Older accounts pass the
  block through unless `thinking.block_binding.prefix_mismatch_behavior` is set, which needs beta
  `thinking-binding-controls-2026-08-01`. `drop_block` drops failing blocks and every later one.

**Conformance probe** [measured, 2026-10-15, `spikes/m0/conformance/probe.py`, 4,973 tokens total]:
- Setup: `claude-haiku-5-5` with adaptive thinking, through the rig proxy. History holds two
  assistant turns with signed thinking.
- `prefix_mismatch_behavior: "error"` was set where noted.

| Case | Status |
|---|---|
| append only | 200 |
| strip all thinking | 200 |
| drop oldest turns, keep later thinking, no beta header | 200, nothing reported |
| same, with `error` | 200, but the kept block was dropped (`thinking_dropped`, reason `end_user_binding_mismatch`) |
| change `system`, keep thinking, `error` | **400** "Invalid `signature` in `thinking` block. The block is bound to a different conversation." |
| add a text block to an earlier user turn, keep thinking, `error` | **400** (same message) |
| full epoch rewrite (drop oldest, new `system`, page index text) with all thinking stripped, `error` | 200 |
| drop oldest with `drop_block` | 200, block dropped (`end_user_binding_mismatch`) |
| server-side `clear_thinking_20251015` | 200 |

What the probe shows:
- This account does not enforce by default: every edit passed with no header. A newer account
  would get 400s.
- The reason `end_user_binding_mismatch` was not on the part of the vendor page we read.

**The four strategies compared:**

| Strategy | Valid? | Accuracy cost | Cache cost | Fits the owner principle? |
|---|---|---|---|---|
| A. Strip all thinking at each epoch; append-only between epochs | yes [vendor-documented; measured: epoch rewrite with strip returns 200] | the model loses earlier reasoning at each epoch. Vendor estimate: output tokens +2.5% (low effort) to +20.1% (max) when dropped once per session, +4.6% to +67.1% when dropped every turn; "wide error bars" [vendor-documented]. With an epoch about every 14 turns in the simulation, the cost sits between those [inferred]. Facts and decisions in visible text are unaffected. | none beyond the epoch's own cache restart, which happens anyway [vendor-documented] | yes: bounded context, no compaction |
| B. Server-side `context_management` (clear tool uses, clear thinking) | yes [vendor-documented; measured: `clear_thinking` 200] | none for what is kept, but the client must send the full, growing history. It cannot drop old turns, insert pinned state, or index pages | "clearing invalidates cached prompt prefixes" [vendor-documented] | no: working context is not bounded, so the CLI still reaches compaction |
| C. Byte-for-byte reinsertion of all prior content | yes [vendor-documented] | none | none | no on its own: reinserting everything means never paging. It is the right rule for hidden recall exchanges within an epoch (section 6, item 4) |
| D. Keep thinking-bearing turns in an untouched recent suffix, edit only before it | **no** [vendor-documented "client-side trimming" fails; measured: the kept block is dropped, or 400 under `error` for other prefix edits] | thinking is lost anyway, and new accounts get 400s | as A | no |

**Choice: A**, at turn boundaries only, combined with C for hidden recall exchanges.
- A is the only strategy that keeps working context bounded and every request valid at turn
  boundaries.
- Inside an active tool-use turn no client-side strategy is valid (section 3). Tool-heavy turns are
  handled by headroom, with a lossy server-side fallback that fails the owner bar.

The cost of A:
- earlier reasoning is lost at each epoch, never facts, decisions, or obligations;
- some extra output tokens.

The window bound and epoch spacing set how often that cost is paid. The L2d strip-thinking arm
(approved for checkpoints 1 and 2) measures the accuracy cost of A directly against preserved
thinking on the same probes.

**What the simulation does not cover.** The fidelity simulation has no thinking blocks. It checks
only the structural property that strategy A needs: earlier content changes only at epochs
[measured, simulated].

## 8. Tool-use and tool-result pairing survives paging

- **Turn-granular cuts.** Every `tool_result` sits in the same turn as the `tool_use` before it, so
  cutting between turns cannot split a pair.
- **Stubs keep the block.** Paging a tool output at an epoch replaces `content` only; the `type` and
  `tool_use_id` remain. Nothing in the active turn is paged.
- **Hidden recall pairs stay complete.** They exist only in hidden round trips, and are reinserted
  whole (section 6).
- **Roles keep alternating.** The page index rides inside the window's first user message, so no
  new message is inserted.
- **Tested.** `check_protocol` validates role alternation and pairing for all four conditions on
  every turn of the 242-turn default session and the 120-turn test session [measured, simulated].

## 9. Backing the pinned state with the M1 ledger

M1 (`docs/milestones/M1-ledger-and-recovery.md` on `main`; implementation PRs #2, #3, #4 paused)
already has most of the machinery.

| Pinned element | M1 source |
|---|---|
| Open obligations | `contexto.state.derive(...).open_obligations` |
| Active decisions | active `decision` records; same-topic conflicts become `CONFLICT` items |
| Verified facts | active `verified` records, which M1 requires to carry provenance |
| Stale warning | `state.stale`, rendered under a stale heading |
| Bounded rendering | `contexto.checkpoint.build` uses this priority order and never drops an obligation |
| Epoch identity | the ledger `head()` hash: re-render at an epoch only if the head changed |

**What M1 lacks for this use** [inferred]:
- **Capture.** Nothing writes ledger records from a live conversation. Pinned state is only as good
  as capture, and the fidelity simulation assumes perfect capture.
- **Page store.** M1 has no store for verbatim pages, no keyed ids, and no recall.
- **Record-to-page links.** M1's `transcript` source type has a free-form `locator` that could carry
  `pg_` ids. That needs pinning in a contract.
- **Token budgets.** The checkpoint budget is in characters; the pinned block needs a token budget.
- **No "fact" kind.** Verified facts map to `event`, `artifact`, or `reference` records.

## 10. Prior art and H5 (provider-native features)

Sources were read on 2026-10-15 unless a date is given.

### MemGPT (paper) and Letta

- **What it does.**
  - MemGPT splits the prompt into "system instructions, working context, and FIFO Queue". The
    queue's head is "a recursive summary of messages that have been evicted". Evicted messages are
    "stored indefinitely in recall storage and readable via MemGPT function calls"
    [vendor-documented, i.e. the authors' paper: Packer et al., arXiv 2310.08560 v2, 2024-02-12].
  - On deep memory retrieval, GPT-4 went from 32.1% to 92.5% accuracy [measured, by the authors,
    Table 2].
  - Letta persists all messages and compacts with `compaction_settings.mode` (default
    `sliding_window`, `sliding_window_percentage` 0.3). Its archival memory is "a semantically
    searchable database" [vendor-documented, docs.letta.com, accessed 2026-10-15].
- **Trigger.** A "warning token count (e.g. 70% of the context window)", then eviction at the flush
  count [vendor-documented].
- **Why it falls short** [inferred]:
  - Retrieval is by search, with embeddings for archival memory.
  - Evicted turns surface as a recursive summary.
  - It owns the agent loop rather than sitting under an existing CLI.

### Anthropic API: context editing, memory tool, compaction, prompt caching, preserved thinking

- **Context editing.**
  - Beta `context-management-2025-06-27`, strategy `clear_tool_uses_20250919`. `trigger` defaults
    to 100,000 input tokens and `keep` to 3 tool uses. Cleared results become "placeholder text"
    [vendor-documented, platform.claude.com context-editing, accessed 2026-10-15; announced
    2025-09-29].
  - Clearing "invalidates cached prompt prefixes" [vendor-documented].
  - Anthropic reports an 84% token reduction in a 100-turn eval [measured, by Anthropic, blog
    2025-09-29].
- **Memory tool.** `memory_20250818` is client-side, with `view`, `create`, `str_replace`,
  `insert`, `delete`, and `rename` under `/memories` [vendor-documented, accessed 2026-10-15].
- **Server-side compaction.**
  - Threshold mode: `compact-2026-01-12`, `trigger` default 150,000. In this mode "the API
    automatically drops all content blocks prior to the `compaction` block".
  - On-demand mode: `compact-2026-09-04`, which returns a signed `compaction` block. Editing the
    block fails with `compaction_signature_invalid`, and sending messages before it returns 400
    `compaction_block_misplaced`.
  - [vendor-documented, compaction-threshold and compaction-on-demand pages, accessed 2026-10-15]
- **Prompt caching (H5).**
  - At most 4 breakpoints and a "5-minute lifetime" refreshed on use, or `"ttl": "1h"`.
  - Writes cost 1.25x or 2x; reads cost 0.1x (0.05x on Opus 5.5 and Sonnet 5.5, 0.025x on Fable 5.1
    and Mythos 5.1).
  - The minimum cacheable prefix is 512 tokens on most 5.x models and 1,024 on Sonnet 5.
  - "Cache hits require 100% identical prompt segments" [vendor-documented, prompt-caching, accessed
    2026-10-15].
- **Preserved thinking.** Earlier thinking stays in context and is billed by default, and edits
  before it invalidate it (section 7) [vendor-documented].
- **Why it falls short.**
  - Cleared results have no id and no way back, and compaction is a signed, lossy summary
    [vendor-documented mechanics; inferred conclusion].
  - Caching makes the growing history cheaper but never bounds it [inferred].

### OpenAI: conversation state, truncation, compaction, prompt caching

- **Conversation state.** With `previous_response_id`, "all previous input tokens for responses in
  the chain are billed as input tokens" [vendor-documented, conversation-state guide, accessed
  2026-10-15].
- **Truncation.** `truncation: auto` drops "items from the beginning of the conversation"
  [vendor-documented].
- **Compaction.** `/responses/compact` (changelog 2025-12-11) and server-side `context_management`
  compaction (changelog 2026-02-10). Its output is "encrypted" and "opaque" [vendor-documented].
- **Prompt caching (H5).**
  - Automatic, at 1,024 tokens minimum for GPT-5.6 and later, and "Cache reuse requires the entire
    rendered prefix to match".
  - Reads cost 0.1x on GPT-5.6 and later and 0.05x on GPT-6.1 Sol; writes cost 1.25x
    [vendor-documented, prompt-caching guide, accessed 2026-10-15].
- **Encrypted reasoning and history edits.** Our probe sent `gpt-6-astra` histories, with
  `store: false`, that edited or removed a user turn before an encrypted reasoning item. The API
  returned 200 [measured, 2026-10-15]. Whether the reasoning item was then used or ignored is not
  observable from the response.
- **Why it falls short.** Truncation is permanent loss, compaction is opaque, and caching does not
  bound context [vendor-documented, inferred].

### Claude Code compaction

- **What it does.**
  - Automatic compaction "clears older tool outputs first, then summarizes the conversation if
    needed" [vendor-documented, code.claude.com how-claude-code-works, accessed 2026-10-15].
  - The summary keeps "requests and intent, key technical concepts, files examined or modified...,
    errors..., pending tasks, and current work", and full tool outputs "are gone"
    [vendor-documented, code.claude.com context-window, accessed 2026-10-15].
- **Trigger.**
  - At the model's context limit: about 967K tokens for 1M-window models. 200K applies only to
    Sonnet 4.6 and Opus 4.6 without extended context, or with `CLAUDE_CODE_DISABLE_1M_CONTEXT=1`
    [vendor-documented, code.claude.com model-config, accessed 2026-10-15].
  - Lane 1 measured the blocking point at about the window minus 23k (about 977K on a 1M window)
    [measured, lane 1, PR #9].
  - Tunable with `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100000 to 1000000), and can be disabled with
    `DISABLE_AUTO_COMPACT` [vendor-documented, env-vars, accessed 2026-10-15].
  - Lane 1 found that `--autocompact` and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` did not force pre-send
    compaction in `-p` resume [measured, lane 1, PR #9].
- **Our observations** (CLI 2.1.301, `claude-haiku-5-5`, headless, manual `/compact`).
  - `/compact` shrank a 29.7k-token context to 4.3k and kept all 37 planted values.
  - Its own model call reported zero `usage`. The session's cumulative `modelUsage` grew by 34,358
    tokens [measured; the figure is derived from recorded cumulative `modelUsage`].
- **Why it falls short.** It is a lossy summary at the point where quality is already degrading
  (owner principle), and its cost does not show in per-turn usage [vendor-documented, measured].

### Codex CLI compaction

- **Requests.** Codex sends the full input on every request (`store: false`, no
  `previous_response_id`) and auto-compacts at about 90% of a 272k window [measured, lane 1,
  PR #9].
- **Configuration.** `model_auto_compact_token_limit`, capped in source at 90% of the window
  [vendor-documented, Codex config reference, accessed 2026-10-15; `openai_models.rs` at
  openai/codex main 16e578dd1d, 2026-10-15].
- **Local mode** keeps the initial context, recent user messages up to
  `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`, and a summary. Assistant turns and tool outputs are
  dropped, except retained tool outputs [vendor-documented, `compact.rs`].
- **Remote mode.** `remote_compaction = "v2"` compacts through the Responses API
  [vendor-documented, `compact_remote_v2.rs`, last commit 2026-10-13].
- **Why it falls short.** It keeps only a summary or opaque items, truncated tool output is gone,
  and there is no recall path [vendor-documented, inferred].

### What none of them provide

No system we found keeps the full history outside the model, under the user's control, behind
stable ids the model can dereference, while bounding every request well below the compaction point
[inferred].

**Not verified**:
- The placeholder text for cleared tool results.
- Whether Claude Code uses Anthropic's server-side compaction.
- Codex's default `remote_compaction`.
- Letta's `conversation_search` implementation.
- The `end_user_binding_mismatch` reason in our probe.

### Sources

All pages without a visible date were accessed 2026-10-15.

- MemGPT, arXiv 2310.08560 (v1 2023-10-12, v2 2024-02-12): https://arxiv.org/abs/2310.08560
- Letta docs:
  - compaction: https://docs.letta.com/v1-sdk/messages/compaction
  - archival memory: https://docs.letta.com/v1-sdk/memory/archival-memory/index.md
  - context engineering: https://docs.letta.com/guides/agents/context-engineering
- Anthropic, "Managing context on the Claude Developer Platform", 2025-09-29: https://claude.com/blog/context-management
- Anthropic docs:
  - context editing: https://platform.claude.com/docs/en/build-with-claude/context-editing
  - threshold compaction: https://platform.claude.com/docs/en/build-with-claude/compaction-threshold
  - on-demand compaction: https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand
  - memory tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
  - prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
  - preserved thinking: https://platform.claude.com/docs/en/build-with-claude/preserved-thinking
- OpenAI docs:
  - conversation state: https://developers.openai.com/api/docs/guides/conversation-state
  - compaction: https://developers.openai.com/api/docs/guides/compaction
  - prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching
  - changelog: https://developers.openai.com/api/docs/changelog
- Claude Code docs:
  - https://code.claude.com/docs/en/how-claude-code-works
  - https://code.claude.com/docs/en/context-window
  - https://code.claude.com/docs/en/model-config
  - https://code.claude.com/docs/en/env-vars
- Codex:
  - config reference: https://learn.chatgpt.com/docs/config-file/config-reference
  - source at openai/codex main 16e578dd1d (2026-10-15)
- Lane 1 measurements: `docs/research/M0-interception.md`, PR #9 (VentureForgre/contexto).

## Open questions

1. **Capture.** Who writes ledger records live: a proxy-added `record` tool, the operator, or hooks?
2. **Window bound.** Set from L2d's quality curve. The cost model (L2a) prices epoch spacing.
3. **Accuracy cost of strategy A.** The L2d strip-thinking arm measures it on the same probes.
4. **Proxy details.** Streaming hold-back latency, mixed-call handling, and usage reporting per CLI
   (lane 1).
5. **Index folding.** Designed but not simulated.
6. **The tool-loop case.** A conformance case that strips or keeps thinking inside a real tool loop
   would turn the inferred silent-disable consequence into a measurement. It is untested so far.
