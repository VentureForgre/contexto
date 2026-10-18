# Proxy at scale: a 30M-token session through the real proxy (zero spend)

Can the Contexto proxy carry a Claude Code session whose history reaches about 30 million tokens?
This harness drives the real proxy over HTTP with a synthetic Claude Code-shaped session and a local
fake upstream, and measures it at 0.5M, 2M, 5M, 10M and 30M tokens of history. Nothing talks to a
model API: the proxy's upstream is `http://127.0.0.1:<port>` and `worker.py serve` refuses anything else.

Measured on 2026-10-17, main at 4525388, Python 3.14.6, Apple M5 Pro (18 cores, 48 GB), with other
agents benchmarking on the same machine. 1-minute load averages were 3.1 to 7.4 during the timed
runs (`load1` in `results/requests_*.csv`; the `uptime` line for each checkpoint is in
`results/checkpoints_*.csv`). Every number below is measured unless it is marked as an inference.

## Short answer

- **With a context block on the first message only and one session-start system message (the `first`
  profile), the proxy carries 30M tokens.** That is the context-block placement Claude Code 2.1.302 uses
  on 5.x model ids, but those ids also send a role `system` message with every main request, which this
  run does not model (`../reminders/`). Every upstream request stayed under the 150,000 bound
  (largest measured 135,954 tokens; largest last request of any fast-forwarded turn 144,176). There were no `over_bound`,
  `bound_too_small` or `transform_error` events, and no non-200 responses. At 30M the proxy added 0.58 s
  to a tool-loop request and 0.86 s to a turn start. The cost grows linearly with history.
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

## Obvious fixes (this spike changes nothing in `src/`; fix 1 was applied later as D32)

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
- `tests/test_scale_proxy_harness.py`: fast checks of the helpers (7 tests, under a second, collected
  by the default `pytest`). The scale run itself is not in the suite.
