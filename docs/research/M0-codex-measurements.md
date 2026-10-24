# M0 Codex measurements: session growth on labcodex.subject

Lane 1, packet L1-d. Author: build.builder-2. Date: 2026-10-15. Brief:
`docs/milestones/M0-infinite-session-study.md` (questions 1 and 2 for Codex, plus numbers for the
lane 2 cost model). Companion to `docs/research/M0-interception.md`.

Tags: **[measured: ...]** from the seat's rollout file through `spikes/m0/codex-growth/codex_usage.py`;
**[documented: ...]** vendor source at `rust-v0.154.0` or vendor docs; **[inferred]** not verified.

Owner principle (2026-10-15): compaction is a failure mode to avoid, not a mechanism to optimize,
because model quality degrades as context grows. This run never steered Codex toward compaction.

## Summary

- Six turns on codex-cli 0.154.0 with `gpt-6-astra` took the context in use from 32,331 to 44,552
  tokens, 17% of the reported 258,400-token window. No compaction happened [measured].
- Each tool turn of this shape (one shell command printing 300 lines) cost two model requests and
  added about 3,000 tokens of context. A tool-free turn cost one request and added 123 tokens
  [measured].
- Every request's reported input is consistent with the whole conversation so far: input tokens
  rise with the newly added items each request. Cache reads covered 96% of input in turns 2 to 6
  [measured]. On the wire, Codex's WebSocket transport sends a delta with `previous_response_id`
  (measured by L1-a), and its HTTP fallback sends the full input (measured in M0-interception.md);
  billing is the full conversation either way (section 4).
- A tool-free recall question about turn 2 and turn 5 outputs was answered correctly at 44.5K
  context [measured]. This is one probe, not the fidelity baseline (section 6).
- Spend for this packet: 351,842 tokens over 9 requests (turns 2 to 6), 351,463 input of which
  337,664 were cache reads, 379 output [measured]. Turn 1 was the seat's OpenRig onboarding
  (58,985 tokens, section 1), recorded separately; including it, the session's total is 410,827.
  Both ran on the seat's ChatGPT login by owner decision (decision record on queue row
  `qitem-20261015224601-10ad09bd`).

## 1. Setup

- **Seat:** `labcodex.subject` (session `labcodex-subject@contexto`), worktree
  `contexto-worktrees/lab-codex`, `gpt-6-astra`, ChatGPT login (owner decision recorded on
  queue row `qitem-20261015224601-10ad09bd`). No proxy.
  No configuration, model, or auth was changed.
- **Rollout location:** found through the file the seat's Codex process holds open
  (`lsof -p <pid>`), under `~/.codex/sessions/2026/10/15/` [measured]. `session_meta` reports
  `model_provider: openai`, `cli_version: 0.154.0`, `history_mode: paginated` [measured].
- **Turns sent** (with `rig send`, one at a time):
  - Turn 1 was the seat's OpenRig startup at 23:08:59Z, not part of this packet: at launch OpenRig
    delivered the seat's `role.md` and `startup/context.md`, and the seat ran `rig whoami` and
    oriented (attribution by intake, relayed by manager-1). It cost 58,985 tokens over 2 requests
    on the seat's ChatGPT login [measured: rollout `token_count`]. It is recorded as seat
    onboarding overhead, separate from the lane 1 budget, and doubles as a measurement of what
    OpenRig onboarding costs on Codex: after onboarding, 32,331 tokens of context were already in
    use before the first lab task (turn 2) arrived.
  - Turns 2 to 5: "Run exactly this one shell command ... `seq -f 'row %g alpha beta gamma delta
    epsilon' <a> <b> | tee scratch/l1d/t<n>.txt`. Then reply with only the word DONE." Each writes
    and prints 300 synthetic lines in the seat's own scratch directory.
  - Turn 6: "do not run any tool ... reply with two integers": the first row number written to
    `t2.txt` and the last row number written to `t5.txt`.
- **Turn completion:** detected from the rollout (`task_complete` count) by
  `spikes/m0/codex-growth/wait_turn.sh`, not by screen capture.
- **Meter:** after each turn, `spikes/m0/codex-growth/meter.py` summed `total_tokens` from turn 2 on
  and projected the next turn as two requests at the current context plus 3,500 tokens. The run
  stopped at turn 6 because a seventh turn projected to 447,946 tokens, too close to the 450,000
  stop line.
- **Checks:** the four scratch files have 300 lines each; the seat's worktree has no new commits
  [measured: `wc -l`, `git log` in the seat worktree].

## 2. Field mapping

Codex writes one `event_msg` of type `token_count` per model response, with
`info.last_token_usage` (that request), `info.total_token_usage` (cumulative), and
`info.model_context_window` [measured; documented: `codex-rs/protocol/src/protocol.rs:2216-2325`].
A parallel `token_usage_record` per request carries `usage`, `turn_token_usage`,
`thread_token_usage`, `response_id`, `turn_id`, `thread_id` [measured: field names].

