# Proxy at scale: a 30M-token session through the real proxy (zero spend)

Can the Contexto proxy carry a Claude Code session whose history reaches about 30 million tokens?
This harness drives the real proxy over HTTP with a synthetic Claude Code-shaped session and a local
fake upstream, and measures it at 0.5M, 2M, 5M, 10M and 30M tokens of history. Nothing talks to a
model API: the proxy's upstream is `http://127.0.0.1:<port>` and `worker.py serve` refuses anything else.

Measured on 2026-10-17, main at 4525388, Python 3.14.6, Apple M5 Pro (18 cores, 48 GB), with other
agents benchmarking on the same machine. 1-minute load averages were 3.1 to 7.4 during the timed
runs (`load1` in `results/requests_*.csv`; the `uptime` line for each checkpoint is in
`results/checkpoints_*.csv`). Every number below is measured unless it is marked as an inference.

The harness was repaired on 2026-11-12 and a 200-turn version of it now runs in the test suite: see
"2026-11-12: repaired at v0.5.24" at the end. Everything between here and that section is the
2026-10-17 record and is left as it was written, apart from two notes that say so.

## Short answer

- **With a context block on the first message only and one session-start system message (the `first`
  profile), the proxy carries 30M tokens.** That is the context-block placement Claude Code 2.1.302 uses
  on 5.x model ids, but those ids also send a role `system` message with every main request, which this
  run does not model (`../reminders/`). Every upstream request stayed under the 150,000 bound
  (largest measured 135,954 tokens; largest last request of any fast-forwarded turn 144,176). There were no `over_bound`,
  `bound_too_small` or `transform_error` events, and no non-200 responses. At 30M the proxy added 0.58 s
  to a tool-loop request and 0.86 s to a turn start. The cost grows linearly with history. With the
  CPU fixes in "Before and after" it is 0.26 s and 0.56 s, with byte-identical output.
- **What breaks first is carried `<system-reminder>` growth, not size.** If later turn starts also
  carry reminders (the assumed `mixed` profile), the fixed overhead F grows by about 215 tokens per
  turn without limit, because context blocks of paged turn starts are carried forever and nothing
  pages them. In that profile `bound_too_small` first fires at **4.86M tokens** (turn 181, F =
  100,810, W = 11,690), and the first request over the bound comes at **8.20M** (turn 308, last
  request of the turn 150,046 tokens, F = 122,305, W = -3,473). At 10M, 7 of the 22 requests
  measured at that checkpoint were over the bound, all tool loops (151,779 to 159,255). Where the
  CLI puts reminders depends on the model id (measured on Claude Code 2.1.302 in `../reminders/`).
  On 4.x ids it puts `<system-reminder>` blocks in every later turn start: 45 to 425
  Contexto-estimator tokens per turn, and the `mixed` profile's 297 bytes per turn lies between the
  measured floor (126) and the busy one-process rate (701). On 5.x ids context blocks stay at
  `messages[0]` and the per-turn text arrives as role `system` messages, one per main request, which
  the fallback can page but this run did not include. [Inferred, not measured] That text probably
  still fits at 30M. Carried content counts in F, and the engine sizes the window as bound -
  headroom - F (`rewrite.py:731`, `:735`), so more F shrinks the window instead of pushing requests
  toward the bound. F here was about 70,000 at 30M, against the 100,810 at which `bound_too_small`
  first fired in `mixed`: about 30,800 tokens of room. In the reminders resume run the pageable text
  held 410 to 2,143 tokens once the fallback engaged (at each rotation the fallback pages it back to
  about 8,000 or less); effort changes on claude-opus-5-5 and claude-haiku-5-5 add unpageable
  content to the same F. The fallback also adds pages, one per paged message.
- **Memory is the next limit.** RSS is not a Python-object leak (live objects stay flat at about
  23,000), but the allocator keeps what each request frees. Over 79 requests at 30M, RSS after
  `gc.collect()` settled between 2.1 and 3.1 GB, with a peak RSS of 3.45 GB. At 5M it levels off near
  650 MB after about 25 requests.

## Command

```
python3 -m venv .venv && .venv/bin/pip install -q -e . pytest
.venv/bin/python spikes/m2/scale/proxy/run.py --label first-1        # about 7 min; 0.5M..30M
.venv/bin/python spikes/m2/scale/proxy/summarize.py first-1 first-2 first-3   # the table below
```

[Note added 2026-11-12] A label names the output files, and the labels used in this document
(`first-1`, `mixed-1`, `perf-1` and so on) name the committed 2026-10-17 files, so running these
commands as written overwrites that record. Use a new label, as in the 2026-11-12 section.

Other entry points:

| Command | What it measures | Time |
|---|---|---|
| `run.py --label mixed-1 --reminders mixed` | the same run with later-turn reminders; stops at the first failure | 30 s |
| `run.py --label mixed-keep --reminders mixed --checkpoints 5,7.5,10 --keep-going --profile-at 0 --http-until 0` | the mixed profile carried past the failure to 10M | 80 s |
| `fidelity.py --turns 120 [--reminders mixed]` | fast-forward against every request over HTTP (below) | 4 min |
| `leak.py --at 30 --turns 25` | RSS and live objects after every request in one proxy process | 10 min |

`run.py` makes a fresh `CONTEXTO_HOME` under `/tmp` and removes it at the end (`--keep-home` keeps it).
Options: `--head-tokens` (default 50,000), `--turn-tokens` (26,000), `--bound` (150,000), `--rounds` (3),
`--turns-per-round` (2), `--profile-at` (10), `--http-until` (0.5), `--seed`.

## What is driven

- **Proxy** (`worker.py serve`, one process per phase): `contexto.proxy.ContextoProxy` with
  `launcher.NoticeLog(store)` as the session log and a `RewriteEngine`, wired the way
  `launcher.start_proxy` (the code `contexto run` uses) wires them. A new engine comes from
  `launcher.build_engine`, and the store is opened with `launcher.open_session`. What differs from
  `contexto run`: no `claude` child is started; timers wrap the handler's `_read_body` and `_transform`,
  the engine call, `observe_usage`, and the store's methods (as harness-side wrappers, with `src/`
  unchanged); and the engine is pickled between phases. The pickle carries the whole engine object
  (epoch states, estimator, private fields) to the next process. This is not a restart, and the
  rule 16 resume path is not used.
- **Fake upstream** (`upstream.py`, a thread in the driver): it streams Messages SSE (`message_start`
  with usage, a text block, a `tool_use` block when the driver asks for one, then `message_delta` with
  `stop_reason` and output tokens, then `message_stop`), chunked, so the proxy's stream hold (PR #74)
  runs. Usage is `ceil(forwarded bytes / 1.83)`, split into 2 uncached tokens and the rest cache read.
- **Synthetic session** (`synth.py`, deterministic from a seed):
  - Head (`system` + 40 `tools`) of 49,885 tokens.
  - `messages[0]` is a user message with a 3,000-token `<system-reminder>` and the prompt, and
    `messages[1]` is the 8,470-character role `system` message with `output_config` (finding 1 in
    spikes/m2/carried).
  - Turns average 26,300 tokens (19,300 to 33,000): a prompt, 0 to 4 tool rounds (assistant thinking
    with signature + text + `tool_use`, then a `tool_result`), and a final assistant message.
  - 3.2 requests per turn on average. `cache_control` moves to the newest message on each request.
  - Bodies are built byte-identical to `serialize_request` (a unit test checks this) and resend the
    full history every time, as the CLI does.
- **Reminder profiles**:
  - `first`: only `messages[0]` has a context block ("context blocks stay at messages[0]",
    spikes/m2/carried finding 5), and the only role `system` message is the session-start one at
    `messages[1]` (`synth.py:176`). This matches Claude Code 2.1.302 on 5.x model ids except for their
    per-request role `system` text (`../reminders/`).
  - `mixed` [an assumed rate]: 35 percent of later turn starts carry a 420-byte todo-style reminder
    and 10 percent a 1,500-byte file-modified reminder. `../reminders/` measured later turn-start
    reminders on 4.x model ids; this profile's 297 bytes per turn lies between their measured floor
    (126 bytes per turn) and the busy one-process rate (701).

## How each checkpoint is reached

| Checkpoint | Turn | How it was reached | Faithful? |
|---|---|---|---|
| 0.5M | 17 | every request over HTTP from turn 0 (47 requests) | yes, it is the real path |
| 2M, 5M, 10M, 30M | 76, 188, 376, 1140 | fast-forward from the previous checkpoint, then measured over HTTP | decisions match up to threshold ties (below) |

Fast-forward (`worker.py ff`, 4 s to 2M and 321 s from 10M to 30M):

- It calls the same engine object directly, on the same store, at each turn start only. The request is
  the dict the proxy would parse from the CLI's body.
- It feeds `observe_usage` the usage the fake upstream would report, computed from the bytes the proxy
  would forward, and writes a proxy-shaped entry with the transform and usage events to the session log.
- It skips the tool-loop requests of fast-forwarded turns (2.2 per turn) and the proxy's JSON work.
  Rotation happens only at turn starts, so it skips no rotation decision.
- It writes each turn's last request size (`turn_peak_tokens` in `ff_*.csv`), the request it does not
  send. That size is exact in bytes, because nothing rotates inside a turn.

Each checkpoint is then measured over HTTP in a fresh proxy process: 3 rounds of 2 turns, every request
of those turns. At 10M one more profiled round runs and is excluded from the timings.

