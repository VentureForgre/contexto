# M2 acceptance, live run #4: the record (2026-11-06)

This is the full M2 acceptance run, D27 protocol, on `claude-haiku-5-5` at the default bound
150,000, on Claude Code 2.1.318, through the owner's proxy. The owner approved the run on
2026-11-06. The record gives numbers, ids, counts and booleans only. No prompts, answers, page text
or transcripts are recorded here. The run's artifacts stay private, outside any repository.

**Every acceptance passes.** H1 completed and passes acceptance 1, 3, 4 and 5. The full-context
baseline's first attempt stopped at request 1's `count_tokens` (400, 0 tokens spent): the replay
kept a `tool_reference` to a Contexto tool it removes. That was the harness, not the product. With
the harness fix (PR #247: `replay.build_request` turns such references into a text note), the
baseline re-ran from the same transcript: 6/6 requests returned 200 and it scored 47/54.
Acceptance 2 passes: H1 52/54, with no category below the baseline. The run spent 6,001,818 tokens
in all (see "The baseline").

## Code and session

| Item | Value |
|---|---|
| Code | tag v0.5.20, `23887526937c4f569a64706a2ed6a796e5396eec`, a clean clone |
| Session | `acc20261106T063507Z` |
| Client and model | Claude Code 2.1.318, `claude-haiku-5-5` |
| Bound | 150,000 |
| Probes | D27: 6 probe turns of 9, seed 20261016, 18 content turns of 40 KB |
| H1 | 06:35:07Z to 06:37:09Z |
| Baseline | attempt 1: 06:37:42Z to 06:37:44Z, stopped before its first request; attempt 2: 06:51:45Z to 06:53:16Z, 6/6 requests |
| Baseline harness | v0.5.20 plus the replay fix, `213efbc` (PR #247, branch `claude/replay-tool-reference`); harness only, product and scorer unchanged |

**Seed.** The same seed as run #3, 20261016. The content turns, the 54 probes and the D27
assignment are identical to run #3's. From fbb1303 to 2388752, `spikes/m2/acceptance/script.py` and
`spikes/m0/fidelity/` changed only in lint (Ruff renames, one unused import, one `noqa`) and one
comment (8 insertions, 7 deletions over 5 files).

## Departures from the contract

None. The model was `claude-haiku-5-5` (the harness default, `driver.MODEL`, also passed as
`--model`). Claude Code ran on default settings: no `--allow-contexto-tools`, and the H1 record has
`allow_contexto_tools: false`. The credential reached Claude Code through the seat-settings
`apiKeyHelper` shim, which reads the owner's token from the environment.

The baseline ran on v0.5.20's harness plus one replay change (PR #247). The change is in
`replay.build_request` only. The H1 record, the H1 answers and the transcript are the ones from
06:35Z, and `run.py check` gives the same 11 verdicts at `213efbc` as at 2388752.

## Verdicts

| Acceptance | Verdict | Figures |
|---|---|---|
| 1. Long run | PASS | See the first table below |
| 2. Fidelity | PASS | H1 52/54, baseline 47/54; overall gap -5; category gaps chain -2, distractor 0, multihop 0, obligations -3, paged_only 0; `compare.pass` true |
| 3. Recall | PASS | Paged-only 4/4 answered correctly and 4/4 credited, each through one `contexto_search` call that returned the plant's paged span before its answer |
| 4. Cache | PASS | 38/38 (1.0); no misses |
| 5. Tests | PASS | 2880 passed, 1 skipped, 1 xfailed at 2388752 (macOS, 5 min 33 s); CI and Release green on 2388752 |

**Acceptance 1, every verdict:**

| Verdict | Result | Figure |
|---|---|---|
| zero_compaction | T | 0 compaction events, 0 stream compactions |
| no_request_over_bound | T | max input 110,465 |
| no_pre_send_over_bound_outside_tool_loop | T | none (and none inside the tool loop) |
| no_real_over_bound | T | 0 |
| no_failure_events | T | none |
| no_bound_too_small | T | 0 |
| every_request_has_usage | T | 45/45 |
| zero_4xx | T | model-path 4xx 0 |
| no_local_5xx | T | local 5xx none; upstream 5xx none |

There were 45 Messages requests, all 200, and 7 epochs (4/3/3/3/4/9/19 requests).
- **Rotations:** 6, by the session log's reasons: 4 `bound` (content turns 5, 9, 12, 16) and 2
  `head_change` (content turn 6 and probe turn 2). There was no `window_minimal`.
- **Head changes:** 2 `head_changed` events, in content turn 5 (epoch 1) and probe turn 1 (epoch
  5). Each follows a `ToolSearch` call in that turn, and the recorded request shapes grow from 12 to
  13 and then 15 tools (`contexto_pin`, then `contexto_search` and `contexto_recall` loaded as
  deferred tools). So the head changes come from tool search loading Contexto's tools [inferred from
  the shapes].
- **Requests:** 20 in content turns, 25 in probe turns. Claude Code 2.1.318 sent no title side
  request (the first request has 2 messages).
- **Pre-usage estimate:** one, the first request: estimate 25,611 against a real 40,266, ratio
  0.636 (the run's one `estimate_error`, `estimate_min_ratio` 0.636).
- **Exit:** the CLI exited 0.

**Acceptance 2, per category (correct):**

| Category | H1 | Baseline |
|---|---|---|
| chain | 28/30 | 26/30 |
| distractor | 6/6 | 6/6 |
| multihop | 8/8 | 8/8 |
| obligations | 6/6 | 3/6 |
| paged_only | 4/4 | 4/4 |
| **Total** | **52/54** | **47/54** |

The pass rule needs H1 within 2 of the baseline overall and no category more than 1 below. H1 is 5
above the baseline overall and at or above it in every category, so `score` gives `compare.pass`
true. All 54 baseline answers parsed. Stale answers: H1 2 (chain), baseline 5 (4 chain, 1
obligations).

**Acceptance 3:** `recall_credited` and `paged_only_plants_paged` (4/4) are true, from `run.py
check` with no correction. The run made 72 `contexto_search` calls and 0 `contexto_recall` calls.
Search returned 11 `in_view` spans and 173 paged spans.

| Probe | Answered correctly | Returned before the answer | Credited | Via |
|---|---|---|---|---|
| q_p_01 | T | T | T | 1 `contexto_search` call, paged span on the plant's page |
| q_p_02 | T | T | T | as q_p_01 |
| q_p_03 | T | T | T | as q_p_01 |
| q_p_04 | T | T | T | as q_p_01 |

All four plants are on one page, each held by 1 page. The one content-turn pin was of kind
`obligation`, not the event ledger the Opus run pinned.

**Acceptance 4:** 38 of 38 eligible requests read the cache. The 7 ineligible requests are the
first request of each epoch.

## Misses (ids only)

| Probe | Category, tier | Kind |
|---|---|---|
| H1: q_a_010 | chain, depth6-previous | stale |
| H1: q_a_025 | chain, depth8-previous | stale |
| Baseline: q_a_012 | chain, depth6-previous | stale |
| Baseline: q_a_022 | chain, depth6-previous | stale |
| Baseline: q_a_033 | chain, depth8-previous | stale |
| Baseline: q_a_035 | chain, depth8-previous | stale |
| Baseline: q_a_040 | obligations, single | stale |
| Baseline: q_a_043 | obligations, set6 | other: the 4 expected items plus 1 more |
| Baseline: q_a_044 | obligations, set6 | other: the 4 expected items plus 1 more |

## Probe turns (H1)

The room is taken at each probe turn's first request, as in run #3: bound 150,000 - input - 200 -
25,000.

| Probe turn | Room | Requests | Searches | Spans in_view / paged | Epoch | Rotation at start |
|---|---|---|---|---|---|---|
| 1 | 36,101 | 6 | 14 | 5 / 27 | 5 | none |
| 2 | 74,121 | 3 | 10 | 2 / 24 | 6 | head_change |
| 3 | 67,250 | 2 | 10 | 0 / 25 | 6 | none |
| 4 | 61,081 | 5 | 12 | 3 / 33 | 6 | none |
| 5 | 53,311 | 5 | 11 | 1 / 26 | 6 | none |
| 6 | 47,244 | 4 | 15 | 0 / 38 | 6 | none |

Every room is at least `min_turn_room` (12,000). The probe turns took 19 tool-loop rounds after
their first request (5/2/1/4/4/3). Probe turn 1 also made 2 `ToolSearch` calls and 2 `Bash` calls.

**Content turns.** One content turn called tools: content turn 5 made 1 `ToolSearch` call and 1
`contexto_pin` call (kind `obligation`, recorded), over 3 requests. Every other content turn took 1
request.

## The baseline

### Attempt 1: stopped before any spend

- `run.py replay --dry` decoded the transcript: 62 messages in request 1, 12 tools, 8 betas,
  `thinking.display` `updates` changed to `omitted`. It does not call `count_tokens`.
- The live replay stopped at request 1's `count_tokens`: 400 `invalid_request_error`. It sent no
  Messages request: `spent` 0, `completed` 0.
- **Cause** (zero-spend diagnosis, `count_tokens` only, outside the harness): the API rejects the
  request because a `tool_reference` names `mcp__contexto__contexto_pin` and that tool is not in
  `tools`. The history before probe turn 1 holds content turn 5's `ToolSearch` result, which
  references `contexto_pin`. The replay strips every Contexto tool and sets `tool_choice: none`.
  This is the risk the runbook noted. Dropping `tool_choice` alone does not fix it.
- **Two fixes, both accepted by `count_tokens`** (request 1, no tokens billed):

  | Variant | count_tokens |
  |---|---|
  | as built (the harness) | 400 |
  | as built, no `tool_choice` | 400 |
  | each `tool_reference` replaced by a text block naming the tool | 200, 426,266 input tokens |
  | the referenced Contexto tool kept, `defer_loading: true`, `tool_choice: none` kept | 200, 426,675 input tokens |

  Choosing one was a harness change (`replay.build_request`). The text block was chosen (attempt
  2), because the baseline must not get Contexto's tools.

### The harness change (PR #247)

- `replay.build_request` turns every history block that names a tool the baseline does not send
  into a text note, `Tool <name> loaded.` (or `removed.`). This covers `tool_reference` in a
  `tool_result`, and 2.1.318's inline-tools `tool_addition` / `tool_removal` blocks in a `system`
  message (used when tool search is off), by reference or by a Contexto `tool_definition`.
  `tool_use` of a removed tool is left as sent; the API accepts it with `tool_choice: none`.
- The replay labels the change: `changes_from_recorded` gives `thinking.display` updates to
  omitted, and `unsent_tool_notes` 1 block (`mcp__contexto__contexto_pin`).
- Tests: 3 new harness tests. The 2 behaviour tests fail on main. 294 harness tests pass (291
  before). Full suite at `213efbc`: 2880 passed, 1 skipped, 1 xfailed; 3 proxy-server tests hit
  `EADDRNOTAVAIL` once and passed on rerun. Ruff clean.

### Attempt 2: the baseline ran

- **Free pre-send check** (`count_tokens` only, outside the harness, all 6 requests, a placeholder
  standing in for the baseline's earlier answers): all 200, with 426,266 / 426,737 / 427,198 /
  427,654 / 428,125 / 428,589 input tokens. No reference to an unsent tool remained.
- `run.py replay --dry`: 62 messages in request 1, 12 tools, 8 betas.
- **Live replay**, `--approved 9865000 --h1-record` (may spend 6,442,012): 6/6 requests returned 200
  with `end_turn`, each 1 thinking and 1 text block; `stopped` null; `answers_file`
  `baseline-answers.txt`.

  | Request | Messages | count_tokens | Cache write | Cache read | Output |
  |---|---|---|---|---|---|
  | 1 | 62 | 426,266 | 426,364 | 0 | 2,131 |
  | 2 | 65 | 426,831 | 565 | 426,364 | 1,480 |
  | 3 | 68 | 427,385 | 427,483 | 0 | 2,076 |
  | 4 | 71 | 427,961 | 1,130 | 426,929 | 2,531 |
  | 5 | 74 | 428,527 | 1,142 | 427,483 | 1,910 |
  | 6 | 77 | 429,087 | 1,126 | 428,059 | 1,895 |

  Each request also had 27 uncached input tokens. Request 3 missed the cache and wrote the whole
  prefix again. The other 4 requests after the first read it.
- **Spent 2,578,830**: input 162, cache write 857,810, cache read 1,708,835, output 12,023.

## Compared with live run #3 and the Opus run

| Figure | Run #3 (`claude-haiku-5-5`) | Opus run (`claude-opus-5-5`) | This run (`claude-haiku-5-5`) | Cause of the change from run #3 |
|---|---|---|---|---|
| Code; client | fbb1303; 2.1.301 | 9d2949a; 2.1.302 | 2388752 (v0.5.20); 2.1.318 | product: 105 merges since fbb1303, 27 since 9d2949a (D43 to D58, D48 not merged); client: tool search on by default |
| Fidelity, H1 / baseline | 51 / 48 | 53 / 54 | 52 / 47 | product (D34, D41, B22, D51); baseline: the model's answers from the full context |
| chain, H1 / baseline | 28 / 25 | 29 / 30 | 28 / 26 | as fidelity |
| distractor | 6 / 6 | 6 / 6 | 6 / 6 | |
| multihop | 8 / 8 | 8 / 8 | 8 / 8 | |
| obligations | 5 / 5 | 6 / 6 | 6 / 3 | baseline: 1 stale, 2 set answers with 1 extra item |
| paged_only | 4 / 4 | 4 / 4 | 4 / 4 | |
| Misses, H1 / baseline | 3 / 6, all stale | 1 / 0, stale | 2 / 7 (H1 stale; baseline 5 stale, 2 other) | as fidelity |
| Recall: paged-only correct; credited | 4/4; 4 | 4/4; 1 | 4/4; 4 | no ledger pin held the answers in this run |
| `contexto_search` / `contexto_recall` calls | 78 / 0 | 38 / 0 | 72 / 0 | product search (D34, D41, B22) and tool text (D51); client: tools load through tool search |
| Spans in_view / paged | 94 / 188 | 25 / 129 | 11 / 173 | B22; D34, D41 |
| Probe-turn tool rounds | 20 (4/2/1/4/5/4) | 11 (4/1/1/2/2/1) | 19 (5/2/1/4/4/3) | client (2.1.318), product |
| Content-turn tool calls | 0 (18 requests) | 48 (47 pin, 1 state; 44 requests) | 2 (1 `ToolSearch`, 1 pin; 20 requests) | client: Contexto's tools are deferred behind tool search; D51 |
| Cache | 36/37 (0.973) | 54/55 (0.982) | 38/38 (1.0) | D36 upstream affinity on the route since run #3; no title side request on 2.1.318 |
| Max upstream input | 109,868 | 112,669 | 110,465 | |
| Messages requests; epochs; rotations | 44; 7; 6 | 62; 7; 6 | 45; 7; 6 (4 bound, 2 head_change) | client: tool search changes the head twice |
| CLI exit | 143 (spend watcher, after the last turn) | 0 | 0 | H1 cap 6,991,331 |
| H1 tokens | 3,748,084 | 4,726,388 | 3,422,988 | probe turns 493,442 less than run #3 (25 requests); content turns 168,346 more (20 requests against 18) |
| Content turns / probe turns | 1,348,490 / 2,399,594 | 3,242,892 / 1,459,473 (+ 24,023 side request) | 1,516,836 / 1,906,152 | |
| Baseline tokens | 2,634,365 | 2,749,815 | 2,578,830 (attempt 1: 0) | a smaller history; request 3 missed the cache |
| Baseline count_tokens per request | 435,590 to 438,406 over 56 to 71 messages | 456,403 to 459,219 over 134 to 149 messages | 426,266 to 429,087 over 62 to 77 messages (attempt 1: 400) | harness: `tool_reference` as a text note (PR #247) |
| **Total tokens** | **6,382,449** | **7,476,203** | **6,001,818** | |

## Spend

| Line | Tokens | Limit |
|---|---|---|
| H1 (record: driver 3,422,988, results 3,422,988, proxy log 3,422,988, in-flight 0) | 3,422,988 | cap in force 6,991,331 |
| Baseline attempt 1 (stopped at request 1's `count_tokens`) | 0 | |
| Diagnosis (4 `count_tokens` calls, outside the harness) | 0 | |
| Pre-send check (6 `count_tokens` calls, outside the harness) | 0 | |
| Baseline attempt 2 (`replay-result.json` `spent`, 6 requests) | 2,578,830 | worst 2,873,669; may spend 6,442,012 |
| **Total** | **6,001,818** | approval 9,865,000; 3,863,182 left |

- **The approval.** `run.py approval-table` at 2388752 matches the runbook's: expected 6,576,867
  (H1 3,891,202, baseline 2,685,665); worst 9,447,449 (H1 6,573,780, baseline 2,873,669). Cap
  floor(1.5 x 6,576,867) = 9,865,300, rounded down to 9,865,000, above the worst case.
- **H1 against the table:** 468,214 (12.0%) under the expected H1; 52.1% of the worst H1.
- **Baseline against the table:** 106,835 (4.0%) under the expected baseline (2,685,665); 89.7%
  of the worst baseline. Total: 575,049 (8.7%) under the expected 6,576,867.
- **H1's usage:** input 90, cache read 2,800,741, cache write 608,355, output 13,802.
- **Zero-spend checks first,** at 2388752:
  - `dry-run`: 18 content turns, 54 probes (4 paged-only), 11 rotations planned, 0 over the bound,
    history 396,207 (2.64x).
  - `check-credential` passed: `x-api-key` and `Authorization` present, the key equal to the seat
    helper's, no OAuth beta header.
  - Stub runs: clean rc 0 with 24/24 turns, 24 requests, 5 rotations, 6 epochs, max input 107,656
    (modelled), 0 over the bound, every verdict true, 4/4 paged-only plants in pages, reach 54/54,
    keys matched on 24/24; `--fault auth:3` stopped on `AuthFailure` (exit 143); `--fault raise:2`
    stopped on the injected `ValueError`; both wrote `.partial` answers. `check-paths`:
    `unexercised: []`.
  - `permstub.py --claude-version 2.1.318`: 5 of 5 Contexto tools ran, 0 refused, 5 `mcp_call`s.

## Figures from the Claude Code transcript

Read from the Claude Code transcript this run created. The baseline has now run from it. The
transcript stays private until the record merges, and is deleted after that.

| Figure | Value |
|---|---|
| CLI-side compaction scan | 0 `compact_boundary`, 0 `isCompactSummary` |
| `api-request` records against proxy requests | 45 against 45 |
| Request shapes | 3: 12, 13 and 15 tools |
| `ToolSearch` calls | 3: 1 in content turn 5, 2 in probe turn 1 |
| `tool_reference` blocks | 6 in all; 1 before probe turn 1 (`contexto_pin`) |
| Tool calls by name | `contexto_search` 72, `contexto_pin` 1, `ToolSearch` 3, `Bash` 2 |
| Recorded betas | 9 (the replay sends 8: no `claude-code-*` beta) |

## Off-path events

- `HEAD /api/hello` returned 401 once: Claude Code's unauthenticated connectivity probe, on a
  non-model path, as in earlier runs.
- 1 `estimate_error` event, on the first request named above, reported only.
- 1 `carried_text_growing` event (content turn 3, epoch 0) and 1 `epoch_not_resumed` (epoch 0, no
  record), both informational.
- `cli_version` logged 2.1.318 as tested.