| Lane schema field | Source in the rollout |
|---|---|
| `turn` | count of `event_msg` `task_started` |
| `input_tokens` | `input_tokens - cached_input_tokens - cache_write_input_tokens` (uncached input, Anthropic semantics; the subtraction matches Codex's own parser test) |
| `cache_creation_input_tokens` | `cache_write_input_tokens`, 0 on every request here. Caveat: Codex fills the field with 0 when the server omits `input_tokens_details.cache_write_tokens` (`#[serde(default)]`, `codex-rs/protocol/src/protocol.rs:2216`, `codex-api/src/sse/responses.rs:156-160`), so in the rollout an absent field looks like a reported 0. Lane 2's conformance probe saw the Responses API report `cache_write_tokens` explicitly as 0 [measured by builder-4: `.contexto/spikes/l2b/conformance/probe-20261015T232600Z.json` in builder-4's worktree; doc lands with PR #7; the model, `gpt-6-astra`, is as stated in builder-4's report, since the probe record has no model field], so these zeros are most likely reported |
| `cache_read_input_tokens` | `cached_input_tokens` |
| `output_tokens` | `output_tokens` (includes `reasoning_output_tokens`) |
| `request_bytes` | empty: not observable without a proxy |
| `message_count` | `response_item` records in history when the request was sent: every recorded item minus the response's own output items (reasoning, tool calls, assistant messages), reset to the `replacement_history` length at a compaction. It excludes base instructions and tool definitions, which are not `response_item`s, so it is a count of conversation items, not of every input item on the wire |
| `cli`, `model`, `ts` | `codex`, `turn_context.model`, timestamp of the `token_count` record |

Extra raw columns: `codex_input_tokens` (Codex `input_tokens`, which includes cached and
cache-write tokens), `reasoning_output_tokens`, `total_tokens` (input plus output: the context in use after the
response), `model_context_window`, `compactions_so_far`.

The OpenAI `cached_tokens` count is a subset of `input_tokens`, unlike Anthropic where cache fields
add to `input_tokens` [inferred from the OpenAI cache-hit-rate formula, see M0-interception.md
section 4]. Here `cached_input_tokens <= input_tokens` on every request [measured], consistent with
that reading.

## 3. Results

Per turn [measured: `codex_usage.py <rollout> --per-turn`, committed as
`M0-codex-measurements-turns.csv`]:

| Turn | Requests | Input (raw) | Cache read | Output | Context after turn | Growth | `message_count`, last request |
|---|---|---|---|---|---|---|---|
| 1 (OpenRig onboarding) | 2 | 58,743 | 33,024 | 242 | 32,331 | n/a | 9 |
| 2 | 2 | 67,762 | 64,384 | 88 | 35,306 | +2,975 | 14 |
| 3 | 2 | 73,704 | 70,272 | 94 | 38,281 | +2,975 | 19 |
| 4 | 2 | 79,652 | 76,416 | 94 | 41,254 | +2,973 | 24 |
| 5 | 2 | 85,801 | 82,304 | 95 | 44,429 | +3,175 | 29 |
| 6 (no tools) | 1 | 44,544 | 44,288 | 8 | 44,552 | +123 | 31 |

Per request: `M0-codex-measurements-requests.csv` (11 rows). Within a tool turn the first request
reads almost everything from cache (for example turn 2: 32,128 of 32,461) and the second request
pays for the new tool output uncached (turn 2: 3,045 uncached of 35,301) [measured]. Cache writes
were 0 on every request, most likely as reported by the server (see the
`cache_creation_input_tokens` caveat above).

Reasoning tokens: 21 on one request in turn 1, 0 elsewhere [measured]. `model_context_window` is
258,400 on every request, which is 95% of the model's 272,000 window, matching Codex's default
`effective_context_window_percent` [documented: `codex-rs/protocol/src/openai_models.rs:385-387`].

## 4. Question 1: does each turn resend the full history?

- **Billing view [measured]:** yes. Each request's reported `input_tokens` is consistent with the
  previous request's context plus the items added since (for example 35,301 then 35,428 then
  38,276); the rollout gives no independent token count for the added items, so "equal" would be
  inferred. The server processes the whole conversation every request. Prompt caching makes most of it cache
  reads (96% across turns 2 to 6).
- **Wire view, WebSocket [measured by L1-a, builder-1: one throwaway Codex probe through the
  pass-through proxy's WebSocket tunnel; to be published in `docs/research/M0-proxy-measurements.md`,
  doc pending]:** Codex uses a WebSocket with permessage-deflate, and its second request carried
  `previous_response_id` with only 5 items (33 KB) instead of the full history. So on WebSocket the
  wire carries deltas against server-side state, while `token_count` still bills the full
  conversation per request (above). This matches the source [documented:
  `codex-rs/core/src/client.rs:300-370, 1331-1410`].
