# M2 acceptance, live run #3: the record (2026-10-16)

This is the final M2 acceptance run, which intake recorded as final at 10:07Z. It gives numbers,
ids, counts and booleans only. No answers, statements, page text or transcripts are recorded here.
The run's artifacts stay private, outside any repository.

## Code and session

| Item | Value |
|---|---|
| Code | main `fbb1303cfded165b455b1d3a56bcbc817a18be58` |
| Session | `acc20261016T095818Z` |
| Client and model | Claude Code 2.1.301, `claude-haiku-5-5` |
| Bound | 150,000 |
| Probes | D27: 6 probe turns of 9, seed 20261016 |
| Content | after the plant-order fix (PR #127); both arms ran on it |
| Product on main | B19 in-view search, B21 latest lines, B20, A12 room rotation, A14 retained snapshot |
| H1 | 09:58:18Z to 10:00:54Z |
| Baseline | 10:02:28Z to 10:03:59Z |

## Verdicts

| Acceptance | Verdict | Figures |
|---|---|---|
| 1. Long run | PASS | See the first table below |
| 2. Fidelity | PASS | H1 51/54 vs baseline 48/54; overall gap -3 (H1 ahead); no category below the baseline |
| 3. Recall | PASS | Paged-only 4/4 answered correctly and credited, each via paged `contexto_search` spans |
| 4. Cache | 36/37 (0.973) | One miss, at Messages request 8 |

**Acceptance 1, every verdict:**

| Verdict | Result | Figure |
|---|---|---|
| zero_compaction | T | 0 compaction events, 0 stream compactions |
| no_request_over_bound | T | max input 109,868 |
| no_pre_send_over_bound_outside_tool_loop | T | none |
| no_real_over_bound | T | 0 |
| no_failure_events | T | `failure_events` {}: `retained_stale` 0, `turn_room_low` 0 |
| no_bound_too_small | T | 0 |
| every_request_has_usage | T | 44/44 |
| zero_4xx | T | model-path 4xx 0 |
| no_local_5xx | T | local 5xx none; upstream 5xx none |

There were 44 Messages requests and 7 epochs. All 6 rotations were `min_turn_room` (A12), and
there was no `window_minimal`.

**Acceptance 2, per category (both arms, correct):**

| Category | H1 | Baseline |
|---|---|---|
| chain | 28/30 | 25/30 |
| distractor | 6/6 | 6/6 |
| multihop | 8/8 | 8/8 |
| obligations | 5/6 | 5/6 |
| paged_only | 4/4 | 4/4 |
| **Total** | **51/54** | **48/54** |

**Acceptance 3:** `recall_credited` is true and `in_view` spans are excluded (PR #130). The run made
78 `contexto_search` calls and 0 `contexto_recall` calls. Search returned 94 `in_view` spans across
the run, and none of them credits.

## Misses (ids only)

| Probe | Category, tier | Probe turn | Kind |
|---|---|---|---|
| H1 only: q_a_010 | chain, depth6-previous | 4 | stale |
| H1 only: q_a_042 | obligations, single | 4 | stale |
| Baseline only: q_a_012 | chain, depth6-previous | 2 | stale |
| Baseline only: q_a_024 | chain, depth8-current | 3 | stale |
| Baseline only: q_a_032 | chain, depth8-current | 6 | stale |
| Baseline only: q_a_038 | chain, depth8-current | 1 | stale |
| Baseline only: q_a_040 | obligations, single | 6 | stale |
| Both: q_a_033 | chain, depth8-previous | 4 | stale |

"Stale" means the answer was an older value of the probe's chain or record.

## Probe turns (H1)

The room is taken at each probe turn's first request: 150,000 - input - 200 - 25,000.

| Probe turn | Room | Requests | Searches | Spans in_view / paged | Epoch | Rotation at start |
|---|---|---|---|---|---|---|
| 1 | 25,696 | 5 | 13 | 11 / 19 | 5 | none |
| 2 | 20,474 | 3 | 10 | 15 / 23 | 5 | none |
| 3 | 59,836 | 2 | 14 | 18 / 40 | 6 | min_turn_room |
| 4 | 51,229 | 5 | 12 | 15 / 33 | 6 | none |
| 5 | 41,380 | 6 | 12 | 15 / 30 | 6 | none |
| 6 | 34,030 | 5 | 17 | 20 / 43 | 6 | none |

Every room is at least `min_turn_room` (12,000), so `turn_room_low` is 0. The content-phase
rotations were at content turns 4, 7, 10, 13 and 16.

## Spend

| Line | Tokens | Limit |
|---|---|---|
| H1 (record: driver = proxy log, in-flight 0) | 3,748,084 | cap 4,007,279 |
| Baseline (6 requests, each 200 end_turn) | 2,634,365 | worst 2,872,721 |
| **Total** | **6,382,449** | approval 6,880,000 (line finished) |

The approval table at this main gives expected 5,636,856 and worst 6,874,704: H1 4,001,983 plus
baseline 2,872,721. The head is derived from measured bytes (PR #140). The live gate refused 6,874,703 and
accepted 6,874,704 and 6,880,000 (zero-spend check against a stub, main fbb1303). The baseline's
`count_tokens` ran from 435,590 to 438,406 tokens per request, over 56 to 71 messages.

## Off-path events

- **cli_exit 143.** After request 44 the spend watcher's projection reached 4,011,276: real spend
  3,748,084 plus 2 x (last input 99,596 + reserve 32,000). That passed the H1 cap of 4,007,279, so
  the watcher sent SIGTERM. All 24 turns had finished by then, so nothing was lost.
- **Kill timing**, checked read-only on the run's files: the SIGTERM came after request 44 was fully
  logged and the store was complete.
  - The session log is 71,742 bytes, ends with a newline, and has 128 lines, all of which parse.
  - Request 44 is complete: status 200 and full usage. Only `proxy_stop` and `exit` (code 143) follow it.
  - `meta.json` is valid, and its `request_seq` of 44 shows the A14 snapshot was written for request 44.
  - `retained.json` is valid, all 17 pages are valid, and there are no temp or partial files.
  - The H1 record shows 24/24 turns, 6/6 probe turns, stopped null and 54 answer lines.
  - Claude Code's session JSONL has 484 lines, all of which parse, and ends with a newline.
- `HEAD /api/hello` returned 401 once. This is Claude Code's unauthenticated connectivity probe, a
  non-model path, and is known from earlier runs.

## Spend calibration (a later change, after v0.1.0)

H1 cost 795,945 more than the model's expected 2,952,139.

- **Content turns:** 1,348,490 actual against 1,489,036 modelled, 18 requests each.
- **Probe turns:** 2,399,594 actual against 1,463,103 modelled, 26 requests against 15. The 78
  searches came in 20 tool-loop rounds against the model's 9 (rounds per probe turn 4/2/1/4/5/4),
  and each round re-sends the whole window. Input grew 1,302 to 8,164 per round, against 17,778
  per recall in the model.
- **Cause:** the round count, not input growth.
- **Proposal** (row `qitem-m2-tool-rounds-calibration`, after the release): rename `budget.Params.recalls`
  to `tool_rounds`. Price the worst case at the observed maximum (5 rounds, 8,200 tokens per
  round) and the expected case at the observed mean (about 3.3 rounds, about 2,300 per round), with a test pinned to
  this run.
- **Product note** (intake): fewer, larger search rounds are a cost lever worth a later look.

## Earlier runs

[M2-acceptance-harness.md](M2-acceptance-harness.md) records the earlier runs:
- live H1 attempt 1;
- the aborted 07:08Z run, not a verdict;
- the plant-order fix;
- the derived head (PR #140).

Live run #2 (main c8f0b08, old content: acceptance 2 failed on chain, 22 against 24) is
superseded by this run on the new content.
