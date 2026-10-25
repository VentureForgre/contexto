# O6 dropped-thinking check (2026-10-25 UTC)

D36 pinned each session to one credential on the owner's proxy. STATUS.md records the deployment
as 2026-10-24. This run checks the half of D36's verification that was still open: with
`--observe-thinking`, does a real Claude Code session on `claude-opus-5-5` show any
`end_user_binding_mismatch` (dropped thinking)? The owner approved the run on 2026-10-25. The record
holds ids, counts, booleans, versions and timestamps only. No prompts, replies, thinking or
transcripts are recorded. The run's artifacts stay private, outside any repository. Every date and
time here is UTC.

## What the log can and cannot show

- `--observe-thinking` adds the `thinking-binding-controls-2026-08-01` beta to every forwarded
  request (`src/contexto/proxy.py`). The proxy copies the `input_transformations` list from
  `message_start` or `message_delta` into the request record's `input_transformations` field. A
  dropped block shows there as `{"type": "thinking_dropped", "reason": "end_user_binding_mismatch",
  ...}`.
- The proxy keeps only the last value it sees (`UsageReader._take_transformations`,
  `proxy.py:533-537`). A drop reported in `message_start` and followed by an empty list in
  `message_delta` would be logged as `[]`. A separate fix is pending in PR #222 (in review, not
  merged): it makes the proxy keep every distinct item across events. (Later: PR #222 merged on
  2026-10-25 and shipped in v0.5.2; it merges the lists by count.) This run does not change
  `src`. In every streamed response that was
  read event by event (16 on `claude-haiku-5-5`, 1 on `claude-opus-5-5`), the field came only in
  `message_start`, and `message_delta` never carried it. The last-wins reading would not have hidden
  a drop in those responses. The Opus controls below were read last-wins, like the proxy.
- The session log does not count thinking blocks in a request; it records message counts and bytes.
  The replayed-thinking counts below come from the run's own Claude Code session JSONL (see "What
  went wrong" for how it was decoded).
- The session log records no replica or credential identity, so whether requests crossed the proxy's
  replicas cannot be seen from the client. Credential sameness is inferred from cache reads and
  writes. That inference assumes no two of the proxy's credentials share a cache
  (`docs/cliproxyapi.md`: "Each credential has its own cache").

## Code and session