- **Wire view, HTTP [measured in M0-interception.md section 4]:** when WebSocket is refused, Codex
  sends the full input over HTTP with `store:false` and no `previous_response_id`.
- **This seat:** it held three TLS connections to a Cloudflare address on port 443 during the run
  [measured: `lsof -i`], which does not show which mode it used. Either way, the server-side context
  and the billed input grow with history.
- **For the design [inferred]:** usage grows with history and is billed per request, so a session
  with many tool calls pays roughly two full-context reads per turn. Keeping working context small
  lowers both quality risk and cost; cache reads soften the cost, not the context size.

## 5. Question 2: does Codex auto-compact, and when?

- **In this run: no.** No `compacted` record exists in the rollout and context peaked at 17% of
  the reported window [measured: `codex_usage.py <rollout> --summary`].
- **Threshold [documented: `codex-rs/protocol/src/openai_models.rs:504-526`]:**
  `min(model_auto_compact_token_limit, 90% of context_window)`, about 244,800 tokens for a
  272,000-token window, with a hard cap at 95%. The count is the server-reported `total_tokens` of
  the last response plus a local estimate for newer items. A planted `token_count` above the
  threshold makes Codex compact before the next turn (measured with a local stub, M0-interception.md
  section 2).
- **What the rollout records when it compacts [documented: `codex-rs/history/src/rollout_payload.rs:155-180`]:**
  a `compacted` record with `message`, `replacement_history`, `window_number`, `first_window_id`,
  `previous_window_id`, `window_id`, `compaction_response_id`, `latest_token_usage_record`,
  `retained_context`, `guardian_history`, `replacement_history_metadata`. With the built-in provider the summary is remote and
  opaque (`encrypted_content`). Not observed on this seat, by design.
- **Per-process override:** `-c model_auto_compact_token_limit=<n>` at launch can only lower the
  trigger [documented]. The seat was already running, its config was not edited, and the owner
  principle rules out steering toward compaction, so no override was used.
- **Distance to the threshold [inferred]:** at about 3,000 tokens per tool turn of this shape, this
  session would need about 67 more such turns to reach 244,800 tokens, costing on the order of
  19.6 million tokens in total (two requests per turn at a context that grows each turn, mostly
  cache reads). Real turns with larger tool outputs get there far sooner.

## 6. Quality

- One recall probe: at 44.5K context the model correctly answered a question about rows it had
  printed in turns 2 and 5 without re-reading the files [measured: the answer was compared with the
  expected pair inside the parser; the text is not reproduced].
- The owner's success bar is no compaction ever, with recall and task accuracy equal to the
  full-context baseline. builder-4's probe set does not exist yet; manager-1 will forward it, and it
  will be run on this seat at the small context sizes measured here. Until then, no accuracy claim
  beyond this single probe is made.

## 7. Reproduction

```sh
# Locate the seat's rollout through its open file, then:
python3 spikes/m0/codex-growth/codex_usage.py <rollout.jsonl>             # per-request CSV
python3 spikes/m0/codex-growth/codex_usage.py <rollout.jsonl> --per-turn  # per-turn CSV
python3 spikes/m0/codex-growth/codex_usage.py <rollout.jsonl> --summary   # record counts, compactions, totals
spikes/m0/codex-growth/wait_turn.sh <rollout.jsonl> <n> [timeout]         # wait for the n-th task_complete
python3 spikes/m0/codex-growth/meter.py <usage.csv> <first_turn> <cap>    # spend so far and GO/STOP
```

The parser prints only usage numbers, record types, field names, and counts. Raw rollout files stay
where Codex writes them.

## 8. Token spend

| Scope | Requests | Input (raw) | Cache read | Output | Total |
|---|---|---|---|---|---|
| Turns 2 to 6 (this packet) | 9 | 351,463 | 337,664 | 379 | 351,842 |
| Turn 1: seat onboarding overhead (OpenRig startup; not lane 1 budget) | 2 | 58,743 | 33,024 | 242 | 58,985 |

Model: `gpt-6-astra`. Budget: 500,000 per experiment and 3,000,000 per packet (shared with L1-b,
whose experiments used 0 model tokens).

## 9. Open questions

- Which transport this seat used (L1-a measured WebSocket deltas on a throwaway probe; the seat's
  own mode was not observed).
- The rollout cannot distinguish a reported 0 cache write from an omitted field; lane 2's probe
  indicates the API reports it explicitly.
- The fidelity probe set and the full-context baseline (builder-4) are pending.
- Turn shapes with large tool outputs, file reads, or reasoning-heavy work would grow faster; this
  run measured one small, uniform shape.