`fidelity.py` reaches turn 120 (3.2M tokens) both ways in separate homes, then sends turn 120 over
HTTP in both (`results/fidelity_*_120.json`):

- **`mixed` profile:** identical. All 78 rotation events match (epoch, k, reason, turns paged, F, W),
  and so do all 120 pages' turn and messages and the next turn's upstream bytes. Turn-start estimates
  differ by at most 143 tokens (0.16 percent).
- **`first` profile:** 120 identical pages and byte-identical next-turn requests, but one decision
  differs. At turn 71 the HTTP path estimated 113,001 and fast-forward 112,930, against the
  `min_turn_room` threshold of 113,000 (bound - floor - min room). HTTP rotated there (k = 441).
  Fast-forward rotated one turn later (k = 455). Request sizes differ at turns 71 to 75 and match
  again from turn 76 (epoch 37), so the paths re-converge after 5 turns. At epoch 36 both rotate at
  the same k but with a different reason, F and W (W 42,740 against 53,409): the JSON reports
  `rotations_equal: false` (2 of 60 rotations differ) and `max_f_w_diff_tokens` 10,669.
- **Cause:** fast-forward anchors the rule 3 estimate on the previous turn start's real usage, not on
  the last tool loop's. That moves the estimate by about 0.1 percent and can flip a decision that sits
  exactly on a threshold.
- **Not exercised by fast-forward:** the room and retained-snapshot store writes of skipped tool-loop
  requests, and the stream hold. The HTTP rounds exercise both at every checkpoint.

Each measurement process lives about 20 requests, so the table's peak RSS is per checkpoint. `leak.py`
measures a longer life.

## Results (`first` profile; 3 runs, `results/*_first-{1,2,3}.csv`)

Seconds. The value is the median of the three runs' medians; p95 pools all samples of the three runs.
"Proxy transform" is the handler's `_transform`: JSON parse, copy, engine, serialize. "e2e" is the
driver's send to the last byte. The fake upstream answers at once, so e2e is almost all proxy.

| History (M tokens) | Body MB | Turn start e2e | Tool loop e2e | Proxy transform, turn start | Proxy transform, tool loop | Engine, tool loop | Rotating turn start e2e | index_text per call | Max upstream tokens | Peak RSS MB | Pages | Store MB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.50 to 0.65 | 0.9 to 1.2 | 0.056 (p95 0.117, n 18) | 0.014 (p95 0.015, n 51) | 0.054 (p95 0.116) | 0.012 (p95 0.014) | 0.007 (p95 0.008) | 0.110 (p95 0.117, n 9) | 0.045 | 134,587 | 44 | 22 | 1.16 |
| 2.04 to 2.18 | 3.7 to 4.0 | 0.099 (p95 0.158, n 18) | 0.044 (p95 0.046, n 21) | 0.096 (p95 0.155) | 0.042 (p95 0.043) | 0.024 (p95 0.025) | 0.156 (p95 0.160, n 9) | 0.052 | 134,587 | 180 | 80 | 4.04 |
| 5.00 to 5.14 | 9.2 to 9.4 | 0.103 (p95 0.231, n 18) | 0.101 (p95 0.102, n 42) | 0.098 (p95 0.227) | 0.096 (p95 0.097) | 0.057 (p95 0.058) | 0.230 (p95 0.234, n 6) | 0.061 | 135,954 | 522 | 192 | 9.63 |
| 10.00 to 10.15 | 18.3 to 18.6 | 0.278 (p95 0.362, n 18) | 0.194 (p95 0.198, n 42) | 0.271 (p95 0.357) | 0.188 (p95 0.192) | 0.111 (p95 0.113) | 0.358 (p95 0.365, n 9) | 0.075 | 135,954 | 1,337 | 382 | 19.15 |
| 30.03 to 30.19 | 55.0 to 55.2 | 0.861 (p95 0.875, n 18) | 0.580 (p95 0.596, n 33) | 0.846 (p95 0.861) | 0.566 (p95 0.579) | 0.332 (p95 0.336) | 0.867 (p95 0.875, n 12) | 0.131 | 135,954 | 1,707 | 1,145 | 56.78 |

How to read the columns:

- **Body MB** is what the CLI sends. Upstream bodies stay at 0.1 to 0.3 MB.
- **Pages and Store MB** are measured on disk. The store holds 1,145 one-turn pages, 56.0 MB, about
  49 KB per page; the session log is 0.78 MB at 30M.
- **Timing spread** between the three runs: the per-run 30M medians were 0.574 to 0.586 s for a tool
  loop and 0.859 to 0.866 s for a turn start.
- **Peak RSS** is `ru_maxrss` of the checkpoint's proxy process, the same in all three runs to within
  1 MB.

Engine events in the session log through each checkpoint (first run; the other two are identical):

| History (M) | Reached by | rotation | window_minimal | bound_too_small | over_bound | carried_over_budget | turn_room_low | transform_error |
|---|---|---|---|---|---|---|---|---|
| 0.5 | http | 10 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2 | fast-forward | 39 | 0 | 0 | 0 | 0 | 0 | 0 |
| 5 | fast-forward | 93 | 1 | 0 | 0 | 0 | 0 | 0 |
| 10 | fast-forward | 186 | 2 | 0 | 0 | 0 | 0 | 0 |
| 30 | fast-forward | 557 | 5 | 0 | 0 | 0 | 0 | 0 |

One `estimate_error` per run comes from the first request, sized on the seed ratio before any usage.
The proxy rotates at about every second turn start: 534 of 1,097 fast-forwarded turn starts, reason
`bound` 370 times, `min_turn_room` 160 times and `window_minimal` 4 times. F stays between 69,565 and 70,031 and W between
42,469 and 55,435.

### Rotation request cost (fast-forward, engine only, `results/ff_first-1.csv`, medians)

| History | Engine, rotating turn start | Engine, other turn start | index_text per rotation (2 calls) | of which reading pages | page_out |
|---|---|---|---|---|---|
| 2M | 0.131 | 0.023 | 0.102 | 0.009 | 0.003 |
| 5M | 0.178 | 0.055 | 0.114 | 0.023 | 0.004 |
| 10M | 0.268 | 0.108 | 0.149 | 0.049 | 0.004 |
| 30M | 0.606 | 0.331 | 0.254 | 0.152 | 0.004 |

`index_text` is called twice per rotation (`fixed_overhead` and `new_epoch`). Each call re-reads and
parses every page file through `SessionStore._pages`, so the page-read share grows linearly with page
count. The rest is rendering, mostly `salient_identifiers` over the pages whose lines fit the 4,000-token
budget.

### `mixed` profile (later-turn reminders at an assumed rate; measured on 4.x ids in `../reminders/`)

From `results/*_mixed-1.csv` (stopped at its first failure) and `results/*_mixed-keep.csv` (`--keep-going`
to 10M):

| History (M) | Max upstream tokens (HTTP) | Largest last request of a fast-forwarded turn | window_minimal | bound_too_small | over_bound | turn_room_low |
|---|---|---|---|---|---|---|
| 5 | 123,462 | 142,567 | 57 | 1 | 0 | 1 |
| 7.5 | 145,663 | 143,011 | 145 | 25 | 0 | 38 |
| 10 | 159,255 | 161,667 (40 turns over) | 236 | 112 | 14 | 129 |

F grew from 62,392 at the first rotation (turn 3) to 100,810 at turn 181. The growth is all carried
context blocks: `carried_over_budget` fired at every rotation after the first, from turn 5 on. Timings match the `first` profile at
equal size: 0.100 s for a tool loop at 5M and 0.210 s at 10M.

### Memory (`results/leak_5M.csv`, `results/leak_30M.csv`)

One proxy process, with `gc.collect()` and a `ps` reading after every request:

| History | Requests | RSS after request 1 | RSS after the last | Peak RSS | Live Python objects, first to last |
|---|---|---|---|---|---|
| 5M (9.2 MB bodies) | 51 | 84 MB | 669 MB (flat from request 26: +1 MB per request) | 720 MB | 22,889 to 23,138 |
| 30M (55 MB bodies) | 79 | 283 MB | 3,035 MB (between 2,114 and 3,126 from request 30 on) | 3,450 MB | 22,925 to 23,260 |

Live objects stay flat, so nothing in the proxy or engine keeps requests. RSS grows anyway: memory freed
after each request is not returned to the OS. [Inference] The working set of one 30M request is the
55 MB body, two parsed copies of it (`request` and `original`), the 55 MB `before` serialization, and
the engine's per-message canonical JSON. RSS levels off at 50 to 70 times the body size. From the
measured trend, a long session at 30M needs 3 to 3.5 GB for the proxy, and about 6 GB at 60M.

## Where the cost goes (cProfile of `_transform` + `observe_usage` at 10M, `results/profile_first-1_10M.txt`)

10 requests (2 turns, one rotation), 2.72 s under the profiler. Shares are each function's cumulative
time over that total. They overlap where one function calls another.

