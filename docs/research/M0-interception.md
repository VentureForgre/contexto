# M0 interception surfaces: Claude Code and Codex

Lane 1, packet L1-b. Author: build.builder-2. Date: 2026-10-15. Brief:
`docs/milestones/M0-infinite-session-study.md` (questions 1, 2, and 5).

Versions studied: Claude Code 2.1.301 (`~/.local/bin/claude`) and codex-cli 0.154.0
(`~/.local/bin/codex`, source tag `rust-v0.154.0`, commit `6b9826e3`).

Every claim carries one tag:

- **[measured: ...]** produced by a command in this repo, normally `spikes/m0/interception/run.sh <experiment>`
  (section 7). All measurements in this document cost zero model tokens (section 8).
- **[documented: ...]** vendor documentation (retrieved 2026-10-15, pages carry no version stamp)
  or vendor source at the pinned tag (`codex-rs/<path>:<line>`).
- **[measured by <seat or packet>: ...]** measured by another seat or packet (for example L1-a or
  lane 2's builder-4), cited with its record or its pending document; not re-run for this doc.
  **[observed by <seat>]** is the same for an observation that has no reproducing command.
- **[inferred]** reasoning from the above, not verified.

Phase 1 rules apply: spike code only observes, nothing here rewrites live traffic, and no content,
credential, or header value was logged. One deviation is disclosed in section 8: two early
non-isolated runs sent the owner's Claude subscription credential to the owner-operated rig proxy,
which rejected them.

## Summary

1. **Both CLIs let hooks add context but never remove or rewrite history.** Injected text reaches
   the upstream request (measured for both) and is then resent every turn. Hooks (H2) can steer what
   survives a compaction but cannot keep the prompt bounded.
2. **Both CLIs accept a synthesized or edited session file on resume, and send it to the model as
   written** (measured for both). Session-file rebirth (H3) works mechanically, between sessions
   only, on an internal format that changes between versions.
3. **Both compaction triggers run on token usage the CLI believes the API reported**, plus a local
   estimate for newer items. Planting a large usage figure in a session file makes either CLI
   compact before it sends the next turn (measured for both). A proxy that keeps the upstream prompt
   bounded will therefore report small usage and the CLI will not compact (inferred; the live check
   is phase 2).
4. **The hard constraint for the main hypothesis (H1) is preserved thinking.** On Claude 5.x models
   the API binds each thinking block to everything before it; shortening an earlier `tool_result`
   invalidates every later thinking block (documented). Lane 2's probe measured that editing content
   before a kept signed thinking block returns 400, while dropping old turns or stripping thinking
   returns 200 on its account. A rewriting proxy must be deterministic and append-only after each
   rewrite point, or use the server-side `context_management` edits Claude Code already sends a
   version of (documented, observed in the request params).
5. **A recall tool is far simpler as a tool the CLI declares (an MCP server) than as a tool only the
   proxy knows about** (section 5, inferred).
6. **Auth:** both CLIs send their subscription credential to a custom base URL. Anthropic and OpenAI
   terms both restrict intermediating subscription credentials. Owner decisions are listed in
   section 10.

## Owner principle and how H2, H3, H4 measure against it

Binding owner principle (2026-10-15): the compaction threshold exists because model quality
degrades as context grows, and results near it are poor. Compaction is a failure mode to avoid,
not a mechanism to optimize. The success bar is that compaction is never called, with recall and
task accuracy equal to the full-context baseline on the fidelity probes. The alternatives are
therefore judged first by how small they keep working context and how stable quality stays, and
only then by token cost. All [inferred] from sections 1 to 3; intake does the cross-lane ranking.

- **H2 (hooks):** cannot shrink working context. Every injection is appended and resent, so context
  grows until the CLI compacts. On its own it fails the principle; it only helps as an add-on that
  re-seeds a pinned state block after a rollover.
- **H3 (session-file rebirth):** can reset working context to a small curated seed at each rollover,
  and the rollover can be triggered far below the threshold (both CLIs trust recorded usage, so the
  writer controls what they believe). Between rollovers context grows normally. Quality stays in the
  low-context regime if the seed holds what later turns need; thinking and reasoning continuity is
  lost at each rollover because opaque items cannot be synthesized. Depends on internal formats.
- **H4 (supervisor rollover):** the same sawtooth as H3, using a fresh session seeded by a handoff
  message instead of a written file. No format dependency; the floor is the handoff size and quality
  depends on handoff fidelity.
- **Reference, H1 (bounded-prompt proxy):** the only option that can hold working context at a fixed
  budget every turn without rollovers; quality then depends on stubbing and recall fidelity, and
  preserved thinking constrains how it may rewrite (section 4).

## Capability matrix

Columns: what the surface lets an outside tool **read**, **rewrite**, or **block**, and what
**breaks** if it is used for context management. The last column gives the tag and source; section
numbers point to details.

### Claude Code 2.1.301

| Surface | Read | Rewrite | Block | Breaks if | Evidence |
|---|---|---|---|---|---|
| API requests (`ANTHROPIC_BASE_URL` proxy) | Full request: system, tools, messages, params, headers | Anything, in principle | Can refuse | Earlier bytes change under preserved thinking (400 or dropped thinking); prefix change re-writes the cache; `anthropic-beta` stripped (401 on OAuth) | documented (4.1, 6); measured that requests reach a local listener (7) |
| API responses and `usage` | Full SSE stream | Anything, including `usage` | n/a | Truncated SSE treated as dropped connection; rewritten `usage` changes the CLI's compaction decision | documented (3, 4) |
| SessionStart hook | `source` (startup, resume, clear, compact, fork), `transcript_path` | Append only (`additionalContext`) | No | 10,000-character cap on every hook output string; added text is itself summarized at the next compaction | measured `cc-hooks`, `cc-precompact`; documented (1) |
| UserPromptSubmit hook | `prompt` | Append only; cannot replace the prompt | Yes | 30 s default timeout drops its output | measured `cc-hooks`; documented (1) |
| PreToolUse / PostToolUse hooks | Tool input / tool output | `updatedInput`; `updatedToolOutput` (current call only, before Claude sees it) | Yes | Built-in tool output that fails the schema is ignored | documented (1) |
| PreCompact hook | `trigger`, `custom_instructions` | Plain stdout is appended to the summarizer instructions (undocumented) | Yes (exit 2) | Blocking a forced compaction surfaces "Prompt is too long" and the turn fails | measured `cc-precompact`; documented block (1) |
| PostCompact hook | `compact_summary` | No | No | n/a | documented (1) |
| Stop hook | `last_assistant_message` | No | Forces continuation (cap 8 in a row) | Loops if misused | documented (1) |
| Session file (`--resume`) | All records, including recorded API request blobs | Yes: synthesized or edited records become model input | n/a | Internal format, changes between versions; planted `usage` forces compaction | measured `cc-resume`, `cc-threshold` (2) |
| Compaction controls | n/a | `--autocompact`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (lower only), `DISABLE_AUTO_COMPACT`, `DISABLE_COMPACT` | Disable | With auto-compact disabled, a full context stops the session ("Prompt is too long") | documented (3); measured `cc-threshold` |

### Codex 0.154.0

| Surface | Read | Rewrite | Block | Breaks if | Evidence |
|---|---|---|---|---|---|
| API requests (`openai_base_url` proxy) | Full Responses API input every HTTP request (`store:false`, no `previous_response_id`) | Anything, in principle | Can refuse | Reasoning item separated from its following item (400); altered `encrypted_content`; WebSocket transport sends only deltas | measured `cx-hooks`; documented (4) |
| API responses and `usage` | Full SSE stream | Anything, including `usage` | n/a | Rewritten `usage` changes the compaction decision | documented (3) |
| SessionStart / UserPromptSubmit / SubagentStart hooks | `source`, `prompt` | Append only; context becomes a persisted `developer` message | UserPromptSubmit only | About 2,500 tokens default per injection; hooks need trust (or `--dangerously-bypass-hook-trust`) | measured `cx-hooks`, `cx-resume`; documented (1) |
| PreToolUse / PostToolUse hooks | Tool input / output | `updatedInput`; block replaces the result with feedback | Yes | n/a | documented (1) |
| PreCompact / PostCompact hooks | `trigger` | No (no context injection) | `continue:false` only | n/a | measured `cx-resume` (fires); documented (1) |
| Stop hook | `last_assistant_message` | No | Forces continuation | n/a | documented (1) |
| Rollout file (`codex resume`, `codex exec resume`) | All records | Yes: appended items become model input; a final `compacted.replacement_history` seeds history verbatim | n/a | Planted or stale `token_count` forces compaction; UI history sidecar tracks byte offsets | measured `cx-resume`; documented (2) |
| Compaction controls | n/a | `model_auto_compact_token_limit` (capped at 90% of the window), `model_context_window`, `compact_prompt` (local compaction only) | n/a | Remote compaction output is opaque `encrypted_content` | documented (3) |

## 1. Hooks (H2)

### Claude Code

What was measured [measured: `run.sh cc-hooks`, `run.sh cc-precompact`]:

- A fresh session fired SessionStart (`source=startup`), UserPromptSubmit, and SessionEnd. The
  `additionalContext` from both SessionStart and UserPromptSubmit appeared in the upstream request
  body next to the user's prompt.
- On `--resume`, SessionStart fired with `source=resume` and its context was in the request.
- Hook input field names: SessionStart `cwd, hook_event_name, session_id, source, transcript_path`;
  UserPromptSubmit adds `permission_mode, prompt, prompt_id`.
- The session file records injected context as `attachment` records of type
  `hook_additional_context`, plus `hook_success` records [measured: inspection of a throwaway
  session file, keys and marker presence only].
- **PreCompact steering works and is undocumented.** With a forced compaction, PreCompact fired with
  `trigger=auto`, and its plain stdout appeared in the compaction request sent upstream.
- **PreCompact exit 2 blocks.** When compaction was forced by the context limit, blocking it sent no
  request and the CLI stopped with "Prompt is too long".

What the documentation adds [documented: https://code.claude.com/docs/en/hooks]:

- 33 hook events. Only `additionalContext` (append), `updatedInput` (current tool call),
  `updatedToolOutput` (current tool result, must match the tool's output shape), and blocking can
  change what the model sees. UserPromptSubmit "can't replace the prompt".
- Every hook output string (`additionalContext`, `systemMessage`, `initialUserMessage`, and plain
  stdout) is capped at 10,000 characters with no override; overflow is replaced by a file path and
  a 2,000-character preview.
- Hooks get `transcript_path`, which "is written asynchronously and may lag". There is no API to
  read or rewrite the in-memory history.
- After a compaction, SessionStart hooks matching `compact` run and their output is added to the
  compacted context [documented: https://code.claude.com/docs/en/context-window#what-survives-compaction].
  This is the supported re-injection point.
- PreCompact stdout steering was found in the binary (the PreCompact runner joins hook stdout into
  `newCustomInstructions` for the summarizer) and is now confirmed by measurement. It is not
  documented and can change in any release.

### Codex

What was measured [measured: `run.sh cx-hooks`, `run.sh cx-resume`]:

- Hooks from a `hooks.json` in a temporary `CODEX_HOME` ran with `--dangerously-bypass-hook-trust`.
  A fresh session fired SessionStart (`source=startup`), UserPromptSubmit, SessionEnd; resume fired
  SessionStart with `source=resume`.
- Hook input field names: SessionStart `cwd, hook_event_name, model, permission_mode, session_id,
  source, transcript_path`; UserPromptSubmit adds `prompt, turn_id`.
- Both injected markers reached the upstream request as `developer`-role `message` items.
- With a forced pre-turn compaction, PreCompact fired (`trigger=auto`) and SessionStart and
  UserPromptSubmit did not fire before the compaction request.

What the source adds [documented: `codex-rs/hooks/src/events/compact.rs:47-54, 254-337`,
`core/src/hook_runtime.rs:830-850`, `hooks/src/output_spill.rs`, https://learn.chatgpt.com/docs/hooks]:

- Events: SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, PreCompact,
  PostCompact, Stop, SubagentStart, SubagentStop, Interrupt, SessionEnd. Hooks are stable and on by
  default, but a new or changed hook is skipped until trusted.
- Injected context is recorded into history, persisted to the rollout, and resent on every later
  request. Default cap about 2,500 tokens per injection (`additionalContextLimit`).
- PreCompact and PostCompact cannot inject context or steer the summary, only stop with
  `continue:false`. `compact_prompt` steers local compaction only; the built-in `openai` provider
  compacts remotely and returns opaque `encrypted_content`.

### H2 compared with the main hypothesis

- **Feasible:** re-inject a pinned state block (obligations, decisions, verified facts) at
  SessionStart for every source, and on Claude Code steer the compaction summary through PreCompact
  stdout or a CLAUDE.md "Compact Instructions" section.
- **Limits:** append only. Every injection becomes part of the history that grows and is resent,
  so H2 cannot bound the prompt; it only improves what survives the CLI's own compaction. Size caps
  (10,000 characters Claude, about 2,500 tokens Codex) bound the pinned block.
- **Breaks:** undocumented PreCompact steering can disappear in an update; Codex cannot steer
  remote compaction at all. Hook output that changes every turn breaks the prompt cache from that
  point on [inferred].
- **Role relative to H1:** H2 is a cheap, terms-safe complement (no credentials touched) and a
  fallback if the proxy is not allowed. It does not replace H1 [inferred].

## 2. Session files (H3)

### Claude Code

- Location: `<config dir>/projects/<cwd with non-alphanumerics replaced by '-'>/<session-id>.jsonl`.
  The cwd is the resolved path (on macOS `/tmp` becomes `/private/tmp`); a mismatched directory name
  gives "No conversation found with session ID" [measured: first `cc-resume` attempt]. The format
  "is internal to Claude Code and changes between versions" [documented:
  https://code.claude.com/docs/en/sessions].
- Record types observed in throwaway sessions [measured: key names only]: `user`, `assistant`,
  `attachment` (types include `hook_additional_context`, `hook_success`, `environment`, `model`,
  `skill_listing`, `instructions`, `session_context`, `date`, `prompt_snapshot`), `system`
  (`api_error`), `queue-operation`, `last-prompt`, `atis-latch`, `cost-state`, and three request
  records: `api-request` (`params`, `cacheControl`, `querySource`, `shapeHash`), `api-request-shape`
  (`system`, `tools`), and `api-request-blob` (hashed message content). Compaction writes a
  `system` record with subtype `compact_boundary` plus a `user` record with `isCompactSummary`
  [inferred from inspection of the 2.1.301 binary, not vendor documentation; the names are present:
  measured with `grep -a -o -F compact_boundary ~/.local/bin/claude | wc -l` (72 hits) and the same
  for `isCompactSummary` (94); no compaction was observed in these experiments].
- **Privacy note:** the session file stores what was sent on the wire, including the system prompt,
  tool definitions, and message blobs. Throwaway sessions from these experiments live in the
  isolated config dir under `.contexto/spikes/L1-b/cc-config/`; the two early non-isolated runs
  (section 8, deviations) and the live-recipe proof wrote to `~/.claude/projects`. They stay local and
  are never committed.
- **A synthesized file is accepted on resume** [measured: `run.sh cc-resume`]: a two-record file (one
  `user`, one `assistant`) written by `spikes/m0/interception/synth_session.py` was resumed with `--resume <id>`,
  and the upstream request contained both synthesized turns before the new prompt.
- **Planted usage drives compaction** [measured: `run.sh cc-threshold`]: the synthesized assistant
  record carries a `usage` object; with a large value the CLI compacts before sending (section 3).
- Resume and fork [documented: https://code.claude.com/docs/en/sessions]: `--resume` and
  `--continue` append to the same session id; `--fork-session` copies history into a new id;
  `--resume` also accepts an absolute `.jsonl` path. The recorded system prompt is reused across
  resume until compaction unless `--system-prompt-snapshot off` is passed.

### Codex

- Location: `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<local time>-<thread uuid>.jsonl`, compressed
  to `.jsonl.zst` after 7 days. Each line is `{timestamp, ordinal, type, payload}` with types
  `session_meta`, `response_item`, `compacted`, `turn_context`, `event_msg`, `world_state`, and
  others [documented: `codex-rs/rollout/src/recorder.rs:1635-1656`, `history/src/rollout_payload.rs`].
  Observed in a throwaway rollout: `session_meta`, `event_msg` (`task_started`, `item_completed`,
  `task_complete`), `response_item` messages, `world_state`, `turn_context` [measured: key names].
- **Appended items are accepted on resume** [measured: `run.sh cx-resume`, variant A]: an assistant
  `response_item` appended by `spikes/m0/interception/codex_append.py` appeared in the upstream input of
  `codex exec resume <thread id>`.
- **A planted `token_count` forces compaction** [measured: variant B]: with
  `total_tokens = 260000` the first upstream request ended with a `compaction_trigger` item and the
  new prompt was not sent ("Error running remote compact task").
- Load path [documented: `rollout/src/recorder.rs:1026-1105`, `core/src/session/rollout_reconstruction.rs:134-160, 345-425`]:
  invalid lines are skipped with a warning, there is no checksum, the newest `compacted` record with
  `replacement_history` seeds history verbatim, and later items replay in order.
  `normalize_history` then inserts `"aborted"` outputs for unanswered calls and drops orphan
  outputs. The UI sidecar `thread_history_1.sqlite` tracks rollout byte offsets, so editing earlier
  bytes in place likely desyncs the displayed history [inferred].

### H3 compared with the main hypothesis

- **Feasible:** write a fresh session file holding a curated state block plus a few recent turns
  and resume it. Codex offers a clean injection point (a final `compacted` record with
  `replacement_history`); Claude Code accepts plain `user`/`assistant` records.
- **Limits:** works only between sessions (the live process holds its own in-memory history), so
  it is a rollover mechanism like H4, not per-turn control. Formats are internal and changed
  recently on both sides. The `usage` or `token_count` recorded in the file must match the new
  content, or the CLI compacts immediately.
- **Breaks:** opaque items (`signature` on thinking blocks, `encrypted_content` on reasoning and
  compaction items) cannot be synthesized, so curated files should carry plain text only, which
  loses thinking continuity [inferred]. A format change in a CLI update breaks the writer.
- **Role relative to H1:** a cheap rollover path that needs no proxy and no credential handling; a
  good candidate for H4-style recovery with a better seed than a free-text handoff [inferred].

## 3. Compaction trigger (question 2)

### Claude Code

- **Mechanism** [inferred from inspection of the 2.1.301 binary; not vendor documentation and not
  reproduced by a committed command]: the count is the `usage` of the last assistant message
  (`input + cache_creation + cache_read + output`) plus a local bytes-per-token estimate of later
  messages. Levels: `compact` at the auto-compact threshold (`window - min(maxOutput, 20000) -
  13000`), `blocked` at `window - min(maxOutput, 20000) - 3000`. The measured part is narrower: the
  CLI trusts the `usage` recorded in the session file (planted values drive the decision, below), and
  the override names exist in the binary [measured: `grep -a -o -F <name> ~/.local/bin/claude | wc -l`
  gives `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 8, `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 16,
  `DISABLE_AUTO_COMPACT` 6, `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE` 3].
- **Thresholds** [documented: https://code.claude.com/docs/en/model-config#set-the-auto-compact-window]:
  native 1M models compact "at about 967K tokens"; 200K models compact at the 200K boundary.
  Unknown model ids behind a gateway are assumed to have 200K.
- **Overrides** [documented: same page and https://code.claude.com/docs/en/env-vars]:
  `--autocompact <auto|tokens>` sets the auto-compact window for one launch (help text: "Auto-compact
  window size (auto, or 100k-1M tokens)" [measured: `claude --help`]); `/autocompact <n>` and the
  `autoCompactWindow` setting persist it; `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100000 to 1000000)
  outranks the others; `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` can only lower the threshold;
  `DISABLE_AUTO_COMPACT=1` disables auto and idle compaction; `DISABLE_COMPACT=1` disables all.
  Idle compaction applies above `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` (default 200000).
- **Measured on resume in `-p` mode** [measured: `run.sh cc-threshold`, `claude-opus-5-5`]:

  | Planted usage | Setting | First upstream request |
  |---|---|---|
  | 150,000 to 970,012 | default | the new turn |
  | 975,012 and above | default | a compaction call (new prompt held back) |
  | 101,012 | `--autocompact 100000` | the new turn |
  | 199,012 | `--autocompact 200000` | the new turn |
  | 120,012 | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=10` | the new turn |
  | 999,012 | `DISABLE_AUTO_COMPACT=1` | none; CLI reports "Prompt is too long" |

  The pre-send trigger sits between 970,012 and 975,012 reported tokens plus the local estimate of
  the new prompt [measured]. Reading it as the `blocked` level (977,000) rather than the 967K
  auto-compact threshold [inferred] assumes the local estimate for the new prompt and its
  attachments is about 2,000 to 7,000 tokens, which was not measured. In `-p` resume, neither the
  window flag nor the percentage override caused a compaction before the first send [measured].
- **Measured mid-turn in `-p` stream-json mode** [measured by L1-a, builder-1: Claude Code 2.1.301,
  `claude-haiku-5-5`, default mode, `-p --output-format stream-json`, `--autocompact 100000`, through
  the pass-through proxy; to be published in `docs/research/M0-proxy-measurements.md`, doc pending].
  This run violated owner decision D3 (default compaction settings only, `docs/DECISIONS.md`,
  PR #12) because it set `--autocompact`; its findings are kept as evidence, not as a pattern to
  repeat. `--autocompact` does fire in this mode, mid-turn, and can loop. Turn 1 read four logs of about
  50 KB in parallel; request 7 had a context of 136,866 tokens (one `tool_result` of 153 KB).
  Compaction 1 ran at 139,320 (cache write 139,318, cache read 0, a full cache miss; output 5,510).
  The model then re-read files; compactions 2 and 3 ran at 78,469 (output 8,908) and 86,968
  (output 9,342), both below 100K, which suggests the trigger counts usage plus pending tool
  results [inferred by L1-a]. The final request had a context of 38,887. stream-json emitted three
  `system` `compact_boundary` events, and `count_tokens` calls of about 50 KB appeared around the
  compactions. Compaction requests were identified by shape (the same 5 messages, no
  `claude-code-20250219` beta, `end_turn`).
- **The two modes together:** a resumed `-p` session checked only the hard `blocked` level before its
  first send (this doc), while a live `-p` stream-json turn applies the `--autocompact` window
  mid-turn and can compact repeatedly within one turn (L1-a). Both are measured; the difference
  between them is not yet explained [inferred: the auto-compact check may run only after a model
  response in the query loop].
- **Under a bounded-prompt proxy** [inferred]: Claude Code's count comes from the `usage` the
  proxy returns. If the proxy forwards a bounded prompt and returns the true upstream usage, the
  CLI sees small numbers and does not compact, while its local history keeps growing. Rewriting
  `usage` would let a proxy steer compaction directly. Gateway hint headers
  (`x-claude-code-request-class: compaction`, opt-in via `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`) let a
  proxy recognize compaction traffic [documented:
  https://code.claude.com/docs/en/llm-gateway-protocol#gateway-hint-headers].
- **Already server-side:** every observed request carried `context_management` with
  `clear_thinking_20251015` (`keep: "all"`) and the beta `context-management-2025-06-27`, plus 1-hour
  `cache_control` on two system blocks and one message block [measured: `api-request` params in a
  throwaway session file].

### Codex

- **Mechanism** [documented: `codex-rs/protocol/src/openai_models.rs:504-526`,
  `core/src/context_manager/history.rs:662-693`, `codex-api/src/sse/responses.rs:128-140`]: the
  count is `total_tokens` from the server `usage` on `response.completed`, plus a 4-bytes-per-token
  estimate of items added after the last model output. It is restored on resume from the last
  `token_count` event.
- **Threshold:** `min(model_auto_compact_token_limit, 90% of context_window)`, hard cap at 95%. For a
  272,000-token window: about 244,800 [documented arithmetic]. Measured: a planted 260,000 compacts
  before the turn, a planted 1,000 does not [measured: `run.sh cx-resume`].
- **Equivalent of `--autocompact`:** no dedicated flag; `-c model_auto_compact_token_limit=<n>` sets
  it per launch (it can only lower the trigger below 90% of the window), and
  `-c model_context_window=<n>` changes the window (clamped to the model's maximum) [documented:
  `codex-rs/models-manager/src/model_info.rs:25-37`, `config/src/config_toml.rs:164-172`; not
  measured].
- **When it fires:** before a turn, mid-turn when a follow-up is needed, on switching to a smaller
  model, and on `/compact` [documented: `core/src/session/turn.rs:463-548, 1087-1115, 1195-1225`].
- **Local or remote:** the built-in `openai` provider compacts remotely (a `/responses` call ending in
  `compaction_trigger`, measured) and gets back opaque `encrypted_content`; other providers summarize
  locally with `compact_prompt`.
- **Under a bounded-prompt proxy** [inferred]: same as Claude Code. A proxy that shrinks input
  lowers reported usage, so Codex compacts later or never; the catalog endpoint
  (`<base_url>/models`) also lets a proxy report a different `context_window`.

## 4. Protocol validity rules a rewriting proxy must preserve

### Anthropic Messages API (Claude Code)

All [documented, 2026-10-15] unless marked.

- **Tool pairing:** a `tool_result` must be in the next user message, matched by `tool_use_id`, and
  come before any text in that message; a missing result fails with "tool_use ids were found without
  tool_result blocks immediately after"
  (https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls). A string stub as
  `tool_result` content is schema-valid. Removing a whole call and result pair together keeps the
  pairing valid [inferred].
- **Thinking signatures:** thinking and `redacted_thinking` blocks in the latest assistant turn must
  be passed back "complete and unmodified" in order
  (https://platform.claude.com/docs/en/build-with-claude/thinking).
- **Preserved thinking, the critical rule:** on Fable 5.1, Opus 5.5, Sonnet 5.5, and Haiku 5.5 the
  API checks that "Nothing before the thinking block has changed": the `system`, `tools`, and earlier
  `messages` form its prefix. "Clear or shorten an earlier `tool_result`" and "Edit, reorder, or
  delete any earlier ... message" are invalid for every later thinking block. Default enforcement
  (400, "bound to a different conversation") applies to accounts created on or after 2026-08-31;
  `drop_block` (beta `thinking-binding-controls-2026-08-01`) is a stopgap that raised output tokens
  67.1% at max effort. Valid: append at the end, or remove thinking blocks from the start, the end,
  or all of them (https://platform.claude.com/docs/en/build-with-claude/preserved-thinking). Claude
  Code's gateway guidance says the same: "inspect without modifying"
  (https://code.claude.com/docs/en/llm-gateway-protocol).
- **Measured on one account** [measured by lane 2, builder-4: conformance probe at 23:26Z,
  4,973 tokens, direct API, `claude-haiku-5-5` with two signed thinking turns; record in builder-4's
  `.contexto/spikes/l2b/conformance/probe-20261015T232600Z.json`, doc lands with PR #7's revision]:
  append-only 200; strip all thinking 200; drop the oldest turns while keeping thinking 200 (with
  `prefix_mismatch_behavior=error` also 200, with the thinking dropped and reason
  `end_user_binding_mismatch`, not 400; the vendor page names the reason `prefix_binding_mismatch`
  [documented: https://platform.claude.com/docs/en/build-with-claude/preserved-thinking], so the
  measured reason string differs from the documented one); change `system` while keeping thinking 400 (invalid
  signature); add text to an earlier user turn 400; full epoch rewrite with thinking stripped 200;
  `drop_block` 200; `clear_thinking_20251015` 200. Reading [inferred by lane 2]: the probe's account
  does not enforce the binding by default for dropped turns, but edits before signed thinking still
  fail with 400. A proxy can therefore drop old turns or rewrite whole epochs with thinking stripped,
  but must not edit content that precedes a signed thinking block it keeps.
- **Server-side edits are exempt:** context editing (`clear_tool_uses_20250919`,
  `clear_thinking_20251015`, beta `context-management-2025-06-27`) and server compaction "never
  trigger" the prefix check, because it compares what the client sent
  (https://platform.claude.com/docs/en/build-with-claude/context-editing).
- **Prompt caching:** at most 4 breakpoints; 20-block lookback; 5-minute (default) or 1-hour TTL;
  any change invalidates that level and everything after it in the order tools, system, messages;
  writes cost 1.25x (5m) or 2x (1h), reads 0.1x (0.05x on Opus 5.5 and Sonnet 5.5); minimum 512
  tokens on 5.x models (https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
- **Compaction APIs:** threshold compaction (`compact-2026-01-12`, edit `compact_20260112`) and
  on-demand compaction (`compact-2026-09-04`) return a readable summary block with a signature that
  must go first in `messages`.
- **Usage:** total input = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`;
  in streaming, `message_delta` usage is cumulative, so read the final event.

### OpenAI Responses API (Codex)

- **Codex's wire behavior, HTTP mode** [measured: `run.sh cx-hooks`, which prints `store` and
  `previous_response_id` per request]: when the stub refused the WebSocket upgrades (4 to 7
  `GET /v1/responses` attempts with 401), Codex sent HTTP `POST /v1/responses` with the full input,
  `store:false`, no `previous_response_id`, and `include: ["reasoning.encrypted_content"]`, and
  retried the identical POST up to 6 times on 401. Proxy logs will show those retries.
- **Codex's wire behavior, WebSocket mode** [measured by L1-a, builder-1: one throwaway Codex probe
  through a WebSocket tunnel in the pass-through proxy; to be published in
  `docs/research/M0-proxy-measurements.md`, doc pending]: Codex uses a WebSocket with
  permessage-deflate, and its second request carried `previous_response_id` with only 5 items
  (33 KB) instead of the full history. So on WebSocket the wire carries deltas against server-side
  state, while the billed input is still the full conversation (M0-codex-measurements.md).
  [documented: `codex-rs/core/src/client.rs:300-370, 1331-1410`] A rewriting proxy must handle or
  refuse the WebSocket path. The session-wide fall back to HTTP is sourced in `client.rs` only for
  HTTP 426 [documented: `core/src/client.rs:556-562`]; that a 401 refusal also led to HTTP POSTs is
  measured above.
- **Call pairing:** `function_call` and `function_call_output` pair by `call_id`. Codex itself
  inserts `"aborted"` outputs and drops orphans before each request [documented:
  `core/src/context_manager/normalize.rs:21-155`].
- **Reasoning items:** "ensure all items between the last user message and your function call
  output are passed into the next response untouched"
  (https://developers.openai.com/api/docs/guides/reasoning). The server rejects a reasoning item
  without its following item (`Item 'rs_...' of type 'reasoning' was provided without its required
  following item.`) [documented: Codex test fixture `core/src/guardian/tests.rs:2971-2980`].
  `encrypted_content` is opaque; editing it is expected to fail [inferred].
- **Does Codex have a preserved-thinking equivalent?** Documented: the adjacency rule above (a
  reasoning item must be followed by its paired item, and items since the last user message must be
  passed back untouched, https://developers.openai.com/api/docs/guides/reasoning). Inferred, not
  documented: whether the server binds `encrypted_content` to the context before it, so that
  altering or reordering it, or editing earlier items, invalidates it the way Anthropic's prefix
  check does. Measured by lane 2 (builder-4's conformance probe above: Responses, `store:false`,
  encrypted reasoning; the model, `gpt-6-astra`, is as stated in builder-4's report, since the
  probe record has no model field): append-only (O1), editing an earlier user turn (O2), and
  dropping it (O3) all returned 200. No rejection was observed; whether the model actually used the
  encrypted reasoning after the edit is not observable from the response. Altering or reordering
  `encrypted_content` itself was not tested.
- **Compaction:** server-side `context_management: [{type: "compaction"}]` or
  `POST /responses/compact`; the compaction item is opaque `encrypted_content`, so a proxy cannot
  audit what was kept (https://developers.openai.com/api/docs/guides/compaction).
- **Caching and usage:** `cached_tokens` is a subset of `input_tokens` [inferred from the
  cache-hit-rate formula]; minimum 1,024 tokens on GPT-5.6 and later. Rates: on GPT-5.6 and later,
  writes 1.25x and reads 0.1x (0.05x on GPT-6.1 Sol); GPT-5.5 and earlier have no write charge
  (https://developers.openai.com/api/docs/guides/prompt-caching).

### Rules for any rewriting proxy (inferred from the above)

1. Never touch the in-flight span: the latest assistant turn and its tool results (Anthropic), or
   everything after the last user message (OpenAI).
2. Keep pairs whole: stub inside a result, or drop call and result together.
3. Make rewrites deterministic and monotonic: once a turn is stubbed it is always stubbed the same
   way, so the rewritten prefix is byte-stable for caching and for preserved thinking.
4. On Claude 5.x, each new stub point invalidates later thinking blocks. Strip them from that point
   and keep them stripped, or prefer injecting server-side `context_management` edits.
5. Batch rewrites at checkpoint epochs: each rewrite turns everything after it into a cache write.
6. Pass opaque fields through byte for byte (`signature`, `redacted_thinking.data`,
   `encrypted_content`, compaction items, `phase`, unknown block types), and forward `anthropic-beta`
   and `cache_control` unchanged.

## 5. A recall tool the CLI never declared

The main hypothesis lets the model fetch a paged-out turn by id. If the proxy adds that tool itself
(the CLI does not know it exists), the proxy must answer the call without the CLI seeing it. All
[inferred] from sections 3 and 4:

1. **Declare it upstream.** Add the tool to `tools` (Anthropic) or the tool list (Responses). This
   changes the cache prefix once; keep the definition byte-stable afterwards. Anthropic's
   `inline-tools-2026-09-15` beta (already in Claude Code's beta list [measured]) is designed for
   adding tools mid-conversation without editing `tools`.
2. **Hold the stream.** The proxy cannot forward SSE events until it knows whether the response
   contains a recall call, because the CLI must never receive a `tool_use` it cannot execute. That
   means buffering, or forwarding text and thinking deltas while holding tool blocks.
3. **Answer and continue upstream.** On a recall call: append the assistant turn (with its
   thinking blocks or reasoning items) and a `tool_result` / `function_call_output` holding the
   page, send another upstream request, and repeat until the model stops calling recall. Each round
   trip costs a full input (mostly cache reads) the CLI never accounts for.
4. **Stitch one response.** The CLI expects one message per request: the proxy must merge several
   upstream messages into one downstream stream (one `message_start` and `message_stop`, renumbered
   content block indexes, combined `usage`, a final `stop_reason` that matches the remaining
   blocks).
5. **Keep a shadow history.** Thinking blocks produced after a hidden recall exchange are bound to a
   prefix that includes it. On every later request the proxy must reinsert the hidden exchange at the
   same position, byte for byte, or those blocks fail the preserved-thinking check. The proxy thus
   owns a second, authoritative history aligned to the CLI's.
6. **Mixed calls.** If one response calls recall and a real CLI tool, the proxy must either resolve
   recall first and let the model re-plan, or split the turn; both make the stitched message differ
   from anything the model produced, which preserved thinking forbids.
7. **Usage reporting.** The `usage` the proxy returns drives the CLI's compaction (section 3); it must
   be chosen deliberately (for example, the final round's bounded input).

**Simpler alternative:** expose recall as an MCP tool that the CLI declares (both CLIs support MCP
servers [documented]). The CLI then executes it, records it, and resends it like any tool call, and
the proxy needs no stream surgery or shadow history. Cost: the CLI's local history grows with
recalled pages, which the proxy's stubbing then has to handle like any other tool result.

**Cross-lane note:** item 5 above and the preserved-thinking rule in section 4 conflict with the
policy proposed in lane 2's PR #7 (reviewer-2 finding B3 there). The two documents must agree before
intake merges the recommendation; this document does not resolve it.

## 6. Auth and terms (question 5)

### Claude Code

- **Which credential goes where** [documented: https://code.claude.com/docs/en/authentication]:
  a signed-in gateway session outranks everything; then cloud provider, `ANTHROPIC_AUTH_TOKEN`
  (sent as `Authorization: Bearer`), `ANTHROPIC_API_KEY` (sent as `X-Api-Key`), `apiKeyHelper`,
  `CLAUDE_CODE_OAUTH_TOKEN`, Anthropic profile and federation credentials, then the `/login`
  subscription.
- **Subscription login honors a custom base URL** [documented:
  https://code.claude.com/docs/en/llm-gateway#subscriptions-and-gateways]: "Requests still route
  through the gateway, but a saved claude.ai login remains the active credential", and the gateway
  must forward the OAuth capability in `anthropic-beta`. Measured: a throwaway session started
  outside the seat worktree fell back to the subscription login, sent the `oauth-2025-04-20` beta, and
  the rig proxy answered 401 (Claude Code then retries up to 10 times) [measured: `api-request`
  params and `api_error` records in that session file].
- **Header names observed** [measured, names only]: with a dummy `ANTHROPIC_API_KEY`: `x-api-key`,
  `anthropic-beta`, `anthropic-version`, `x-app`, `x-claude-code-session-id`, `x-stainless-*`. With the
  seat's `apiKeyHelper` (recipe proof): both `authorization` and `x-api-key`. Subscription-mode
  headers were not captured.
- **Terms** [documented: https://code.claude.com/docs/en/legal-and-compliance]: OAuth is "designed to
  support ordinary use of Claude Code"; developers may not "route requests through Free, Pro, or Max
  plan credentials on behalf of their users" or "collect, store, or intermediate Claude.ai
  credentials or session tokens"; an end user may sign in "to the unmodified Claude Code binary with
  their own Claude subscription". Consumer Terms (effective 2025-10-08) forbid automated access
  except with an API key or where Anthropic otherwise explicitly permits it (https://www.anthropic.com/legal/consumer-terms).

### Codex

- **Base URL** [documented: `codex-rs/model-provider-info/src/lib.rs:293-312`,
  `config/src/config_toml.rs:400-401`]: `openai_base_url` (config or `-c`) points the built-in
  provider elsewhere in every auth mode; `OPENAI_BASE_URL` is no longer read by the model provider.
  ChatGPT-login mode only defaults to `https://chatgpt.com/backend-api/codex`, so the ChatGPT token is
  sent to whatever URL is configured [inferred from code, not measured].
- **Credentials sent** [documented: `codex-rs/model-provider/src/auth.rs:197-326`]: API-key mode
  `Authorization: Bearer <key>`; ChatGPT mode `Authorization: Bearer <access token>` plus
  `ChatGPT-Account-ID`. Measured with a dummy key: `authorization` on both the WebSocket upgrade and
  the HTTP POST [measured: `run.sh cx-hooks`, names only].
- **Terms** [documented: https://openai.com/policies/terms-of-use/, effective 2026-01-01]: "You may
  not share your account credentials", and users may not "Automatically or programmatically extract
  data or Output" or "circumvent any rate limits". Codex docs: "Use API key authentication for
  programmatic Codex CLI workflows" (https://learn.chatgpt.com/docs/auth).

### Reading (inferred, not legal advice)

A local proxy that the owner points their own unmodified CLI at is a configuration both vendors
document. A product that intermediates subscription tokens, or heavy automated rewriting of
subscription traffic, is where the terms bite. API-key auth through the proxy is the lowest-risk
path for both CLIs. The decision belongs to the owner (section 10).

## 7. Experiments and reproduction

### Zero-spend method

Every measurement above comes from `spikes/m0/interception/run.sh`. It runs a throwaway CLI against
`spikes/m0/interception/stub401.py`, a local listener on `127.0.0.1` that answers every request with 401 and
logs method, path, header names, body size, message or input-item structure, and synthetic
`MARKER-*` test tokens, nothing else. The CLI builds, records, and sends its full request before
seeing the 401, so injection, resume, and compaction decisions are visible without a model call.

Isolation per run: `env -i` with only `HOME`, `PATH`, `TERM`; an isolated `CLAUDE_CONFIG_DIR` or
`CODEX_HOME` under `.contexto/spikes/L1-b/`; a dummy key that is not a credential; `cwd` under
`/private/tmp/m0-l1b-scratch/`; `CLAUDE_CODE_MAX_RETRIES=0` for Claude; a 60 s alarm.

```sh
spikes/m0/interception/run.sh all            # or one of: cc-hooks cc-resume cc-threshold cc-precompact cx-hooks cx-resume
```

Output and logs go to `.contexto/spikes/L1-b/` (gitignored). A full `all` run took about four
minutes on 2026-10-15 and reproduced every result in sections 1 to 3.

### Live-call recipe proof (for lane 1 live runs)

Manager-1's recipe for a throwaway process that uses the seat's inherited credential, proven once
with zero tokens on 2026-10-15 22:59:20Z [measured]:

```sh
cd <worktree>/.contexto/spikes/L1-b/recipe-proof/cwd
env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_CHILD_SESSION \
    -u CLAUDE_CODE_MESSAGING_SOCKET -u CLAUDE_CODE_MESSAGING_TOKEN -u CLAUDE_PID \
    -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SESSION_ATTENDED -u CLAUDE_CODE_EXECPATH \
    $(env | grep -oE '^(OPENRIG|RIGGED)_[A-Z_]+' | sed 's/^/-u /') \
    CLAUDE_CODE_MAX_RETRIES=0 \
  claude -p --settings '{"disableAllHooks": true, "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:<port>"}}' \
    --strict-mcp-config --output-format json '<prompt>' < /dev/null
```

- **Why strip the environment:** the seat's `activity-relay` hook identifies the seat from
  `OPENRIG_*` variables, and on SessionStart it posts a `session_identity` event that could rebind
  the seat's resume token to the throwaway session. Stripping those variables means a hook that did
  fire could not report as the seat. The `CLAUDE_CODE_*` session variables are stripped so the child
  does not attach to the parent session. `disableAllHooks` is the primary control.
- **Checks recorded:**
  1. The listener (the 401 stub on port 18431) logged `HEAD /api/hello` and three
     `POST /v1/messages` (130,385 bytes each), with header names `authorization`, `x-api-key`, and
     `anthropic-beta`: the seat's `apiKeyHelper` credential was inherited from the worktree
     settings. Values were not logged.
  2. The throwaway session file had zero hook attachments, so `disableAllHooks` suppressed the seat
     hooks by itself.
  3. `rig ps --nodes -A --full --json` before and after: the seat's `resumeToken` and its last hook
     event (`UserPromptSubmit` at 22:58:01Z, the seat's own prompt) were unchanged; only sampling
     timestamps and `lastActivityAt` (pane activity from the seat's own work) moved.
  4. `rig queue whoami --json`: pending 0, in progress 1, blocked 1, same rows before and after.
  5. `rig capture` of the seat showed no throwaway output.
- **Side notes:** the CLI printed "claude.ai connectors are disabled because ANTHROPIC_API_KEY or
  another auth source is set and takes precedence over your claude.ai login". The inherited
  worktree `CLAUDE.md` and rig onboarding inflate the throwaway's system prompt (the request body was
  130,385 bytes against about 81,900 for the isolated-config runs); live measurements should report
  that size separately. Throwaway transcripts land in `~/.claude/projects` and stay local.
- For a live run, the env override points at the pass-through proxy, whose upstream is read at
  runtime from the value the process would otherwise inherit and is never written to files or logs.

### OpenRig operational notes for lane experiments

- **Routing miss on the lab seat: the measured cause** [measured by L1-a, builder-1; doc
  `docs/research/M0-proxy-measurements.md` pending]. After the lab seat was recreated (23:09Z), its
  model calls went to the owner-operated rig proxy instead of the spike proxy. Three throwaway runs
  (Claude Code 2.1.301, environment stripped, `--settings '{"disableAllHooks": true}'` only,
  `--strict-mcp-config`, `--no-session-persistence`, `claude -p hi < /dev/null`, a 401 stub on
  `127.0.0.1:18451`) isolate why:
  - T1: cwd in the lab seat's worktree, whose own `.claude/settings.local.json` names port 18451.
    The stub got 0 requests and a real model answered elsewhere.
  - T2: cwd in a non-git `mktemp` directory whose `.claude/settings.local.json` holds only
    `env.ANTHROPIC_BASE_URL` pointing at port 18451. The stub got 3 `POST /v1/messages`; 0 model
    tokens.
  - T3: T1 plus `--debug`. The settings watch list included `~/.claude/settings.json`, the
    worktree's `.claude/settings.json` (missing) and `.claude/settings.local.json`, and also the
    main checkout's `.claude/settings.local.json`. Requests were logged as "dispatching to
    firstParty", and the stub got 0 requests.

  Conclusion: in a git worktree, Claude Code also loads the main checkout's
  `.claude/settings.local.json` [measured by L1-a], and that file's `env.ANTHROPIC_BASE_URL` wins
  over the worktree's own file [inferred from the outcome; the precedence order itself was not
  printed]. The live-call recipe above was not affected because its base URL comes from the
  flag-level `--settings`, which outranks both files.
- **Launch before projection** [observed by manager-1, 2026-10-15]: on a rig shrink and expand of
  the lab pod, the seat's `claude` process started at 18:22:35 local time, and OpenRig's projection
  rewrote that seat's `.claude/settings.local.json` at 18:22:37 and again later. This is an
  observation only; it is not the cause of the routing miss above (a second recreate with the file
  already correct also missed the spike proxy). Rule: after recreating a seat, verify its live
  routing with one tiny turn before measuring.
- **Design consequence for Contexto** [inferred from the measured cause]: Contexto must attach at a
  layer that wins over every settings file, for example an explicit `--settings` flag or a launcher
  wrapper that sets it, and must not rely on a worktree's own `.claude/settings.local.json`.
- **Cost lesson** [observed in this packet and L1-a]: a throwaway run without `--model` uses the
  user's default model (the early non-isolated runs in section 8 used `claude-fable-5-1` that way).
  Pin `--model` on every throwaway run.
- **Slash commands through `rig send`** [measured by manager-1]: a slash command sent with
  `rig send` arrives at the seat as a task, not as a CLI command.

## 8. Token spend

| Experiment | Model tokens (input, cache read, cache write, output) |
|---|---|
| All `run.sh` experiments, the recipe proof, and two early hook runs | 0 (every request answered by the local stub or rejected by the rig proxy with 401) |

### Deviation: two early non-isolated runs

Before the isolation in `run.sh` and the agreed recipe existed, two throwaway Claude Code runs were
started from `/private/tmp` with none of the required isolation: no isolated config dir, no dummy
key, and an unscrubbed environment [measured: request and error records in their local session
files, names and counts only].

| Run (UTC) | Model | Requests recorded, including retries | Outcome |
|---|---|---|---|
| 22:49:15 to 22:52:21 | `claude-haiku-5-5` | 11 | 401 on every request; process stopped by hand |
| 22:52:56 to 22:55:22 | `claude-fable-5-1` | 10 | 401 on every request; stopped by a 150 s alarm |

- **Credential type:** both runs ran outside the seat worktree, so they did not load the seat's
  `apiKeyHelper` and fell back to the owner's claude.ai subscription OAuth login (`anthropic-beta`
  included `oauth-2025-04-20`). With the inherited `ANTHROPIC_BASE_URL`, that credential went to the
  rig proxy, which answered 401 each time.
- **Not checked:** whether the rig proxy logs authorization headers.
- **Exposure:** the owner confirmed that the rig proxy endpoint is owner-operated infrastructure, so
  the credential did not reach a third party and nothing needs rotating (decision relayed by intake
  through manager-1).
- **What changed afterward:** every later experiment used `run.sh` isolation (isolated config dir,
  dummy key, `env -i`, local stub), and the one live-credential run used the recipe above.

Research for this packet used three in-session research subagents (Anthropic docs and the Claude
Code binary; Codex source and docs; API protocol docs). Their own consumption, as reported by the
harness: 206,547 + 232,154 + 228,620 = 667,321 tokens. No experiment called a model.

## 9. Related measurements

- **Proxy measurements (placeholder):** builder-1's pass-through proxy measurements are delivered
  separately in `docs/research/M0-proxy-measurements.md`: per-turn request sizes, reported usage,
  cache behavior, the mid-turn `--autocompact` run (section 3), and the Codex WebSocket probe
  (section 4). Not duplicated here.
- **Codex session growth:** `docs/research/M0-codex-measurements.md` (PR #10)
  measures six turns on the `labcodex.subject` seat from its rollout `token_count` events.

## 10. Owner decisions

1. **Credential mode for any rewriting proxy (Q5).** Route API-key traffic only, or also the owner's
   Claude subscription and ChatGPT login. Both vendors' terms restrict intermediating subscription
   credentials (section 6). This gates phase 2.
2. **Live calls on the owner's subscription for experiments.** Decided by the owner for Codex: the
   `labcodex.subject` seat runs on the owner's ChatGPT login (decision record on queue row
   `qitem-20261015224601-10ad09bd`). Claude live runs in lane 1 use the seat's proxy credential
   through the proven recipe, or zero-spend stubs.
3. **Dependence on undocumented behavior.** PreCompact stdout steering in Claude Code works today
   (measured) but is undocumented. Building on it means re-verifying on every CLI update.
4. **Session-file writing.** H3 writes into internal, version-specific formats in `~/.claude` and
   `~/.codex`. Decide whether a product may do that or should stay with hooks and the proxy.
5. **Recall through MCP or a hidden proxy tool** (section 5). MCP is far simpler; a hidden tool
   keeps the CLI's history smaller but needs a shadow history and stream stitching.
6. **Rewriting the `usage` a proxy reports to the CLI** (section 3). It would steer the CLI's
   compaction decision, but it changes what the owner's tool reports about real consumption.
7. **Preserved-thinking status of the owner's Anthropic account** (section 4). A fact to check
   rather than a choice: accounts created on or after 2026-08-31 get the prefix check by default.
   Lane 2's probe account dropped turns without error but rejected edits before signed thinking;
   the owner should confirm the account phase 2 would use behaves the same.
8. **Attachment layer** (section 7). [intake decision, not a finding] Resolved by intake under D0,
   see `docs/DECISIONS.md` D16 (pending on main): a launcher wrapper is acceptable and the preferred
   phase 2 attachment layer (for example `contexto run claude ...` passing an explicit `--settings`
   with the proxy base URL; `contexto run codex ...` using a temporary `CODEX_HOME` whose provider
   uses `auth.command`), because it beats every settings file and never edits the user config.

## 11. Open questions

- Subscription-mode request headers for Claude Code (names only) were not captured.
- Exact OpenAI errors for an orphaned `function_call` or altered `encrypted_content`, and whether
  edited history degrades use of encrypted reasoning; Anthropic behavior for an orphaned
  `tool_result`.
- Whether the account phase 2 would use matches lane 2's probe account on preserved-thinking
  behavior (section 4).
- Why a resumed `-p` session ignores `--autocompact` before its first send while a live `-p`
  stream-json turn applies it mid-turn (section 3); interactive mode was not measured.
- Codex `-c model_auto_compact_token_limit` per launch was read from source, not measured.
- Whether a Codex custom provider named `openai` is treated as OpenAI for remote compaction (read
  from source, not tested).