| Item | Value |
|---|---|
| Code | main `9d2949a2df1e955a78585649e50a8e9ef2490262`, a clean detached checkout |
| Session | `thk20261025T015534Z` |
| Client and model | Claude Code 2.1.302, `claude-opus-5-5` |
| Thinking | `{"type": "adaptive", "display": "updates"}` on 17 of 17 main requests, effort medium (Claude Code's own settings) |
| Launcher | `contexto run --observe-thinking --bound 150000 ... claude -p --input-format stream-json --output-format stream-json --verbose --model claude-opus-5-5 --permission-mode acceptEdits --allowedTools <python3, ls, cat>` |
| Upstream | the owner's proxy; key from the seat's key helper at call time, in no file |
| Driver | the acceptance harness's driver (isolated config, child environment, stream-json transport, auth and spend watcher); 7 scripted turns of small coding work |
| Preflight | `run.py check-credential`: pass (key matched the seat helper, no OAuth beta). Zero-spend stub run of the same driver: 7 of 7 turns, binding beta on 14 of 14 requests, thinking `adaptive` on 7 of 7 main requests |
| Session window | 2026-10-25 01:55:35Z to 01:56:36Z |
| Opus controls | 2026-10-25 01:58Z to 02:03Z |
| Haiku positive control | 2026-10-25 02:12:10Z to 02:14:16Z |

## Verdicts

| Check | Result | Figures |
|---|---|---|
| Session completed | T | 7 of 7 turns, exit 0, no error turn, no watcher stop |
| Requests | T | 18 POST `/v1/messages`, all 200; 7 turns of 2 to 3 requests. By Claude Code's 17 `api-request` records: 17 main conversation, 1 side request. Contexto's `main` flag in the log says 15 and 3: the side request came first and was main until the real main's second turn start (`rewrite.py:1958-1985`) |
| Tool use | T | 14 tool calls: Write 5, Bash 7, Edit 2 |
| Replayed thinking | T | 14 of 17 main requests carried signed thinking from an earlier turn; 4 also carried thinking from earlier in the same turn's tool loop |
| Dropped-thinking signals | 0 | `input_transformations` present on 18 of 18 responses, every one an empty list; 0 `thinking_dropped` items |
| Contexto left request bodies unchanged | T | 18 of 18 forwarded with the CLI's own body bytes (`request_bytes` = `original_request_bytes`); the headers gained the one beta; no rotation, epoch 0 throughout |
| Compaction | 0 | 0 `compact_boundary` events; largest input 33,391 against the bound 150,000 |
| The signal still fires | T on `claude-haiku-5-5`; not shown on `claude-opus-5-5` | see "Positive control" and "Opus controls" |

## Thinking in the session

| Item | Count |
|---|---|
| Main-conversation responses with a thinking block | 6 of 17 (adaptive thinking skipped the rest) |
| Requests replaying signed thinking | 14 of 17 |
| Replayed block instances, summed over requests | 46, all signed, 0 redacted |
| Most blocks in one request | 6 (the last two requests; every block the session produced) |
| `thinking_dropped` items | 0 |

Per turn (requests / requests with replayed thinking): 1: 3 / 0, 2: 2 / 2, 3: 2 / 2, 4: 2 / 2,
5: 3 / 3, 6: 2 / 2, 7: 3 / 3. Turn 1 had none to replay; its last response produced the first block.

## Cache

| Item | Value |
|---|---|
| Requests with a cache read | 17 of 18 |
| Requests with a cache write | 17 of 18 |
| Request with neither | the 1 side request (1 message, 1,236 input tokens, below the cache minimum) |
| Exact prefix chain | 16 of 16: from request 3 on, every cache read equals the previous request's read plus write |
| First main request | read 22,150 (the system and tools prefix, already cached), wrote 5,436 |
| Tokens | cache write 11,239; cache read 508,970; uncached input 1,270; output 3,924 |

Each main request wrote only its new tail and read everything before it. So the session stayed in one
cache scope, which means one credential if no two credentials share a cache [inferred]. By the same
evidence, O6's original cause, one session spread across credentials, did not occur.

## Positive control: `claude-haiku-5-5` (direct to the owner's proxy)

This is the shape of 2026-10-15's M0 L1-e Series A (`spikes/m0/route-identity/probe.py`):
- `claude-haiku-5-5` with `{"type": "enabled", "budget_tokens": 1024}` and
  `block_binding.prefix_mismatch_behavior: drop_block`;
- a fixed synthetic `metadata.user_id`, the binding beta alone, and `max_tokens` 1500;
- L1-e's multiplication questions.

It differs from L1-e in three ways:
- the harness's user agent;
- an 11,400-token cached synthetic system prefix, added so that cache reads and writes show which
  credential served each request;
- in part 2, `X-Session-Affinity` keys that put replays onto other credentials.

The control ran once with non-streaming JSON and once streaming. Each response's
`input_transformations` was recorded at every place it appeared: the JSON body and its `usage`, and
the stream's `message_start` message and usage, and `message_delta` and its usage.

Part 1 is L1-e's pure appends with no session signal: 6 requests, each replaying every earlier block.

| Transport | Requests | Fresh cache writes | Replays | `thinking_dropped` |
|---|---|---|---|---|
| JSON | 6 | 2 (requests 1 and 2) | 4 | 0 |
| Streaming | 6 | 2 (requests 1 and 2) | 4 | 0 |

With no session signal, requests 1 and 2 landed on different credentials: request 2 read nothing
after request 1's write. The first thinking block came from request 2. Every later request read
request 2's cache chain, so no replay left that credential [inferred, as above]. What kept them
together is not shown here; D36 says requests with no session signal go through CLIProxyAPI's
per-process prefix matcher (`docs/DECISIONS.md`, D36 "Limits"). This is the outcome that differs
from 2026-10-15: L1-e Series A reported drops on 8 of its 9 replaying requests, and part 1 here
reported 0 of 8.

Part 2: request A under key k0 produces a signed block. Requests B replay it under k0 to k6, and
once under k0 with another `metadata.user_id`. Placement is by what each B request read
[inferred, as above]:
- a read of A's cache chain (JSON 11,372, or 11,468 after B-k0's tail; streaming 11,371, or
  11,461) puts it on A's credential;