| Cost centre | Cumulative s | Share | Where |
|---|---|---|---|
| `message_hash` (normalise + canonical JSON + SHA-256 of every message), 48,040 calls | 1.272 | 47% | rewrite.py:256 |
| of which `matching_prefix`, the first pass over all messages | 0.638 | 23% | rewrite.py:709-715 |
| of which `send`'s `hashes=tuple(message_hash(m) for m in messages)`, the second pass | 0.639 | 23% | rewrite.py:973 |
| of which canonical `json.dumps(sort_keys=True)` alone | 0.929 | 34% | canonical.py:13 |
| `serialize_request` twice per request (`before` and `after`) | 0.440 | 16% | proxy.py:820, 849 |
| `json.loads` (the body, then the `original` copy) | 0.376 | 14% | proxy.py:809, 821 |
| `is_turn_start` scans of the whole history, 290,564 calls (`conversation_key`, `at_turn_start` x3, `_main_after`, `turn_id`, `carried_content`) | 0.306 | 11% | rewrite.py:213 |
| `index_text` (1 rotation, 2 calls) | 0.227 | 8% | store/__init__.py:652 |
| of which `salient_identifiers` | 0.169 | 6% | store/recall.py:149 |

By own time, `json` encoding (1.304 s) and decoding (0.352 s) are 61 percent of the total; SHA-256 is
only 0.112 s. Everything above is linear in history length, which matches the near-linear growth of the
timing table: about 0.019 s per million tokens for a tool loop.

## Before and after the per-request CPU fixes (branch claude/proxy-cpu)

Fixes 2, 4 and most of 6 below are applied in `src/`; the output is unchanged byte for byte.

- **One hash per message per request.** `RewriteEngine` calls, `rewrite` and `observe_usage` run inside
  `request_scope` (rewrite.py), which memoises each message object's rule 10 hash, so
  `matching_prefix`, `send`'s epoch hashes, `usage_identity`, the conversation key and the resume
  anchor share one computation. The memo holds the message object, so an id cannot be reused while it
  lives. A tool-loop request at 52 messages now hashes 52 times instead of 104.
- **No normalising copy for most messages.** For the Messages API, a message with no `cache_control`
  key and no single text block to unwrap normalises to an equal copy, so its hash is the SHA-256 of its
  own canonical JSON. The fast path is taken only when the quoted name `"cache_control"` does not occur
  in that JSON. A key would always show up there, so a false match only sends the message to the slow
  path.
- **No defensive copy and no reference serialization in the proxy.** A transform with a true
  `preserves_input` attribute (the engine and `identity_transform`; the harness timer forwards it)
  gets the parsed request, and that same object goes to `observe_usage`. The proxy no longer
  serializes the 55 MB request (`before`) or parses it a second time (`original`). The
  unchanged-request check is now `same_wire(new, request)`. It is exactly `serialize_request(new) ==
  serialize_request(request)`, decided by walking both: identical objects are equal, values of
  different JSON types never are, and floats compare by `repr`. Anything outside the JSON types, or
  nesting too deep to walk, falls back to serializing both. The engine's output shares `system`,
  `tools` and the message dicts with its input, so the walk is mostly identity checks. A transform
  without the attribute keeps the copy-and-compare path.
- **One turn-start scan per request.** `turn_starts`, `at_turn_start`, `turn_id`, `_main_after` and
  `retained_entries` share one scan of the request's own message list (`request_scope` caches it by
  list identity and length). `carried_content`'s scan of `M[:k]` is not cached; it sits in code another
  branch is changing. Turn-start checks per tool-loop request at 52 messages: 468 before, 107 after.
