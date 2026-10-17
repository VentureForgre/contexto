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
and a system message with empty `content` can appear. Lane A resolved that last item (rule 12; measured by builder-2, evidence in `spikes/m2/carried/` (PR #30,
reviewed)):
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
   user-then-system shape. Measured by builder-2, evidence in `spikes/m2/carried/` (PR #30, reviewed)
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
   estimated upstream input would exceed `bound - headroom`, or when the per-turn recall room that
   would be published for the new turn (`bound - estimate - headroom_floor`) is below `min_turn_room`
   (default 12,000, about 1.7 times the largest per-turn evidence measured in the engine-driven
   6x9 estimate, 6,964 tokens; 20,000 made the engine rotate on 10 of 12 content turns at bound
   150,000 with 40 KB turns, each a cache miss and a thinking strip); without that second condition the room is smallest on the turn just before a
   rotation, because both shrink toward the same line (engine-driven estimate, PR #113: 5,219 at
   the start of turn 4, 738 at its lowest). A rotation fired by the room condition sizes its window
   target as `0.5 * (W - min_turn_room)` instead of `0.5 * W` (rule 4), falling back to the minimal
   window, so the room it publishes is at least `min_turn_room`. It fires only if that rotation
   would actually restore a room of at least `min_turn_room`; otherwise the engine does not rotate
   for the room and logs `turn_room_low` once per epoch. When both triggers fire at the same turn
   start, the room-sized target is used if that rotation would restore the room and page at least
   as far as the bound trigger's target; otherwise the bound trigger's own target applies. A head
   change and the minimal window keep their own sizing. A turn that starts below `min_turn_room`
   and still publishes a room below it after the decision logs `turn_room_low`, once per epoch. This keeps a small bound from rotating on
   every turn (reviewer-2: a plain re-trigger would thrash at bounds of 42,000 to 100,000).
   `budget.simulate` models both conditions. Estimate = last upstream
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
   within that target, retaining the newest complete turn and the new turn (except under the
   minimal-window fallback below), and never below
   the current `E.k` (a rotation never un-pages a turn already in the store). If the chosen `k`
   equals `E.k`, the rotation is a no-op: nothing is paged and no thinking is stripped. Write the
   newly paged turns to the page store, one page per turn, minus carried content; add any newly
   paged system messages to `carried`; rebuild `pinned_text` and `index_text`; strip thinking per
   rule 2. If `W` cannot hold the newest complete turn and the new turn together, retain
   only the new turn (the newest complete turn is paged like any other and stays recallable) and
   log `window_minimal`; this keeps the request under the bound at the cost of the previous turn's
   verbatim text. Only if `W` is not positive or cannot hold even the new turn, log
   `bound_too_small` and still rotate to the minimal window (the new turn alone), which is
   smaller than sending unrotated; the launcher warns at start when the measured
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
   Log every 4xx with the epoch state for diagnosis. Every locally generated upstream failure is
   logged with its phase (`connect`, `tls`, `send`, `wait_headers`), elapsed milliseconds, and
   errno, content-free. One exception to never retrying, from the second live H1 run (a locally
   generated 502 after an 18.2 s OS-level timeout; the proxy opens a fresh upstream connection for
   every request): if the failure happened before the request body was fully written (`connect`,
   `tls`, or mid-`send`), upstream cannot have acted on the request, so the proxy resends the same
   bytes once on a new connection and logs `upstream_retry` with the original phase and error. A
   failure after the body was fully sent (`wait_headers`) or after any response byte is never
   retried, because upstream may be running it; it returns the 502 as before, and the CLI may
   retry on its own.
9. **Streaming.** Relay SSE events as they arrive, except for the stream hold described under the
   per-turn recall budget; the proxy reads `message_start` and `message_delta` usage.
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
    client-side edit precedes kept thinking (zero-spend stubs; measured by builder-2; evidence in
    `spikes/m2/carried/`, PR #30, reviewed). If a
    later CLI version starts emptying them, re-run that check before relying on kept thinking.
13. **Carried growth.** Measured by builder-2 without hooks (zero spend, real CLI-written history,
    one fresh start and five resumes; evidence in `spikes/m2/carried/`, PR #30,
    reviewed): system messages
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
14. **Transform failures.** Expected failures never reuse the component that failed, and only the
    terminal step below may forward the CLI's original request:
    - **Degraded mode** (every request sent in it is logged with `degraded: true`) is entered on `page_conflict` (a stored page differs in
      `{turn, messages}` under its id, which can only mean corruption) or `store_error` (the store
      cannot be read or written). In degraded mode the engine pages nothing and never rotates: it
      sends the last good transform (the current `E`, with its in-memory `pinned_text` and
      `index_text`) extended append-only with the new messages. That is always protocol-valid,
      keeps thinking and the cache intact, and loses nothing, because no turn is paged while the
      store is broken. Requests may then exceed the bound; each is logged `over_bound` and fails
      acceptance. At each new turn start the engine tries the store once; if it is healthy, it leaves
      degraded mode and rotates normally. A persistent `page_conflict` is resolved by moving the
      corrupt stored page to quarantine (never deleted) and paging the turn again from the CLI's
      current messages, logged `page_quarantined`. Mid tool loop
      nothing changes, because degraded mode only appends.
    - **Terminal step.** A failure inside degraded mode, or a resync (rule 10) that cannot be
      completed without paging while the store is broken, is final for that request: the proxy
      forwards the CLI's original request and logs `fallback_failed`. Each request enters each
      fallback at most once; there is no retry loop.
    - **Genuine bugs.** An exception on a path not listed by lane A is handled the same way: the
      original request is forwarded so the user is never blocked by a Contexto defect, logged as
      `transform_error` at error level, and reported prominently by `contexto sessions show`.
    Acceptance counts these and the other off-path events listed in acceptance 1 as failures. Lane
    A lists every engine path that can raise and covers each with a fault-injection test.
15. **Encoded request bodies.** The launcher disables request compression, so a compressed
    request body means a misconfigured client. The proxy decodes the encodings the standard
    library supports (`gzip`, `deflate`, and `zstd` on Python 3.14 or newer), transforms the
    request as usual, and forwards it uncompressed. Any other encoding, or a body that fails to
    decode, is rejected with HTTP 415 in the API's error format, with a message beginning
    `contexto:` that names the encoding and the fix (run through `contexto run`, or disable
    request compression), logged `encoded_body_rejected`. The request is never forwarded
    unbounded. This is client misconfiguration, not a Contexto defect, so rule 14's
    degrade-instead-of-block policy does not apply.
16. **Resuming an epoch after a proxy restart.** A new engine on an existing session continues
    the main conversation's current epoch instead of rotating again from epoch 0, so a restart
    keeps the prompt byte-identical to what the previous process would have sent and the cache
    still holds. Measured basis (A28, builder-1, zero-spend replay of the Codex acceptance run,
    PR #174; reviewer-1 confirmed the cause from the run's log and its own replay): the harness started a new proxy for each of 21 `codex exec` turns, every
    restart rotated from epoch 0, and replaying with epoch state resumed cut rotations from 19 to 9
    and modeled cost by 9.8 to 16.2 percent, depending on the bytes-per-token rate used for the
    changed content. Any user whose proxy restarts mid-session hits the same gap.
    - **Record.** At every turn start of the main conversation (rotations happen only there)
      and whenever the engine leaves degraded mode, the engine writes the main conversation's
      epoch record, after that request's transform and before it is sent (as for
      `set_retained`; a failed write never blocks the send). The record holds every piece of
      engine state that decides later request bytes or rotation decisions, so a resumed engine
      behaves exactly as the old one would have: `epoch`, `k`, `strip_upto`, `turns_paged`, the
      exact `pinned_text` and `index_text` sent in that epoch, a SHA-256 of the rendered preamble
      as sent, `head_fingerprint`, `paged_carried`
      (hashes only), `head_tokens`, `initial_carried`, `max_turn_growth`, the turn-start counters
      (`last_turn_start_n`, `last_turn_count`, `last_carried_text_count`, `carried_text_streak`),
      `window_budget`, `window_minimal_count`, and `turn_room_low_epoch`; the whole engine
      configuration (every setting, including the recall preamble options and budgets); the
      dialect; the model named by the request; the conversation's identity as the engine keys
      it (a hash); and an anchor: the count `n` of messages in the request that wrote the record
      and a hash over their normalised hashes (rule 10). Per-request values (the previous
      request's hashes, sizes, and estimates) are not recorded. It is a full atomic overwrite at
      mode 0600, never logged, never sent upstream, and holds conversation text only in
      `pinned_text` and `index_text`, which the store already holds in other forms. A degraded
      engine (rule 14) writes no record.
    - **Resume.** A new engine reads the record once, at the first request of a conversation
      whose identity matches the record's, and resumes only if every check holds: same dialect,
      model, and configuration; the preamble the engine renders from the recorded texts with its
      own code hashes to the recorded SHA-256 (so a code change that renders differently does not
      resume); the request's head fingerprint equals the record's; the request has at least `n`
      messages; and its first `n` messages hash to the anchor. A resume adopts every recorded
      field, seeds rule 10's previous-request hashes with the hashes of those first `n` messages
      (so the request is matched as an append and does not start a new epoch), makes this
      conversation the main one, and continues append-only (rules 1 to 3). A resume at a turn
      start applies rule 4's decisions (room and size) to that request itself. The record holds no
      per-request usage, so that first decision uses a full estimate instead of the previous
      request's real input; at a borderline it can differ from what an uninterrupted engine would
      have decided (reviewer-2's differential for A34: 17 of 17 restart points byte-identical in
      every case but Codex at 3.0 bytes per token, where one restart at the session's last turn
      start did not rotate where the uninterrupted engine did, and none went over the bound). A
      resume mid-turn
      is sent under rule 5 (a logged in-turn over-bound turn if it is over). The newest record
      written is trusted: if a write and its invalidation both failed, an older record can still
      be found, which is consistent but has a smaller `k`, and the two rules above bound the
      harm. Any failed check, an unreadable or malformed record, or no record starts the epoch
      from 0 as before, and the engine logs content-free `epoch_not_resumed` with one reason
      (`no_record`, `unreadable`, `dialect_changed`, `model_changed`, `settings_changed`,
      `preamble_changed`, `head_changed`, `history_shorter`, `history_diverged`,
      `other_conversation`, `pages_missing`: the store holds fewer pages than the record's
      `turns_paged`, packet A31). A resume logs `epoch_resumed` with `epoch` and `k`. Measured sizing
      (packet A8's ratio restore) is unchanged and still applies.
    - **Who writes.** Only the main conversation writes. After a restart, no conversation writes
      until one has resumed from the record or has strictly more turn starts than the record's
      `last_turn_count`, so a side request that arrives first cannot overwrite it.
    - **Store interface.** `SessionStore.set_epoch(record)` writes `epoch.json` atomically (temp
      file, fsync, rename) at
      mode 0600 with `version: 1`, never logged; on a failed write it unlinks the previous record
      and raises. `SessionStore.epoch_record()` returns the record or `None`. The store validates
      shape and version (an unknown version returns `None`); the engine validates meaning (the
      resume checks). `MemoryStore` behaves the same.
    - **Write failures.** A failed `set_epoch` leaves no usable record (the store unlinks the
      previous one), so a later restart cannot resume a stale epoch, and the engine logs
      `epoch_record_stale`. It does not
      enter degraded mode: the record only saves cache, and losing it costs one rotation.
    - **Not covered.** Subagents and side requests are not recorded (they rotate from 0 after a
      restart, as today). A record written by an older version, or with a field missing, does
      not resume. Two proxies serving one session at once are out of scope (the last writer
      wins).

17. **Headroom.** At a turn start, headroom = max(`headroom_floor`, min(ceil(`headroom_factor` x
    `max_turn_growth`), floor(`headroom_cap` x bound))), with `headroom_cap` = 0.25. At each turn
    start where the engine measures the turn's growth g (as before: a previous turn start is
    recorded and the turn count has grown), and before the rotation decision, `max_turn_growth` =
    max(floor(previous `max_turn_growth` x `headroom_decay`), g), where g is the tokens of the
    messages added since the previous turn start, per turn start (averaged when several turns
    arrive at once), as before, and `headroom_decay` = 0.5. A turn start sent
    again with no new turn (a retry) measures no growth and does not decay. So one outlier turn
    raises headroom by at most a quarter of the bound, and its weight halves at every later turn
    start that measures growth. Both are configuration settings (`headroom_cap`,
    `headroom_decay`), included in rule 16's configuration check. Rule 16 records
    `max_turn_growth` as before (one integer): a resumed engine applies the cap at every use and
    the decay at its next turn start that measures growth (not at the turn start that wrote the
    record), so it forgets exactly as the previous engine would have. A record written by an
    engine without these settings does not resume (`settings_changed`). Headroom enters only the
    turn-start rotation decision and the window budget `W` (which an engine also sets once from
    the measured head after its first response, packet A8); the in-turn over-bound check never
    reads it, so rule 5 is unchanged: nothing rotates inside a tool loop, and an in-turn request
    over the bound is logged as before. Measured basis (A33, `docs/research/M2-headroom-outliers.md`,
    zero-spend replay, reviewed in PR #184): on A32's synthetic seat session with one 149,140-token
    read (92k head, bound 150,000), the unbounded headroom left the session at the minimal window
    for good (34 rotations, 27 minimal windows, `W` as low as -136,986); with this rule, 15
    rotations, 1 minimal window, and `W` never below 18,907. Live run #3 and the Codex acceptance
    run are unchanged. The cost: in synthetic sessions with two large reads, a band of second-read
    sizes (about 6,000 tokens wide three turns later, up to about 36,000 eight turns later with a
    30,000-token head) now goes over the bound inside its tool loop where the stuck session's
    minimal window had kept it under; the largest later read kept under the bound is never below
    that of a session with no outlier at the same turn.

Defaults (configurable flags and env): `bound` 150,000 tokens (D31); `headroom` 1.25 times the
largest per-turn growth, decaying and capped per rule 17, floor 25,000; `window_fraction` 0.5 of the window budget `W` (rule 4).
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
  must not be guessable from their id). Pages are immutable: paging a turn whose id
  already exists is a no-op that keeps the stored page, and only a differing `{turn, messages}`
  under the same id is a conflict. No migration is needed, because no real stores existed before
  this rule.
- **Ledger:** the M1 record model (kinds `event`, `decision`, `obligation`, `artifact`,
  `reference`, `closure`; epistemic `verified`, `reported`, `summary`, `hypothesis`; provenance;
  supersession; open obligations) harvested from PR #2 (record model), PR #3 (state derivation),
  and builder-2's unpushed ledger work at commit 1ab5f29 (readable from the shared object store),
  append-only JSONL with a hash chain and crash-truncation repair. File sources require `sha256`;
  a missing file counts as stale. Records may cite pages as provenance (`type: page`).
- **Pinned text** starts with fixed instruction lines, then lists open obligations, active
  decisions, then verified facts, newest first, each with its id, capped at `pin_budget` (default
  6,000 tokens). The instruction lines must say that earlier turns are paged out and how to reach
  them (`contexto_search` with an exact key, name, or identifier, then `contexto_recall` with a page
  id); that the model does this before relying on a detail from an earlier turn and before
  answering that something is unknown; that it records decisions and obligations with
  `contexto_pin` and closes finished ones with `contexto_close`; and one worked example of a
  search followed by a recall. The preamble is placed before any carried context in the first
  retained turn-start message. Measured (PR #70 experiment, 415,413 tokens): with the earlier
  mechanism-only line the model made 0 tool calls in the H1 run; with this text it searched for
  all four paged-only keys in its first response. Over budget: list the newest, then a
  count of omitted items and a pointer to `contexto_state`. It always starts with one line telling
  the model that earlier turns are paged out and how to recall them.
- **Index text** = one line per page (id, turn number, stub, and `[ids: ...]` listing the page's
  identifiers such as keys, names, and paths, never values after `=` and never identifiers
  containing digits, so the index shows where a fact lives without giving it away), newest first, capped at
  `index_budget` (default 4,000 tokens), with a count of older pages omitted.

## MCP server

`contexto mcp --session S`, stdio JSON-RPC per the MCP spec, tools:

- `contexto_recall(page_id)` returns the page's messages rendered as plain text (thinking omitted),
  exactly, up to a per-call limit with `offset` for more. The limit is the smaller of 32,000
  characters and about one quarter of the session's window budget `W` in tokens, converted with the
  smaller of the session's measured bytes-per-token ratio and the 1.5 `tool_result` seed (recalled
  pages are often numeric, and the session average would let one recall reach about 0.42 `W`);
  until `set_window` has been called, the limit is 12,000 characters; the engine publishes `W` and the ratio through the
  session store (`SessionStore.set_window(window_tokens, bytes_per_token)`, kept in the private
  session metadata). Reason: a 32,000-character recall of numeric log text is about 17,800 tokens,
  more than half of an OpenRig seat's `W` near 33,000, and recall results arrive inside a tool
  loop where the engine cannot rotate (D17).
  **Per-turn recall budget.** Recall results stay in the window until the turn ends, so a turn
  that recalls too much would push its own requests over the bound. Rules:
  - The **turn id** is the number of turn-start messages in the CLI's full history, so it is
    stable across rotation and proxy restarts.
  - The **room** is `bound - (input + output) - headroom_floor`, from the response's real usage
    (the output, including thinking and `tool_use`, is passed back in the loop). No `max_tokens`
    reservation: Claude Code sends `max_tokens` 128,000 on every request, which would leave no room.
  - The engine publishes `(turn_id, room_tokens)` through `SessionStore.set_turn_room` at request
    time, from the pre-send estimate (no `max_tokens` reservation), and again when the response's
    real usage arrives in `message_delta`; the later publish replaces the earlier one. Measured by builder-1 at zero spend (Claude
    Code 2.1.301, three runs): Claude Code starts an MCP tool call 5 to 16 ms after a `tool_use`
    block's `content_block_stop`, 3.1 to 6.2 seconds before `message_delta`. So the proxy holds the
    stream from the first `tool_use` `content_block_start`: that event and all later events are
    buffered in order and released in order at the first of: `message_delta` (after its usage is
    observed and the room is published), an `error` event, or upstream end of stream, so an error
    or disconnect is never swallowed. Text, thinking, and `message_start` before the first
    `tool_use` still stream live. For the Responses dialect (Codex) the equivalent hold runs from
    the first `response.output_item.added` whose item is any tool-call type (anything other than a
    `message` or `reasoning` item; Codex 0.154.0 emits `custom_tool_call`, measured in the A6 smoke)
    until `response.completed`, `response.incomplete`, or `response.failed` (whose usage, when
    present, sets the room), an error event, or end of stream. A tool call
    therefore always sees the current turn's room. Keepalive pings may be sent during a hold.
  - `contexto_recall`, `contexto_search`, and `contexto_state` charge the tokens they return
    against the room for the current turn id, converted with the same ratio as the per-call limit,
    with an atomic, locked read-modify-write so parallel calls cannot overspend. The charge
    counter resets on every publish, because each published room already accounts for everything
    in the input it was measured from, including earlier recall results; charges after a publish
    therefore count only results not yet in measured input. This is safe within one conversation
    because of the stream hold: a tool call can only charge after its response's real-usage
    publish, and the next request-time estimate includes its results. If a hold is released at end
    of stream without usage, tool calls see the request-time room, which is off by at most one
    response's output. `count_tokens` never publishes a room. A single `contexto_recall` call
    returns at most the smaller of its per-call limit and half of the room that remains when it is
    made, with offset paging for the rest, so one recall cannot consume the turn; parallel recalls
    therefore get successively smaller halves. Measured in the second live H1 run
    (acceptance-20261016T044723Z): the room was 23,042, one full-page recall took 20,550 of it (89.2
    percent; the half cap would have allowed about 10,683), the next recall took the last 816, and
    the four calls after that found the room spent. When the room is above zero but half of it
    cannot hold about 200 page characters (below that a recall returns almost nothing useful;
    reviewer-1 measured 20 characters at a room of 200), `contexto_recall` replies: "The recall budget for
    this turn is nearly used up (about N tokens left), too little for a recall. Use contexto_search
    with an exact key to get the matching lines, answer from what is in view, or say the detail is
    unknown." `contexto_search` keeps working while its hits fit. The fixed short control replies (no hit, budget
    used up, budget nearly used up) and the coverage line on every search reply are protocol text
    and are not charged; each is at most about 60
    tokens (the nearly-used-up reply is about 225 characters), so even 15 parallel calls add about
    900 tokens, well inside `headroom_floor`. `contexto_recall`, `contexto_search`, and `contexto_state` never return more than
    remains, apart from these uncharged control replies; when it is
    used up they return: "Recall budget for this turn is used up; answer from what is in view, or
    say the detail is unknown."
  - Charges convert at `min(bytes_per_token, 1.5)` characters per token, using 1.0 before
    `set_window` has recorded a ratio. `contexto_recall`, `contexto_search`, and `contexto_state`
    return text content blocks only (no `structuredContent`), and the charge counts exactly what
    they return.
  - With no engine (no window recorded, for example tests or a direct `contexto mcp`), the per-turn
    budget does not apply and only the per-call limit holds. With an engine present but no room
    published for the call's turn, the same applies and the store logs `turn_room_missing`, so a
    missed publish shows up in acceptance logs.
  - Known limitation for v0.1.0: subagents launched by the CLI share the session's room and
    store. When subagents run concurrently with each other or with the main conversation, each
    one's publish replaces the shared room and resets its counter, so the per-turn recall budget is
    not enforced across overlapping conversations and they can overspend it; keying the room by
    conversation is the later fix.
  - Scope: this bounds what Contexto's own tools add. Results of other tools (file reads, shell
    output) are outside Contexto's control; a turn they push over the bound is logged
    `over_bound` as before.
- `contexto_pin(kind, text, supersedes?, epistemic?, provenance?)` appends a ledger record.
- `contexto_close(obligation_id, note)` appends a closure.
- `contexto_state()` returns the full pinned state without the budget cap.
- `contexto_search(query)`: exact substring search over page stubs and text. For each of the 3 newest matching pages
  (newest first, as in the index; for superseded facts the newest statement is the current one) it
  returns a short header (page id and turn number, no stub) and up to 3 matching lines, the last 3 in page order (a turn's latest statements, shown oldest to
  newest, so the last line shown is the newest), each with one line of context and its character
  offset in the rendered page; when a page has more matches than it shows, a note gives their
  count and the offset of the page's first match, so earlier statements stay reachable by recall; when a match is in the stub
  itself, the stub line is shown as the matching line. Any further matching pages are listed
  compactly as page id, turn, and the offsets of their first and last match (`@first..last`, or
  one offset when they coincide; about 10 tokens each), so every
  match stays reachable by `contexto_recall` at that offset even when the query cannot be
  narrowed. The model can answer from a hit or recall exactly that offset instead of reading a
  page from the top (measured in the second live H1 run: a full recall from offset 0 reached
  character 30,651 of a 39,405-character page within the turn's room, and the two facts beyond it
  were lost). Measured offline by builder-3 (numbers-only output under `spikes/m2/estimate/`):
  this format costs a median of about 138 tokens per hit instead of 359, and the 54 probes' total
  demand falls from 91,249 to 31,002 tokens. An answer more than one line away from its match is not shown in
  the hit and needs a recall at the offset. Every reply states its coverage: paged hits come only from paged-out turns, and the conversation
  in view is newer than any of them. Search also matches the retained window: after each request's transform (never from
  `count_tokens`) the engine calls `SessionStore.set_retained(epoch, k, turns)` with entries
  `{turn, message_index, role, text}` (text blocks of the retained messages, including
  `tool_result` text; `role` is `user`, `assistant`, or `tool_result`, the last for every tool
  result, including a Responses `function_call_output` or
  `custom_tool_call_output`; no thinking or images), a full atomic overwrite at mode 0600, bounded by the
  window, never logged, stamped with that request's sequence number. Both room publishes of the
  same request (request time and real usage) carry the same request sequence, so a tool call
  inside the stream hold sees a snapshot that matches its room. Search refuses a snapshot whose
  request sequence differs from the latest room publish's and says "in view not checked". A failed
  snapshot write only marks the snapshot stale (search then says "in view not checked") and logs
  `retained_stale`, not `store_error`; it does not enter degraded mode. The index, pinned text, and stubs never read the snapshot. Only the main
  conversation publishes the room and the snapshot: the engine keeps state per conversation (the
  main one, subagents, side requests), and the main conversation is the one with the most turn
  starts, re-evaluated at each turn start: it changes only when another conversation has strictly
  more turn starts (a tie keeps the current one), so a short side request that arrives first cannot
  stay main. Other conversations publish neither, so their tool calls see the
  main conversation's room and snapshot, and for a subagent "in view" refers to the main
  conversation's window (part of the documented subagent limitation); the MCP
  server cannot tell which conversation issued a call. Matches in the
  snapshot are listed first, labeled in view, newest first, as the matching line only, at most 3
  per reply (the newest 3 across the window, the rest counted in one line), charged,
  skipping the current turn's own question: entries with role `user` in the snapshot's newest
  turn are never matched, since that text is what is being asked, not a
  statement about it (B22; inferred from reviewer-2's zero-spend reach re-check on main 8571095: an offline
  search against a stub run's final snapshot returned probe q_a_045's own question, from an
  earlier turn, as an in-view line. In D27's plan, 64 of the 84 probe key terms appear in their
  own probe's question and none in another probe's, so during a probe turn the only question a
  search can match is the current turn's); the turn's `tool_result` text is still matched;
  with their returned spans logged content-free under a distinct source marker and no page id. In-view hits do not count toward acceptance 3,
  which measures retrieval of paged-out content. Measured basis (live run #2 chain
  analysis, builder-3, zero spend): 4 of H1's 5 extra chain misses had a newer link in the retained
  window, but the model answered from a search reply whose newest paged hit looked current.
  Results are charged against the per-turn room. No embeddings, no
  similarity ranking (owner: not RAG).

The preamble tells the model to pin decisions and obligations as it makes them and to recall
before relying on a paged-out detail.

## CLI

`contexto run claude [args]`, `contexto run codex [args]` (lane A step 2), `contexto proxy` (run
the proxy alone, for tests), `contexto mcp`, `contexto sessions list|show <id>`,
`contexto recall <page id>`, `contexto state`. Exit codes and `--help` documented in README.

## Lanes

- **Lane A, management.manager-1 (builder-1, builder-2):** launcher, proxy server and SSE relay,
  rewrite engine (rules 1 to 15), upstream resolution, session log; then the Codex adapter
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
   upstream request under the bound except logged over-bound turns, and zero 4xx responses to
   model requests (`/v1/messages`, `/responses`, and their `count_tokens`); 4xx answers to
   non-model requests, such as Claude Code's unauthenticated `HEAD /api/hello` startup probe (the
   owner's proxy answers it 401), are reported separately and do not fail the run. A 5xx that Contexto's own proxy generates on a model request (for
   example `upstream unreachable`) fails the run; an upstream 5xx passed through unchanged is
   reported. The run also fails on any event that
   means the product left its designed path: `degraded_enter`, `store_error`, `page_conflict`,
   `fallback_failed`, `transform_error`, `turn_room_missing`, `turn_room_low`, `retained_stale`,
   `epoch_record_stale`, or `unclassified_system_message`.
2. **Fidelity:** the L2f harder probes, in the chain-heavy mix (deep supersession chains are the
   only probe type that failed at small contexts in the two scored pilots, measured by builder-4
   in PR #22: deep chains 5/8 at about 12k, and 29/30 overall at 15,002 counted tokens with the
   one miss in a depth-8 chain; pilot 2 overran with no score), asked after that run's content
   turns over 6 user turns of 9 probes each (D27: fixed assignment, seed 20261016). They are
   scored against a full-context baseline asked the same 54 probes under the same 6 by 9
   assignment, as 6 sequential full-context requests each including the earlier probe turns.
   Pass: within 2 probes of that baseline overall and no category more than 1 probe below. The
   earlier single-turn scores (H1 18, baseline 44 of 54) stay on record.
3. **Recall:** at least one probe answerable only from a paged-out turn is answered correctly,
   and the session log shows that the answer reached the model through Contexto's tools: either a
   `contexto_recall` of the page holding it, or a `contexto_search` hit whose returned lines contain
   it. Either is retrieval of paged-out content; with search returning matching lines (B15),
   answering from the hit is the intended path.
4. **Cache:** within an epoch, requests read the head and window prefix from cache, except
   misses explained by the route (report the rate).
5. **Tests:** unit tests for every rewrite rule, the store, and the MCP server; CI green.
6. Intake runs the acceptance on integrated `main` and records the numbers in `STATUS.md`. Then
   intake cuts v0.1.0 (O5).

## Out of scope for M2

Embeddings or similarity search, model-written summaries of paged turns, multi-host sync, any UI,
publishing outside this repository (D0).
