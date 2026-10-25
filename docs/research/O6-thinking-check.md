# O6 dropped-thinking check (2026-10-25)

D36 pinned each session to one credential on the owner's proxy. This run checks the half of D36's
verification that was still open: with `--observe-thinking`, does a real Claude Code session on
`claude-opus-5-5` show any `end_user_binding_mismatch` (dropped thinking)? The owner approved the
run on 2026-10-25. The record holds ids, counts, booleans, versions and timestamps only. No prompts,
replies, thinking or transcripts are recorded. The run's artifacts stay private, outside any
repository.

## What the log can and cannot show

- `--observe-thinking` adds the `thinking-binding-controls-2026-08-01` beta to every forwarded
  request (`src/contexto/proxy.py`). The proxy copies any `input_transformations` list from
  `message_start` or `message_delta` into the request record's `input_transformations` field. A
  dropped block shows there as `{"type": "thinking_dropped", "reason": "end_user_binding_mismatch",
  "location": "messages.N"}`.
- The session log does not count thinking blocks in a request; it records message counts and bytes.
  The replayed-thinking counts below come from the run's own Claude Code session JSONL. Its
  `api-request` records were decoded the way the replay baseline decodes them
  (`spikes/m2/acceptance/replay.py`), with one addition (see "What went wrong").
- The session log records no replica or credential identity. Whether requests crossed the proxy's
  replicas cannot be seen from the client. Credential sameness can be inferred from cache reads and
  writes only.

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
| Preflight | `run.py check-credential`: pass (key matched the seat helper, no OAuth beta). Zero-spend stub run of the same driver: 7 of 7 turns, binding beta on every request, thinking `adaptive` on main requests |
| Session window | 01:55:35Z to 01:56:36Z |
| Controls window | 01:58Z to 02:03Z |

## Verdicts

| Check | Result | Figures |
|---|---|---|
| Session completed | T | 7 of 7 turns, exit 0, no error turn, no watcher stop |
| Requests | T | 18 POST `/v1/messages`, all 200 (17 main conversation, 1 side request); 7 turns of 2 to 3 requests |
| Tool use | T | 14 tool calls: Write 5, Bash 7, Edit 2 |
| Replayed thinking | T | 14 of 17 main requests carried signed thinking from an earlier turn; 4 also carried thinking from earlier in the same turn's tool loop |
| Dropped-thinking signals | 0 | `input_transformations` present on 18 of 18 responses, every one an empty list; 0 `thinking_dropped` items |
| Contexto left requests unchanged | T | 18 of 18 forwarded with the CLI's own bytes (`request_bytes` = `original_request_bytes`); no rotation, epoch 0 throughout |
| Compaction | 0 | 0 `compact_boundary` events; largest input 33,391 against the bound 150,000 |
| Positive control for the signal | not obtained | see "Controls" |

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

Each main request wrote only its new tail and read everything before it, so the session was on one
credential throughout (inferred from the cache). O6's original cause, one session spread across
credentials, did not occur.

## Controls (direct to the owner's proxy)

Direct requests with the harness's user agent and key headers, as the replay baseline sends them.
Each used Claude Code's thinking setting, effort and betas from the run (minus `claude-code-*`), plus
the binding beta. Request A produced a signed thinking block under `X-Session-Affinity` key k0.
Requests B replayed A's blocks verbatim under k0 and under other random keys. In controls 2 and 3 an
11,400-token cached system prefix shows whether a key reached a credential holding k0's cache: a
fresh write means it did not.

| Control | B requests | B with a fresh cache write (other credential) | `thinking_dropped` | Field present | Tokens |
|---|---|---|---|---|---|
| 1. beta only, no cacheable prefix | 5 (k0 to k4) | unknown (below the cache minimum) | 0 | 6 of 6 | 1,540 |
| 2. beta only, cached prefix | 9 (k0 to k8) | 3 | 0 | 10 of 10 | 114,766 |
| 2b. as 2, plus `prefix_mismatch_behavior: drop_block` and a fixed `metadata.user_id` (the M0 L1-e settings) | 9 (k0 to k8) | 3 | 0 | 10 of 10 | 114,797 |
| 3. as 2b, B's `metadata.user_id` differs from A's, all on k0 | 3 | 0 | 0 | 4 of 4 | 45,884 |
| 4. no binding beta, one request | - | - | - | 0 of 1 | 151 |

- The field follows the beta: present on 48 of 48 responses with it (the session and controls 1 to
  3), absent on 1 of 1 without it.
  So the beta reaches the API through the proxy and the reporting channel is on.
- No arrangement produced a drop report: 6 replays onto another credential (by cache evidence), 3
  replays under a different end-user id, with and without `drop_block`. On 2026-10-15 the L1-e
  settings through the owner's proxy reported drops on most replays (`M0-interception.md`). The
  cause of the change is not known from the client side.
- A functional check was tried at small size (1,590 tokens): a number chosen only in A's thinking,
  asked for in B. It could not be interpreted. Under `display: "updates"` the returned thinking text
  is empty, so the number could not be confirmed to exist, and the model answered NONE on k0 as well.

## Spend

| Line | Tokens |
|---|---|
| Session (proxy log usage, 18 requests) | 525,403 |
| Controls 1 to 4 | 277,138 |
| Functional check (3 small attempts) | 1,590 |
| **Total** | **804,131** (limit 2,000,000; the session's watcher was armed at 1,900,000) |

The acceptance budget tools (`budget.py`, `codex_budget.py`) model the acceptance script, not this
session, so the hard limit was the harness's spend watcher. Its worst case is the approval itself,
1,900,000, below 25,000,000.

## What went wrong

- The replay decoder stopped on 3 of 50 message hashes. Claude Code 2.1.302 writes one user record
  per `tool_result`, but sends the results of parallel tool calls as one message. Merging consecutive
  `tool_result` records, as the decoder already does for assistant blocks, resolved all 3. The fix was
  made in the analysis script only; the harness is unchanged.
- `HEAD /api/hello` returned 401 once. It is Claude Code's unauthenticated connectivity probe on a
  non-model path, known from earlier runs.

## Reading

- Measured: no dropped thinking was reported in the session or in any control, and the session stayed
  on one credential by cache evidence.
- Not shown: that a cross-credential drop would still be reported on this route today. The control
  meant to show it produced no drop report at all, so a zero from the signal is consistent with kept
  thinking but does not prove it.