- **Pathological change.** A body that parses but cannot be re-serialized (nesting between the decoder's
  and the encoder's recursion limits) used to be skipped with `transform_skipped`. With a preserving
  transform it now reaches the engine and fails as `transform_error`. The CLI's bytes are forwarded
  either way. On Python 3.14.6 both the decoder and the encoder handle 100,000 levels, so this case
  was not reached here.
- **Deep nesting, the other way.** A message nested deeper than the recursion limit, with no
  `cache_control`, used to raise `RecursionError` in normalisation (`transform_error`); the fast hash
  path does not normalise, so it now hashes and goes through (reviewer-2 on #205, checked at 5,000 and
  100,000 levels). Neither case comes from Claude Code.

### Equivalence

- **Every scale run.** The non-timing columns of `requests_*.csv` and `ff_*.csv` match row for row
  between base and after, in all three run pairs: 150 HTTP requests and 1,097 fast-forwarded turn
  starts each. That covers upstream bytes, tokens and message counts, rotation and its reason, k,
  epoch, F, W, and events, through 30M. Event counts and pages per checkpoint match too.
- **In the suite.** `tests/test_proxy_cpu.py::test_old_and_new_paths_send_identical_bytes_and_reach_identical_state`
  drives a synthetic session through the real proxy and a local upstream twice, once on the old paths
  (no memo, normalise-then-hash, the copying transform step) and once on the new. The session is 18
  turns: tool loops with signed thinking, 17 rotations, a paged-history resync, count_tokens, moving
  `cache_control`, string and single-block prompts, and a proxy restart that resumes the epoch. The test
  compares every upstream body, every log entry (clock and port removed), the engine state, the
  estimator, the epoch record, the pages, and the published window, room and retained snapshot.
- **Against the base commit.** The same driver run against `a15770f`'s `src/` gives identical digests
  for 18 turns (42 bodies, 17 rotations) and 40 turns (92 bodies, 39 rotations).
- **No aliasing.** A test checks that the engine leaves the request unchanged across a 16-turn session,
  through the transform and `observe_usage`. Further tests check the hash fast path against
  normalise-then-hash on 23 message shapes under both dialects, and `same_wire` against serialized
  equality on 28 pairs (`1` and `1.0`, `True` and `1`, `0.0` and `-0.0`, key order, tuples, non-string
  keys, sets, surrogates).

### Numbers (`first` profile, 3 runs each, `results/*_base-{1,2,3}.*` and `results/*_perf-{1,2,3}.*`)

Base is `a15770f` (this branch's parent), measured on 2026-10-18 just before the change, on the same
machine. 1-minute load was 1.9 to 4.1 during the base runs and 2.3 to 6.5 during the after runs.
Seconds; median of the three runs' medians:

| History | Tool loop e2e | Proxy transform, tool loop | Engine, tool loop | Turn start e2e | Rotating turn start e2e | Peak RSS MB | Fast-forward 10M to 30M |
|---|---|---|---|---|---|---|---|
| 10M, base | 0.209 | 0.202 | 0.118 | 0.295 | 0.382 | 1,337 | |
| 10M, after | 0.090 (-57%) | 0.083 | 0.064 | 0.174 (-41%) | 0.260 | 756 | |
| 30M, base | 0.610 | 0.594 | 0.347 | 0.906 | 0.911 | 1,707 | 331 to 344 s |
| 30M, after | 0.260 (-57%) | 0.245 | 0.185 | 0.555 (-39%) | 0.563 | 1,573 | 247 to 256 s |

Per-run medians of the 30M tool loop were 0.583 to 0.616 s before and 0.244 to 0.264 s after. The
cost is still linear in history, at about 0.0085 s per million tokens per tool loop instead of 0.020.

cProfile of `_transform` + `observe_usage` at 10M (`results/profile_{base,perf}-*_10M.txt`, the same
10 requests and one rotation):

| | Base | After |
|---|---|---|
| Total under the profiler (3 runs) | 2.80 to 2.90 s | 1.23 to 1.33 s |
| Function calls | 7,100,362 | 3,036,238 |
| Hashes computed (`message_hash` work) | 48,040, 1.311 s | 24,030, 0.654 s |
| `json.loads` | 0.396 s (the body, then the copy) | 0.224 s (the body) |
| `serialize_request` | 0.457 s (20 calls, the ten `before` ones 18 MB each) | under 0.05 s, not in the top 40 (upstream bodies of 0.1 to 0.3 MB only) |
| Turn-start checks | 290,564, 0.306 s | 81,810, 0.091 s (mostly `carried_content`) |
| `index_text` (not touched here) | 0.231 s | 0.237 s |

What is left per request: one canonical JSON encoding and SHA-256 per message (0.48 s of encoding
over the 10 profiled requests), one parse of the body, and `carried_content`'s scan of the paged
region. Fix 3 (hash only from the first byte that differs from the previous request) would remove most
of the first; `carried_content` could be cached per epoch.

## Static analysis checked

| Claim | Verified |
|---|---|
| Uncompressed bodies are read with no size cap | yes: `_read_body` (proxy.py:695-711). `MAX_DECODED_BODY_BYTES` (64 MiB) applies only to encoded bodies (proxy.py:792). [Inference] An encoded body (Codex sends zstd) would be refused with 415 above 64 MiB decoded, about 36.7M tokens at 1.83 bytes per token; the Claude launcher disables compression, so Claude bodies are uncapped |
| Body parsed, serialized, parsed again for a copy | yes: proxy.py:809, 820, 821; then serialized again at 849 |
| Every message normalised and hashed twice | yes: rewrite.py:709-715 and 973, plus `conversation_key` (first message) and `usage_identity` (last message) on both the transform and `observe_usage` |
| The paged region is scanned for carried content on every request | yes: `transform_messages` calls `carried_content(messages, state.k)` (rewrite.py:560) |
| One global transform lock | yes: proxy.py:826 for the transform and 1071 for `observe_usage`. [Inference, not measured] At 30M a main-conversation request holds it for about 0.33 s, during which a subagent request waits |
| `index_text` re-reads every page twice per rotation | yes: `fixed_overhead` (rewrite.py:617) and `new_epoch` (649-650), each through `_pages()` |
| Carried `<system-reminder>` blocks are uncapped | yes: `carried_content` (rewrite.py:493) keeps every context block of every paged turn start; `page_carried_text` (671-701) pages only role `system` text messages; `carried_over_budget` (663-667) only reports. This is the `mixed` failure above |

## Obvious fixes (as found at 4525388; fix 1 was applied as D32 in #203, and 2, 4 and most of 6 in #205)

1. **Page old carried context blocks** the way `page_carried_text` pages system messages: keep the first
   message's blocks and the latest of each recognisable kind, and page the rest once growth passes
   `carried_text_budget`. [Inference] This removes the `mixed` failure, F stays near its first-request
   size, and the same session would behave like the `first` profile to 30M. Applied as D32 on
   2026-10-18 and measured: the `mixed` run to 30M (`results/*_mixed-carried-bound.csv`, against
   `*_mixed-before.csv` on the code before) had 0 `bound_too_small` and 0 `over_bound` (871 and 692
   before), F between 62,392 and 75,820, and largest measured request 134,203 tokens; details in
   `../reminders/README.md`, "After D32 and D33".
2. **Hash each message once per request.** `send` can reuse the hashes `matching_prefix` computed and hash
   only the messages after the match. Expected: 23 percent of transform time, about 0.13 s per request
   at 30M.
3. **Skip the prefix re-hash.** Compare the raw body with the previous request's: the CLI resends a
   byte-identical prefix except where `cache_control` moved. Hash only from the first differing message
   on, so the per-request hash cost no longer grows with history. Expected [inference]: up to the whole
   47 percent `message_hash` share. Together with fix 2, a 30M tool loop would take about 0.3 s instead
   of 0.58 s.
4. **Drop the `original` copy and the `before` serialization** (proxy.py:820-821). The engine is
   documented never to mutate its input. The unchanged-request check can compare `after` with the raw
   body only when the transform returned the same object. Expected: about 15 percent of transform time,
   plus one parsed copy of the history less in memory per request.
5. **Cache page stubs and index lines in memory.** Pages are immutable, so `_pages` need not re-read the
   whole store and re-scan salient identifiers twice per rotation. Expected: index_text from 0.254 s to
   near 0 per rotation at 30M, and the gain grows with page count.
6. **Compute `turn_starts` once per request** and pass it to `conversation_key`, `at_turn_start`,
   `_main_after`, `turn_id`, and the carried scan. Cache `carried_content` per epoch, since it changes only
   at a rotation. Expected: about 11 percent.

## Files

- `run.py`: the one-command harness. `worker.py`: the proxy process and the fast-forward. `synth.py`: the
  synthetic session. `upstream.py`: the fake upstream. `summarize.py`: the tables above. `fidelity.py`:
  fast-forward against HTTP. `leak.py`: the memory probe.
- `results/requests_<label>.csv`: one row per HTTP request, with sizes, timings, store-call timings,
  rotation, events, load average, and RSS.
- `results/checkpoints_<label>.csv`: one row per checkpoint, with how it was reached, pages, store size,
  RSS, event counts, and `uptime`.
- `results/ff_<label>.csv`: one row per fast-forwarded turn start, with engine and store timings, k,
  epoch, F, W, events, and the turn's last-request size.
- `results/profile_first-{1,2,3}_10M.txt`, `results/fidelity_*_120.json`, `results/leak_*.csv`.
- `results/*_base-{1,2,3}.*` and `results/*_perf-{1,2,3}.*`: the before and after runs of the CPU fixes.
- `tests/test_scale_proxy_harness.py`: fast checks of the helpers (7 tests, under a second, collected
  by the default `pytest`). The scale run itself is not in the suite. [Note added 2026-11-12] A
  200-turn run is in the suite now, with two more test files; see the next section. [Note added
  2026-11-13] This file has 13 tests now: the driver's kept-open connections, the fake upstream
  closing first, and a failing short run's report were added.

## 2026-11-12: repaired at v0.5.24, and a 200-turn run in the test suite

Main at b0e2dbc (v0.5.24), Python 3.13.14, the same Apple M5 Pro (18 cores, 48 GB). Nothing here
talked to a model API. Other agents ran test suites on the machine the whole time, so every timing
below is given with the 1-minute load average it was taken at. A load above 18 means more runnable
processes than cores.

### What was broken

`run.py` stopped at its first proxy start with `AttributeError: 'SessionStore' object has no
attribute '_pages'`. No test ran the harness, so nothing reported it. With that fixed, more had
drifted from the package since 2026-10-18. Each item says what the harness does now.

1. **`SessionStore._pages` is gone.** The store was split into `src/contexto/store/*.py`, and
   `index_text` now reads pages through a per-process `PageCatalog` (D34): `entries()` lists
   `pages/` and reads only the files this process has not seen. The timer moved to
   `PageCatalog.entries` and kept its key, so `pages_read_s` (`requests_*.csv`) and `store_pages_s`
   (`ff_*.csv`) still mean the time `index_text` spent finding and reading pages. Two new columns,
   `page_file_reads` and `store_page_reads_n`, count the page files read (`PageCatalog._read`).
2. **The engine could not be pickled between phases.** The catalog holds a lock
   (`TypeError: cannot pickle '_thread.lock' object`). The harness drops the catalog before it
   pickles the engine. Each phase's process then reads every page once, at its first rotation, as a
   restarted proxy does; that read is inside the first rotating turn start measured at a checkpoint.
3. **The proxy was built by hand**, as a copy of `launcher.start_proxy` from October. That function
   has since gained the `SessionRecorder` around the engine (D55), the `X-Session-Affinity` key for
   an upstream that is not a vendor host (D36), and the clearing of the MCP-listed record. The
   harness now calls `launcher.start_proxy` itself. It replaces `launcher.build_engine` for the
   length of that call, so the timed engine goes in, and it stops the proxy the way `launcher._run`
   does (`report_missing_tools`, then `SessionRecorder.flush`).
4. **The timer around the engine hid arguments from the proxy.** The proxy passes `tool_calls` and
   `tool_call_ids` only to a usage callback whose signature takes them (D40), and calls the
   transform's `prime` when it has one (D60). The October wrapper had neither, so the engine was
   never told whether a response held a tool call. `worker.TimedEngine` passes both through.
5. **Two break events were never counted.** `page_conflict` and `observe_usage_error` were in
   `BREAK_EVENTS` but not in `COUNTED_EVENTS`, so a run could not stop for them. They are counted
   now, and eleven events the package has gained since stop a run too: `request_refused`,
   `request_too_deep`, `hold_skipped`, `page_unreadable`, `proxy_stop_incomplete`,
   `session_start_unrecognised`, `unclassified_system_message`, `cli_version_untested`,
   `mcp_tools_missing`, `toolcheck_error` and `cli_version_error`. `cli_version`, `epoch_not_resumed`
   and `epoch_resumed` are counted and do not stop a run. New `n_<event>` columns are appended to
   `checkpoints_*.csv`; the columns of the 2026-10-17 files keep their places.
6. **The engine no longer took the synthetic session for Claude Code's.** The first run after the
   fix logged `session_start_unrecognised`. Its tool list also held none of Contexto's tools
   (`toolcheck.anthropic_tools_offered` returns False for it), which the engine reports as
   `mcp_tools_missing` once a session is 30 seconds old. `synth.py` and the driver now send what
   the engine reads:
   - the session-start message begins with `# Environment` (the D33 signature) and is still 8,470
     characters;
   - `User-Agent: claude-cli/2.1.318 (external, cli)`, the newest version in
     `cliversions.TESTED_VERSIONS` (it was `contexto-scale-harness`);
   - one Claude Code session id, in `X-Claude-Code-Session-Id` and in `metadata.user_id` as a JSON
     string, which is how 2.1.314 and later send it;
   - five of the 40 tools are Contexto's MCP tools by name (`mcp__contexto__contexto_recall` and
     the other four).

   The head's `system` and `tools` are 49,909 tokens with these names (49,885 before), and
   `metadata.user_id` grew from 31 to 104 characters, so every request is about 70 tokens larger:
   the whole head (a request with no messages) is 50,056 tokens by `synth.tokens_of`, 49,985
   before. Turn 0 is 17 bytes longer; every later turn's messages are byte for byte the same, but
   each request carries the head, so F and the engine's rotation decisions move (see "The full run
   at v0.5.24").
7. **Fast-forward** passes `tool_calls` to `observe_usage`, as the proxy does, and reports the
   output tokens of the reply the fake upstream would send (420 for a turn with no tool call; it
   was 180 for every turn). [Added 2026-11-13] It also passes the driver's `User-Agent` and
   `X-Claude-Code-Session-Id` to the engine (`synth.CLI_HEADERS`); it passed none, so its session
   logged no `cli_version` event where the HTTP path logs one. The rotations are the same either
   way (Reviewer A checked).

Harness-only changes came with the short run: the fake upstream records when it wrote a request's
last event (`t_done`) and listens with a backlog of 256, and `requests_*.csv` has more columns,
`upstream_s`, `added_s`, `page_file_reads` and `proxy_cpu_s` (defined under "The short run").
[Added 2026-11-13] The driver keeps its connections open and the fake upstream closes each one
after its reply (see "Connections").

Left as it was: the `mixed` profile's reminder texts ("The TodoWrite tool hasn't been used
recently", "... was modified, either by the user or by a linter") are not among the kinds the engine
recognises since D32 (`rewrite.CONTEXT_BLOCK_KINDS` has "The task tools haven't been used recently"
and "Note: ... changed on disk"). The fallback may therefore page every one of them, where a real
4.x session keeps the latest block of each kind. The `mixed` runs of the record used these texts,
so the profile still means what the record measured.

### So it fails loudly next time

`tests/test_scale_proxy_drift.py` (10 tests, about two seconds) fails when:

- a name in `worker.INSTRUMENTED` is not an attribute of the class it is listed under (the store
  methods, `SessionStore._catalog`, the catalog's `entries` and `_read`, the handler's `_read_body`
  and `_transform`, and the engine attributes the timer forwards);
- a timer key the driver reads into a CSV column is not one the worker writes, or a page written
  and indexed through an instrumented store does not reach the timers behind those columns;
- an event name in the harness's lists is no longer written by `src/contexto/`, or a break event is
  not counted. "Written" means the name is the value of an `"event"` key or `event=` keyword, as a
  literal, a conditional of literals, or a variable the same function assigns one to (parsed with
  `ast`); a name that is only compared or mentioned does not count;
- `launcher.start_proxy`, `build_engine`, `open_session`, `NoticeLog` or
  `RewriteEngine.observe_usage` no longer take what the harness passes, or a field `fidelity.py`
  compares is not in the epoch record;
- the proxy started by the harness is not the launcher's (no `SessionRecorder`, no affinity key),
  or the timed engine hides `preserves_input`, `prime`, `tool_calls` or `tool_call_ids`;
- the engine does not take the synthetic first request for Claude Code's: the session-start
  signature, the tested CLI version, the session id, and Contexto's tool names, each checked with
  the package's own function;
- `cputime.py` can no longer drive the handler's `_transform` and the engine without a server, or
  stops passing `observe_usage` the reply's output tokens and `tool_calls`;
- fast-forward does not pass the driver's headers to the engine (its session must log `cli_version`,
  as the HTTP path's does).

[Changed 2026-11-13.] The event check looked for the quoted name anywhere in the package, so
Reviewer B's mutation, `store_failure` always returning `store_error`, passed it while
`"page_conflict"` stayed in a comparison. With the parsed check that mutation fails the test (run
in a scratch copy: the old test passed 8 of 8, the new one failed), and a test applies the same
mutation to a small package of its own. Every assertion now says which part of the harness to
update.

The short run below also fails on any `BREAK_EVENTS` event, so a signature the drift test does not
name still stops the suite when the engine reports it.

### The short run

```
uv run --with pytest pytest spikes/m2/scale/proxy/tests      # the tests; numbers at the end of the output
.venv/bin/python spikes/m2/scale/proxy/short.py --repeat 5   # the numbers alone, five times, with the spread
```

`short.py` sends 200 turns of the `mixed` session (20,000-token head, 3,000-token turns, bound
150,000) through one proxy process and the fake upstream, every request over HTTP: 634 requests
and 1,269 messages, ending at 913,084 tokens of history in a 1.67 MB body, six times the bound.
Before that it starts three proxies on empty homes, sends the session's first request (50,838
bytes) 20 times to the last of them and 20 times straight to the fake upstream, and then 200 times
from 100 threads at once. It needs neither `claude` nor `codex`. `tests/test_scale_proxy_short.py`
runs it once per test session and `tests/conftest.py` prints its numbers at the end of the pytest
output whether the tests pass or fail, so every CI log has them.