- a fresh write (JSON 11,394, streaming 11,393), or a read of exactly that fresh write, puts it on
  another credential.

| Transport | B requests | On A's credential | On another credential | Dropped on A's | Dropped on another |
|---|---|---|---|---|---|
| JSON | 8 | 3 (k0, k1, k0 with other user) | 5 (k2, k4 fresh; k3, k5, k6 read a B request's write) | 0 of 3 | 5 of 5 |
| Streaming | 8 | 5 (k0, k2, k3, k5, k0 with other user) | 3 (k1 fresh; k4, k6 read k1's) | 0 of 5 | 3 of 3 |

- Every drop reported `thinking_dropped` with reason `end_user_binding_mismatch`, with no `location`
  value (the script cannot tell a missing key from a null). JSON reported it in the body's top-level field. The stream
  reported it only in `message_start`'s message; `message_delta` carried no field in any of the 16
  streamed responses (the first run's 6 part-1 requests and its one A request, which produced no
  thinking; the rerun's A and 8 B requests).
- A different `metadata.user_id` on A's credential caused no drop (0 of 2). On this route the binding
  follows the credential, not the end-user id sent.
- So the API still reports dropped thinking on `claude-haiku-5-5` through the owner's proxy, in both
  transports, when a replay lands on another credential [inferred, as above].
- The affinity key decides which credential serves a replay: the same key kept it on A's credential
  (4 of 4, B-k0 and B-k0 with the other user id, both transports), and other keys moved 8 of 12 to
  another credential.

## Opus controls (`claude-opus-5-5`, direct to the owner's proxy)

These ran before the haiku control.
- Model and thinking: all used `claude-opus-5-5` with Claude Code's thinking setting from the
  session (`adaptive`, `display: "updates"`, effort medium).
- Betas and transport: Claude Code's betas (minus `claude-code-*`) plus the binding beta; streaming,
  read last-wins.
- Requests: A produced a signed block under key k0, and B replayed it verbatim.
- Cache evidence: controls 2 and 3 added the 11,400-token cached system prefix; a fresh write means
  the key reached a credential that did not hold the prefix.

| Control | B requests | B with a fresh cache write (another credential) | `thinking_dropped` | Field present | Tokens |
|---|---|---|---|---|---|
| 1. beta only, no cacheable prefix | 5 (k0 to k4) | unknown (below the cache minimum) | 0 | 6 of 6 | 1,540 |
| 2. beta only, cached prefix | 9 (k0 to k8) | at least 3 | 0 | 10 of 10 | 114,766 |
| 2b. as 2, plus `drop_block` and a fixed `metadata.user_id` | 9 (k0 to k8) | at least 3 | 0 | 10 of 10 | 114,797 |
| 3. as 2b, B's `metadata.user_id` differs from A's, all sent on k0 (the file labels two of them k1 and k2) | 3 | 0 | 0 | 4 of 4 | 45,884 |
| 4. no binding beta, one request | - | - | - | 0 of 1 | 151 |
| 5. one small streaming request, no replayed thinking, read event by event | - | - | - | `message_start` only | 151 |

- Fresh-write counts are lower bounds. A read can come from a prefix another B request wrote, not
  only from A's, so at least 6 Opus replays across controls 2 and 2b landed on another credential.
- Control 2b is not L1-e's setup. It differs in model (`claude-opus-5-5`, not `claude-haiku-5-5`),
  thinking (adaptive with `display: "updates"`, not `enabled` with budget 1,024), betas (Claude
  Code's set, not the binding beta alone), and transport (streaming, read last-wins, not
  non-streaming JSON).
- The field follows the beta: present on 48 of 48 responses with it (the session and controls 1 to
  3), absent on 1 of 1 without it. That shows the beta reaches the API and the field is returned.
  It does not show that Opus drops get reported.
- Control 5 carried no replayed thinking and none of Claude Code's betas. It shows where the field
  sits in an Opus response with nothing dropped, not where an Opus drop item would sit.
- Every drop on 2026-10-15 was on `claude-haiku-5-5`. The one `claude-opus-5-5` series that day (M0
  L1-e Series C, `M0-interception.md`) kept every replayed block and reported no drop, and did not
  show that its replays left a credential. Here at least 6 Opus replays landed on another credential
  [inferred, as above], and none reported a drop. The haiku control shows the signal and route still
  work, so on Opus no change is shown. Three readings fit: Opus 5.5 thinking is not bound per
  credential; Opus drops it without reporting; or a drop was reported in an event that the last-wins
  read overwrote, which no stream read event by event showed.
- A functional check was tried at small size: a number chosen only in A's thinking, asked for in B.
  It could not be interpreted. Under `display: "updates"` the returned thinking text is empty, so the
  number could not be confirmed to exist, and the model answered NONE on k0 as well. Its 1,590 tokens
  over 3 attempts are from the run's notes; only the last attempt's file (1,140) remains.

## Spend

| Line | Tokens |
|---|---|
| Session (proxy log usage, 18 requests) | 525,403 |
| Opus controls 1 to 5 | 277,289 |
| Functional check (3 small attempts, from notes) | 1,590 |
| Haiku positive control (JSON 171,791; streaming part 1 with a part 2 that stopped on no thinking 80,273; streaming part 2 103,012) | 355,076 |
| **Total** | **1,159,358** (limit 2,000,000; the session's watcher was armed at 1,900,000) |

The acceptance budget tools (`budget.py`, `codex_budget.py`) model the acceptance script, not this
session, so the hard limit was the harness's spend watcher. This run's brief set a stop rule for a
worst case above 25,000,000 tokens; the worst case here was the watcher's approval, 1,900,000.

## What went wrong

- `replay.recorded_messages` (`spikes/m2/acceptance/replay.py`) could not decode the session. It
  stopped on 3 of 50 message hashes: Claude Code writes one user record per `tool_result` but sends
  parallel tool calls' results as one message. This is known, not new in 2.1.302. It has been seen
  since live run #2. `classa.py` already merges such runs (`_known_messages`), and
  `spikes/m2/acceptance/tests/test_classa.py:132-155` pins that `replay.recorded_messages` raises on
  this shape.
  - It does not affect `run.py score`, which reads the answer files and the H1 record.
  - `run_replay` refuses (exit 2) on it rather than building a wrong baseline.
  - This run's analysis decoded with the same merge `classa.py` does; the harness is unchanged.
- The first streaming haiku run's part 2 stopped because A produced no thinking block. Part 2 was
  rerun alone with a harder fallback question; its A attempt 0 produced a block.
- `HEAD /api/hello` returned 401 once. It is Claude Code's unauthenticated connectivity probe on a
  non-model path, known from earlier runs.

## Reading

- Measured: no dropped thinking was reported in the Opus session. The session stayed on one
  credential by cache evidence [inferred, as above].
- Measured: on `claude-haiku-5-5` the API still reports drops through the owner's proxy whenever a
  replay lands on another credential [inferred, as above], in JSON and streaming, in
  `message_start`. The affinity key decides the credential: same key 4 of 4 on A's, other keys 8 of
  12 elsewhere. Unkeyed replays also stayed on one credential (0 of 8 dropped), unlike 2026-10-15.
- Not shown: whether Opus 5.5 thinking is ever dropped across credentials. Opus replays onto other
  credentials reported no drop here; the one Opus series on 2026-10-15 also reported none, with
  crossing unknown. For an Opus session, a zero from the signal
  says nothing either way; the one-credential cache evidence is the stronger result.