What the run found, the same in every measured run on this machine and in CI (Ubuntu and macOS):
all 634 responses were 200; the largest upstream request was 117,423 tokens; no event of `run.BREAK_EVENTS` (which include
`over_bound`, `bound_too_small` and `transform_error`); 20 rotations, 267 pages, 19 `carried_paged`,
3 `carried_over_budget` and the first request's one `estimate_error`.

The quantities with a budget, as `short.py` defines them:

- **Added time per request** is the driver's time from sending a request to its last byte, less
  the time the fake upstream spent on it (`added_s`). It covers the body's way to the proxy, the
  proxy's read, parse, transform and store writes, its connection to the upstream, and the relay.
  The clock starts after the TCP connection is open. That change came after the first CI run and
  the two loops of the flake check, whose times include the connect, as `e2e_s` does in the
  2026-10-17 files. Since 2026-11-13 the driver also keeps its connection open between requests
  (see "Connections"). On Linux that adds a wait of about 41 ms to every request, which is the
  proxy's, not the harness's (see "The 40 ms wait on Linux"); on macOS there is none.
- **Growth with history** is the slope of added time against the history the CLI sent, over the
  434 tool-loop requests, as the median of the slopes between every pair of them (Theil-Sen).
- **Startup** is from starting the proxy process to its `ready` line: the interpreter, the imports,
  opening the session, building the engine and binding the port. The smallest of three starts.
- **Peak RSS** is read by the proxy process itself: `ru_maxrss` on macOS, `VmHWM` on Linux. On
  Linux `ru_maxrss` is carried across exec, and the first CI run reported the pytest process's
  1,600 MB for a proxy whose RSS was 36 MB.
- **RSS growth per MB of body growth** and **live objects gained per turn** compare the proxy after
  turn 20 and after turn 200 (`ps` RSS, and `len(gc.get_objects())` after `gc.collect()`).
- **Throughput** is 200 copies of the first request from 100 threads, in requests a second.

Measured on 2026-11-12. "CI" is this branch's first CI run: four Ubuntu runners (4 CPUs, Python
3.12 to 3.15, loads 0.6 to 1.0) and two macOS runners (3 CPUs, Python 3.12 and 3.15, loads 1.7 to
2.3). "This machine" is the 39 measured runs of the flake check below: 18 of a first loop of 20
(two errored before measuring), the 20 of a second loop, and one run with `yes > /dev/null` on each
of the 18 cores, at loads of 25 to 84.

| Quantity | CI, Ubuntu (4 runs) | CI, macOS (2 runs) | This machine (39 runs) | Budget | Budget over the slowest CI run |
|---|---|---|---|---|---|
| Added time per request, median | 17.1 to 26.2 ms | 19.6 and 50.9 ms | 31.7 to 79.2 ms | 200 ms | 3.9 |
| Added time per request, p95 | 28.2 to 41.9 ms | 41.5 and 109.8 ms | 50.3 to 388.2 ms | 800 ms | 7.3 |
| Growth of added time per million tokens | 25.8 to 36.5 ms | 28.1 and 14.7 ms | 20.4 to 164.6 ms | 300 ms | 8.2 |
| Startup, best of 3 | 77 to 101 ms | 113 and 423 ms | 133 to 1,049 ms | 3,000 ms | 7.1 |
| Peak RSS | 52.0 to 86.8 MB (second CI run; the first read it wrongly, see above) | 66.0 and 55.3 MB | 51.6 to 62.8 MB | 300 MB | 3.5 |
| RSS growth per MB of body growth | 3.5 to 9.8 MB | 17.9 and 9.6 MB | 6.4 to 13.9 MB | 150 MB | 8.4 |
| Live objects gained per turn | 7.43 to 8.91 | 7.27 and 8.91 | 7.11 to 7.59 | 40 | 4.5 |
| Throughput, 100 concurrent | 148 to 226 req/s | 150 and 50 req/s | 15 to 125 req/s | floor of 5 req/s | 10 (below) |
| Duration of the run | 14.5 to 21.0 s | 18.8 and 45.0 s | 25.7 to 102.8 s | none | |

The second CI run's four Ubuntu jobs: median 15.4 to 25.0 ms, p95 26.5 to 79.4 ms, growth 21.2 to
36.7 ms per million tokens, startup 64 to 101 ms, 151 to 207 req/s, 12.9 to 24.0 s.

The second macOS runner of the first run was slow in every fixed cost and fast per byte: its
growth per million tokens was the lowest of the six (14.7 ms) and its added time for the first request the highest
(18.1 ms over a direct send, against 3.8 to 5.7 ms on the other five). The proxy writes and
fsyncs three or four small files per request (`set_retained`, `set_turn_room` twice, and
`set_epoch` at a turn start), so [inference] that runner's disk was slow. The run took 45 s there
and 14.5 to 21 s on the other five. [Inference] With `short.TURNS = 150` it would take about 34 s
on such a runner.

What the budgets catch, and what they do not:

[Rewritten 2026-11-13: the first version of this list said a 4 to 8 times slowdown and a quadratic
pass "at Python speed" were caught. Reviewer B measured otherwise.] What follows was measured by
Reviewer B at e14f6630 by injecting each change into the package of a scratch copy and running the
test on this machine (loads 7 to 10), and from the CI runs' values. The budgets were not tightened
to catch more: a required check that fails on a slow runner would be worse.

- **A linear slowdown** is caught only when it is large. Here, the handler spinning until its CPU
  time for a request was 2, 4, 8, 12 and 16 times what it was passed every budget (16 times:
  median 170 ms against 200, growth 265 against 300). From the CI values of 2026-11-12 the
  threshold was about 8.5 to 12 times on the median and 9 to 13 times on growth on an Ubuntu
  runner, about 15 times on this machine, and near 4 times only on the slow macOS runner. Since
  the driver keeps its connections open, every request on an Ubuntu runner also carries the
  proxy's 41 ms wait (see "The 40 ms wait on Linux"). In CI run 43552340153 (2026-11-14) the
  median there was 61.8 to 65.7 ms, 3.0 to 3.2 times under the budget, of which about 20 to 24
  ms is the rest of the proxy's work. [Inference] A slowdown of that part is now caught on the
  median from about 6.5 to 8 times, since the wait does not grow with it. Growth, a slope, does
  not include the wait: 28.2 to 37.4 ms per million tokens there, 8.0 to 10.7 times under the
  budget. So the test catches a slowdown from about 7 to 10 times on a typical CI runner.
- **A quadratic pass over history** is caught when a step costs about 200 ns: the session ends at
  1,269 messages, 1.6 million pairs per request, and a dict `.get` comparison done 8 times per pair
  gave growth 402 ms per million tokens (budget 300). A cheap pass is not caught: a pair loop with
  `a is b` (5 ns a step), one `.get` comparison per pair (26 ns; growth 65) and a list membership
  test per message all passed. The full run at 30M tokens, 28 times as many messages, is the check
  for those.
- **A leak** of Python objects or of request bytes is caught: keeping each request's parsed
  messages gave 5,203 objects per turn (budget 40), keeping its bytes a 622 MB peak (budget 300).
  A leak under about 350 KB per request that is not made of Python objects passes.
- **A slow start** is caught from 3 seconds: a 3-second sleep in `start_proxy` failed (3,119 ms),
  a 2.5-second one passed.

Not caught, then: a slowdown of less than about 7 times on a typical runner (a doubling, or a
regression of 50 percent, passes with room to spare), a cheap quadratic pass, a small leak outside
Python objects, and anything that only shows after more than 200 turns (0.9M tokens of history).

**Throughput is asserted only as a floor of 5 requests a second**, with all 200 responses 200. It
spread from 15 to 226 requests a second over these runs, a factor of 15, and the 100 client
threads, the fake upstream and the timer share one Python process, so the number is not the
proxy's alone. A budget near the measured values would fail on a busy runner without the proxy
having changed.

#### Flake check

The test ran 60 times on this machine with `uv run --with pytest==9.1.1 pytest
spikes/m2/scale/proxy/tests/test_scale_proxy_short.py`, in three loops of 20, while other agents'
test suites kept the 1-minute load between 20 and 84 on 18 cores.

- First loop, with tighter latency budgets (median 100 ms, p95 300 ms, growth 120 ms per million
  tokens): 16 passed. 2 failed on latency while the load passed 80 (p95 388 and 356 ms, growth 165
  ms). 2 errored in the fixture before measuring, after 18 s and 3 s; their output was not kept,
  so the cause is not known. This session ran nothing else at the time.
- The latency budgets were then loosened to the values in the table. All 18 measured runs of the
  first loop meet them.
- Second loop, with the budgets in the table and failing runs' output kept: 20 of 20 passed, at
  loads of 43 to 84. The two errors did not recur.
- One run with `yes > /dev/null` on each of the 18 cores, on top of a load of 48: passed in 50 s
  (median 43.5 ms, p95 187 ms, growth 65.5 ms per million tokens, startup 583 ms, 78 req/s).

- Third loop, on the final code, with this branch's whole test suite running beside it: 20 of 20
  passed, at loads of 20 to 68 (median 36.1 to 57.4 ms, p95 92.7 to 196.8 ms, growth 53.7 to 90.5
  ms per million tokens, startup 94 to 553 ms, peak RSS 50.9 to 59.9 MB, 54 to 179 req/s, 31 to
  53 s).

Between the second loop and the third, the driver was changed to open its connection before
starting the clock and to retry a failed connect up to three times. The cause of the two errors
was found later by Reviewer B: local ports ran out (see "Connections" below). A proxy process that
exits now puts the end of its log in the error, and a run that raises prints the step it was in
and what it had measured.

On the final code of fix round 1 (2026-11-13) the three test files ran 20 times in a row: 20 of
20 passed, each in 12.4 to 14.1 s, at loads of 5.7 to 8.0 (median 11.0 to 15.0 ms, p95 17.3 to
22.5 ms, growth 15.1 to 19.5 ms per million tokens, startup 49.6 to 70.3 ms, peak RSS 51.0 to 56.9
MB, 273 to 444 req/s).

The spread of the first 39 measured runs (smallest, median, largest), from `short.py --spread`:
added time median 31.7, 43.3, 79.2 ms; p95 50.3, 127.3, 388.2 ms; growth 20.4, 55.5, 164.6 ms per
million tokens; startup 133, 336, 1,049 ms; peak RSS 51.6, 55.5, 62.8 MB; RSS growth 6.4, 9.9,
13.9 MB per MB; objects per turn 7.11, 7.43, 7.59; throughput 15, 82, 125 req/s; duration 25.7,
42.4, 102.8 s.

#### Connections (2026-11-13)

Reviewer B found that the short test failed with `OSError: [Errno 49] Can't assign requested
address` (EADDRNOTAVAIL) when several copies overlapped. Six copies started within 30 seconds of six
others: in the second round 4 errored in the fixture and 2 failed with non-200 responses (the
proxy's own connect to the fake upstream failing). Twelve at once: 11 errored and 1 failed. That is
the likely cause of the two fixture errors in the first loop of the flake check.

The cause: the side of a TCP connection that closes first holds it in TIME_WAIT, on macOS for 30
seconds (`net.inet.tcp.msl` is 15,000 ms), and a TIME_WAIT on a local port keeps that port from new
connections. This machine has 16,384 of them (49152 to 65535). The driver opened a connection per
request and closed it, and the proxy opens a new upstream connection per request and closes it
after the reply. Counted with `netstat -an -p tcp` right after one run, on the run's own ports (the
machine as a whole had 2,900 to 6,300 TIME_WAIT sockets from other sessions throughout):

| | Driver to proxy | Proxy (and direct sends) to fake upstream | Local ports held |
|---|---|---|---|
| Before (e14f6630) | 854 | 874 | 1,728 |
| After, two runs | 102 | 874 on the upstream's listening port; 1 and 4 of them also on a local port (both sides closed at once) | 103 and 106 |

The "after" runs came before the 104-byte sends were added, which add one driver connection and 40
upstream connections to a run.

What changed, in the harness only:

- The driver keeps one HTTP/1.1 connection per client open between requests (`run.Client`), as
  Claude Code does: one for the session, one each for the first-request and 104-byte sends (two
  for the 104-byte sends since 2026-11-14; see "The 40 ms wait on Linux" below), one per client of
  the burst. A connection is opened before the request's clock starts, and a connect that fails
  with EADDRNOTAVAIL is retried with a growing pause. The wait is 30 seconds of wall-clock time in
  all for the run (`run.PATIENCE`): waits that overlap, as the burst's clients' do, count once, and
  waits one after another add up. Until 2026-11-14 each connect had 30 seconds of its own, so a run
  on a machine whose ports stayed used up took about 22 minutes to fail; for a while after that the
  30 seconds were counted in thread-seconds, so 100 burst clients spent them in under half a second.
- The fake upstream answers with `Connection: close` and closes first, so the TIME_WAIT of each of
  the proxy's upstream connections is on the upstream's listening port and holds no local port. The
  proxy reads each reply to its last chunk before either side closes, so what it does is unchanged.
- The proxy itself was not changed. **Finding:** it opens a new upstream connection per request by
  design (`contexto/proxy.py`: "a new connection per request: nothing is pooled or reused"). A
  short run makes 934 of them (634 for the session, 200 for the burst, 100 for the first-request
  and 104-byte sends; 914 before the second set of 104-byte sends), and the full run one per
  request it forwards. At the rate of a real session, a few requests a minute, the ports this holds
  are negligible. The cost that matters is elsewhere: `ContextoProxy.connection()` builds a new
  `_UpstreamHTTPS` with `ssl.create_default_context()` for every request, so each request to a
  real HTTPS upstream also pays a TCP connect, a TLS handshake (at least two round trips to the
  host) and the building of a context with the CA store loaded (2 to 3 ms on this machine: over
  20 calls, Reviewer A measured 2.4 ms minimum and 2.7 ms median, and I measured 2.1 and 2.2 ms on
  2026-11-14 at a load of about 5). Claude Code without Contexto keeps its
  upstream connection open. The fake upstream is plain HTTP on 127.0.0.1, so the harness cannot
  measure these costs, and none of its added-time figures include them. What they cost a session
  was not measured.
- `test_the_driver_keeps_its_connections_open` checks the counts on every run (104 driver
  connections, and one upstream connection per forwarded request and direct send).

After the change, twelve copies at once and then twelve more as soon as they ended, done twice
(the second time on the final code, with the 104-byte sends): 24 of 24 passed each time, none with
EADDRNOTAVAIL, at loads of 9 to 24. The machine's TIME_WAIT count peaked at 26,046 and 26,365,
more than the 16,384 local ports, which shows that the sockets on the fake upstream's listening
port do not use them up. The copies' added time was 17.4 to 24.3 ms (median), p95 31.6 to 78.4 ms,
growth 22.7 to 34.9 ms per million tokens.

Other test files on this machine have failed with the same error under load
(`tests/test_launcher.py`). [Inference] The old harness plausibly contributed: each whole-suite run
held about 1,700 local ports for 30 seconds while its short test ran, so four to six suites
reaching it within half a minute of each other, on top of the 1,400 to 6,000 sockets other loopback
tests hold, could take all 16,384, and any process connecting in that window fails. It was not the
only load on the ports, and no `test_launcher.py` failure was reproduced here.

#### The 40 ms wait on Linux (2026-11-14)

Reviewer B found that, once the driver kept its connections open, every request on GitHub's Ubuntu
runners took about 41 ms more, whatever its size. macOS showed no such wait. **Finding: the wait is
the proxy's, not the driver's** (issue [#284](https://github.com/VentureForgre/contexto/issues/284)).
It comes from how the proxy writes its responses. [Inference] A client that keeps its connection
open to the proxy on Linux, as Claude Code does, would meet it on every request; only the Python
driver was measured.

[Inference, from the code and the measurements below] The proxy's handler (`contexto.proxy._Handler`,
a `BaseHTTPRequestHandler`) leaves Nagle's algorithm on, as `socketserver` does by default
(`disable_nagle_algorithm = False`). It writes a response's headers and then each chunk in separate
`send` calls. Nagle holds the first chunk until the client has acknowledged the headers. On a
connection that has carried a request and a response before, Linux delays that acknowledgement by
its minimum of 40 ms, hoping to send it with data. A new connection acknowledges at once, which is
why the driver's connection per request until 2026-11-13, and the direct sends to the fake
upstream (which closes each connection), did not show the wait.

Reviewer B suggested that the driver's request writes were the cause (`http.client` sends a
request's headers and body in two calls). They are not: `http.client.HTTPConnection.connect` has
set `TCP_NODELAY` on every Python CI runs (3.12 to 3.15). The driver now sets it as well
(`run.Client.connect`), so its timings do not depend on that. As expected, this changed nothing.

To measure it, the short run sends the 104-byte request 20 more times after `worker.py`'s `nagle`
command has set `_Handler.disable_nagle_algorithm` in the proxy process: the package with
`TCP_NODELAY` on its accepted sockets, as an experiment in the harness only. It also reports how
much of the request's added time comes before the response's headers. Neither is budgeted. In CI
run 43552340153 at 8e0dc259, on the four Ubuntu legs (Python 3.12 to 3.15, 4 CPUs, loads 0.5 to
0.9), in milliseconds over a direct send:

| | Ubuntu, the package as shipped | Ubuntu, `TCP_NODELAY` on the proxy's sockets | This machine (macOS), 10 runs |
|---|---|---|---|
| 104-byte request | 41.3 to 41.5 | 0.79 to 1.47 | 0.47 to 0.59 as shipped, 0.46 to 0.58 with it |
| of which before the response's headers | 0.90 to 0.98 | | 0.27 to 0.33 |
| First request (50,838 bytes) | 44.8 to 45.2 | not measured | 2.39 to 2.85 |

So about 40.5 ms of each request's added time on Ubuntu comes after the proxy has sent the
response's headers, and it goes when the proxy's sockets have `TCP_NODELAY`. The fix belongs in the
package: `disable_nagle_algorithm = True` on `_Handler`. One write for the headers and the first
chunk would remove only the first wait, since a later chunk written while an earlier one is not yet
acknowledged can still be held. The harness does not change the package; issue
[#284](https://github.com/VentureForgre/contexto/issues/284) records the fix. What it costs a real
session was not measured: a real upstream's replies stream over seconds, and the fake upstream
sends each reply at once, so whether the chunks of a stream wait as well was not tested.

What the Ubuntu legs measure, before and after (Reviewer B's figures for the first two columns):

| | e14f6630 (run 43365787007), a connection per request | bd7b8dd8 (43548374232), kept open | 8e0dc259 (43552340153), kept open, driver `TCP_NODELAY` |
|---|---|---|---|
| Added time per request, median | 16.5 to 23.6 ms | 54.3 to 65.6 ms | 61.8 to 65.7 ms |
| p95 | | 64.0 to 81.3 ms | 78.3 to 99.0 ms |
| Growth per million tokens | | 19.3 to 37.3 ms | 28.2 to 37.4 ms |
| First request over a direct send | 4.4 to 5.4 ms | 43.4 to 45.1 ms | 44.8 to 45.2 ms |
| 104-byte request over a direct send | not measured | 40.6 to 41.6 ms | 41.3 to 41.5 ms |
| Duration of the short run | 14.6 to 23.7 s | 38.8 to 47.2 s | 45.9 to 48.4 s |

The budgets were not changed. The median's margin on Ubuntu is 3.0 to 3.2 times, against 8.5 to
12 times before the driver kept its connections open; growth's is 8.0 to 10.7 times. The harness
could turn Nagle off in the proxy for the whole run to measure the proxy's work alone, but then the
required check would no longer measure the package as it ships, so it does not.

### Against the audit's four numbers

An audit on 2026-11-12 measured four numbers on a fast Apple laptop; it did not record the turn
size of the session it timed. [Rewritten 2026-11-13] The first version of this section compared
them with four sizing runs whose outputs were not kept, made with a driver whose clock still
included the TCP connect. Those numbers are replaced by fifteen runs of the final `short.py`, all
in `results/short_2026-11-13.jsonl` (one JSON object per run, as `short.py --json` writes it; `short.py
--spread FILE` prints the spread): three runs at each of five turn sizes, 200 turns each, except 60
at 26,000 tokens, as Reviewer A ran it. Measured on the M5 Pro on 2026-11-13 at 1-minute loads of
10.1 to 13.0, all at the head of this PR (b0e2dbc's package with main merged in).

```
for t in 3000 4000 5000 8000; do .venv/bin/python spikes/m2/scale/proxy/short.py --turn-tokens $t --repeat 3 --json out.jsonl; done
.venv/bin/python spikes/m2/scale/proxy/short.py --turn-tokens 26000 --turns 60 --repeat 3 --json out.jsonl
```

| Quantity | Audit | Measured here (median of the runs, smallest to largest) | How |
|---|---|---|---|
| Added time per request | about +2 ms | 3.2 ms (2.5 to 4.7) for the session's first request (50,838 bytes); 0.73 ms (0.50 to 1.13) for a 104-byte request with no tools; 3.8 ms (2.9 to 5.0) as the intercept of the growth fit | through the proxy less straight to the fake upstream, median of 20 sends each, 15 runs |
| Growth with history | about 13 ms per million tokens, turn size not recorded | 16.7 ms with 3,000-token turns, 18.6 with 4,000, 15.3 with 5,000, 12.5 with 8,000, 9.2 with 26,000 (each the median of 3 runs) | the growth fit over tool loops |
| Throughput at 100 concurrent | 768 requests a second | 318 requests a second (248 to 417) | 200 copies of the 50,838-byte first request from 100 threads, 15 bursts |
| Startup | 60 ms | 57.2 ms (49.6 to 73.1) | process start to `ready`, best of 3, 15 runs |

**Growth per million tokens depends on the turn size.** Part of the proxy's cost per request is per
message (hashing and classifying each one), so the same number of tokens in smaller turns, which
means more messages, costs more. Each figure below is the median of three runs, the range in
brackets:

| Turn size | Growth of added time, ms per million tokens | Growth of the proxy's CPU time | Reviewer A, 2026-11-12, loads 10 to 12 |
|---|---|---|---|
| 3,000 tokens | 16.7 (16.7 to 19.9) | 16.6 | 18.9 |
| 4,000 tokens | 18.6 (16.8 to 19.6) | 18.4 | 18.2 |
| 5,000 tokens | 15.3 (14.9 to 16.6) | 15.0 | |
| 8,000 tokens | 12.5 (12.2 to 13.1) | 12.2 | 16.2 |
| 26,000 tokens (60 turns) | 9.2 (8.9 to 9.4) | 9.1 | 9.9 |

The audit's "about 13 ms per million tokens" therefore matches 8,000-token turns here and not the
short run's 3,000-token turns (about 17 to 19). The record's 8.5 ms per million tokens was measured
with 26,000-token turns (`synth.Shape`'s default), where this package measures 9.2 at a load of 10.

Added time depends on the request: the audit's 2 ms lies between the 0.73 ms of a 104-byte request
and the 3.2 ms of a Claude Code-sized first request, of which about 1.4 ms is four store writes
(`set_retained`, `set_turn_room` twice, `set_epoch`; measured on 2026-11-12, not in these files).
Throughput here is about 40 percent of the audit's. What the audit sent is not recorded, and the
100 clients here share one Python process with the fake upstream, so the two may not measure the
same thing.

Against the 2026-10-18 record ("Before and after", after the CPU fixes): a tool loop cost 8.5 ms
more per million tokens of history then, with 26,000-token turns. [Rewritten 2026-11-13: this
paragraph put the difference from the 13 to 17 ms of the sizing runs down to "this machine on this
day". Reviewer A showed the cause is the turn size: those runs used turns of 4,000 to 8,000
tokens.] At 26,000-token turns this package measures 9.2 ms per million tokens through HTTP (table
above), and `cputime.py` gives the record's package and this one nearly the same slope.

`cputime.py` runs the same requests through two source trees in turn, with no HTTP, and keeps each
request's smallest CPU time over several repetitions. It gives each tree's `observe_usage` what that
tree's proxy would: the reply's output tokens, and `tool_calls` and `tool_call_ids` where the tree
takes them (62b02a1 takes neither). Its 26,000-token turns are `synth.Shape`'s default.

```
mkdir /tmp/r1018 && git archive 62b02a16 src | tar -x -C /tmp/r1018   # 62b02a1, the merge of #205 (the record's "after")
.venv/bin/python spikes/m2/scale/proxy/cputime.py --src /tmp/r1018/src --src src --turns 17,76 --reps 3
```

The first version of this section called 62b02a1 "v0.2.9". That cannot be checked: its
`pyproject.toml` says 0.1.0 and the v0.1.0 to v0.4.0 tags were deleted (D62). It is named by its
commit now.

CPU time depends on the load on this machine, so a comparison holds only between two trees run
side by side in one invocation. The same 62b02a1 tool loop at 0.55M tokens cost 6.7 CPU-ms at a
load of 8 to 13 (`results/cputime_2026-11-13.md`), 7.4 at a load of 10 (Reviewer A), and 16.3 to
19.7 at loads of 32 to 51 (the two comparisons below). [Inference] At high load the scheduler puts
work on efficiency cores and lowers clock speeds, which the smallest of a few repetitions does not
remove; the 49.8 against 32.1 row below is the kind of outlier that follows.

| History | Load | Tool loop, 62b02a1 | Tool loop, head | Rotating turn start, 62b02a1 | Rotating turn start, head |
|---|---|---|---|---|---|
| 0.55M | 8 to 13 (2026-11-13, committed) | 6.7 | 6.9 | 81 | 17 |
| 2.07M | 8 to 13 (2026-11-13, committed) | 18.4 | 19.3 | 118 | 32 |
| 0.55M | 10 (Reviewer A) | 7.4 | 7.5 | 88 | 18 |
| 2.07M | 10 (Reviewer A) | 18.7 | 20.5 | 120 | 34 |
| 0.55M, first comparison | 32 to 51 (2026-11-12) | 16.3 | 17.5 | 175 | 40 |
| 0.55M, second | 34 | 19.7 | 21.5 | 222 | 51 |
| 2.1M, first | 32 to 51 | 41.8 | 43.9 | 261 and 251 | 82 and 69 |
| 2.1M, second | 34 | 49.8 | 32.1 | 299 and 310 | 47 and 55 |
| 5.0M, first | 32 to 51 | 90.9 | 95.7 | 356 | 139 |
| 5.0M, second | 34 | 89.7 | 100.4 | 353 | 149 |

The 2026-11-12 rows used 5, 4 and 3 repetitions at the three sizes in the first comparison and 3 in
the second; their output was not kept. In them `observe_usage` was given 180 output tokens and no
`tool_calls`, which the CPU time hardly depends on.

- Slope of a tool loop, 0.55M to 2.07M: 62b02a1 7.7 and the head 8.1 ms per million tokens at a
  load of 8 to 13; 7.4 and 8.5 in Reviewer A's run at a load of 10. The record's 8.5 is in the same
  range. At loads of 32 to 51 the same code measured 15.6 to 17.5, about twice as much: the load,
  not the package, doubles CPU time there.
- Per tool loop the head was slower in 9 of the 10 rows, by 1 to 12 percent (3 and 5 percent in the
  committed run), and 36 percent faster in the one outlier. That leans towards the head costing a
  few percent more per tool loop. With one to three runs per row and a spread between runs as
  large as the difference, it is not established.
- A rotating turn start cost 2.4 to 6.4 times less CPU on the head in every row. That is D34's page
  catalog (next section).

### The full run at v0.5.24

```
.venv/bin/python spikes/m2/scale/proxy/run.py --label v0524-1          # 0.5M to 30M
.venv/bin/python spikes/m2/scale/proxy/summarize.py v0524-1
.venv/bin/python spikes/m2/scale/proxy/fidelity.py --turns 30          # writes results/fidelity_first_30.json
.venv/bin/python spikes/m2/scale/proxy/leak.py --at 0.5 --turns 3 --label check
```

All four ran to the end at b0e2dbc, as did `run.py --reminders mixed` to 2M and `fidelity.py --turns
30 --reminders mixed` (both fidelity runs: rotations, pages and next-turn requests equal). The
small runs' outputs were not kept.

The kept run, `results/*_v0524-1.*`, was rerun on 2026-11-13 with the final harness (commit
d37d347a; nothing `run.py`, `worker.py`, `synth.py` or `upstream.py` does changed after it), so
`requests_v0524-1.csv` has every column the harness writes, `proxy_cpu_s` included. The first
`v0524-1`, taken on 2026-11-12 at loads of 26 to 42 by an earlier revision of the harness (34
columns, no `proxy_cpu_s`), is replaced; its sizes, counts and rotations were the same as the
rerun's. The package is b0e2dbc's with main merged in, which changed only `contexto/__init__.py`
and `contexto/cli.py` (the version and packaging); the proxy and the engine are v0.5.24's. The
rerun took 241 s at loads of 9 to 18, against the record's 420 s or so at 2 to 7, so its timings
are still not comparable with the record's and are not in the table.

To tell the two causes of the differences apart, the same run was made once more at the same
package with the October `synth.py` (only the names the repaired harness imports were added to it),
`results/*_v0524-synth1017-1.csv`, with `--keep-going` because that session logs
`session_start_unrecognised` and `mcp_tools_missing`. Against `perf-1` of 2026-10-18:

| | 2026-10-18 (`perf-1`): October package and synth | 2026-11-13, head package, October synth | 2026-11-13 (`v0524-1`): head package and synth |
|---|---|---|---|
| Turns at 0.5M, 2M, 5M, 10M, 30M | 17, 76, 188, 376, 1,140 | the same | the same |
| Largest upstream request measured | 135,954 tokens | 136,105 | 136,176 |
| Largest last request of a fast-forwarded turn | 144,176 tokens | 143,940 | 144,011 |
| `over_bound`, `bound_too_small`, `transform_error`, non-200 | 0 | 0 | 0 |
| Rotations through 30M (session log) | 557 | 559 | 559 |
| Fast-forward rotations by reason: `bound`, `min_turn_room`, `window_minimal` | 370, 160, 4 | 370, 163, 4 | 371, 161, 4 |
| F | 69,565 to 70,031 | 69,716 to 70,182 | 69,786 to 70,252 |
| W | 42,469 to 55,435 | 42,319 to 55,284 | 42,249 to 55,214 |
| Pages and store size at 30M | 1,145 and 56.78 MB | 1,145 and 56.83 MB | 1,145 and 56.84 MB |
| Peak RSS of the checkpoint's proxy at 0.5M, 2M, 5M, 10M, 30M | 41, 124, 322, 756, 1,573 MB | 60, 135, 332, 544, 1,676 MB | 51, 119, 338, 761, 1,663 MB |

What each change accounts for:

- **The synth change** (third column against the second: same package, the request content of
  this PR against October's) moves F up and W down by 70 tokens, the 70 tokens the head grew by,
  and the largest requests by 71. It changes 12 of the 1,097 fast-forward rotation decisions the
  two runs share: two rotations change reason (turns 90 and 426, `min_turn_room` to `bound`), one
  is added (a `min_turn_room` at turn 338), and a run of five `bound` rotations at turns 296 to 304
  becomes four, each one turn later. The counts by reason (`bound` 371 against 370,
  `min_turn_room` 161 against 163) are also not over the same turns: turns 382 and 383 were
  fast-forwarded in the October-synth run (with a `min_turn_room` rotation at 383) and measured
  over HTTP in the other (1,099 against 1,097 fast-forward rows, 140 against 150 request rows).
  The session log counts 559 rotations in both at 30M, but 186 against 187 at the 10M checkpoint.
- **The package and the repaired harness** (second column against the first: the same October
  request content) move F up and W down by 151 tokens, add 2 rotations (557 to 559), change 44 of
  the 1,097 fast-forward rotation decisions the two runs share (3 more `min_turn_room` rotations
  net), and lower the largest fast-forwarded last request by
  236 tokens. These include what the harness now does as the proxy does (D40's `tool_calls`, the
  Claude Code headers, 420 output tokens for a reply with no tool call) as well as three weeks of
  package changes; this run does not separate those. In the second column the engine also did not
  recognise the session start (`session_start_unrecognised`), which the third column's synth fixes.
- Nothing in either change comes near the bound: every request is at least 5,800 tokens under it.

Peak RSS at 30M is from one process that handled 17 requests, once per run: 1,663 MB in the rerun,
822 MB in the replaced first run and 1,573 MB in the record, so it varies by about two times
between runs and one run says little. `leak.py --at 30` was not run.

One timing survives the load, because it fell so far. `index_text` per rotation at 30M (two
calls, fast-forward, median of the rotating turns among the last 60) was 0.272 s in the record, of
which 0.167 s was reading pages. In the rerun it was 0.022 s at loads of 9 to 13, with 0.015 s in
the catalog's `entries` (0.045 s and 0.027 s in the first run, at loads of 27 to 40): D34's
catalog reads one to three page files per rotation where `_pages` read all 1,145 twice. A proxy
process started on an existing store reads every page once, at its first rotation (1,160 page
reads in one request at 30M, in both runs).

### Not measured

- Wall-clock timings of the full run at a load comparable with the record's. The rerun was at
  loads of 9 to 18. `cputime.py` compared CPU time with 62b02a1 up to 5M tokens; nothing was
  compared at 10M or 30M.
- The short run on a quiet machine in its final form. Every one of its runs here was at a load
  of 9 or more; the CI runs (loads under 2.4) are the quiet ones.
- Throughput with the clients in other processes than the fake upstream.
- `leak.py` at 5M or 30M, and `fidelity.py` at 120 turns, at this commit. Both were run only at
  small sizes to check that they work.
- Which package change since October moves F by 151 tokens and adds the two rotations (see "The
  full run at v0.5.24"); the comparison separates the synth change from everything else, not the
  package from the harness's own fixes.
- Whether the old harness's port use caused the `tests/test_launcher.py` failures seen on this
  machine (see "Connections").
- Windows, which the package does not support.
