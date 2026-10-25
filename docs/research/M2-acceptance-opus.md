# M2 acceptance on Claude Opus 5.5: the record (2026-10-25)

This is the full M2 acceptance run, D27 protocol, on `claude-opus-5-5` instead of
`claude-haiku-5-5`, at the default bound 150,000, through the owner's proxy. Everything else follows
live run #3 ([M2-acceptance-run3.md](M2-acceptance-run3.md)). The owner approved the run on
2026-10-25. It gives numbers, ids, counts and booleans only. No prompts, answers, page text or
transcripts are recorded here. The run's artifacts stay private, outside any repository.

## Code and session

| Item | Value |
|---|---|
| Code | main `9d2949a2df1e955a78585649e50a8e9ef2490262` (v0.5.1 code), a clean detached checkout |
| Session (the verdict) | `acc20261025T020104Z` |
| Session (attempt 1, not a verdict) | `acc20261025T015440Z` |
| Client and model | Claude Code 2.1.302, `claude-opus-5-5` |
| Bound | 150,000 |
| Probes | D27: 6 probe turns of 9, seed 20261016, 18 content turns of 40 KB |
| H1 | 02:01:04Z to 02:06:11Z |
| Baseline | 02:08:04Z to 02:08:47Z |

**Seed.** The same seed as run #3, 20261016. The content turns, the 54 probes and the D27
assignment are then identical to run #3's, so the model (and the client version) is what changed,
and each category compares directly. A new seed would mix a content change into the comparison.

**Model.** The harness pins its model in `driver.MODEL`. A wrapper outside the repository set it
to `claude-opus-5-5` at run time; no file in the checkout changed. The replay takes the model
from the H1 request Claude Code recorded, which was `claude-opus-5-5`.

## Verdicts

| Acceptance | Verdict | Figures |
|---|---|---|
| 1. Long run | PASS | See the first table below |
| 2. Fidelity | PASS | H1 53/54 vs baseline 54/54; overall gap 1; chain 1 below, every other category level |
| 3. Recall | PASS | Paged-only 4/4 answered correctly; 1 of them (q_p_03) credited through a paged `contexto_search` span |
| 4. Cache | 54/55 (0.982) | One miss, at Messages request 2 |
| 5. Tests | PASS | 2065 passed, 1 skipped, 1 xfailed at 9d2949a (macOS, Python 3.13); CI green on 9d2949a |

**Acceptance 1, every verdict:**

| Verdict | Result | Figure |
|---|---|---|
| zero_compaction | T | 0 compaction events, 0 stream compactions |
| no_request_over_bound | T | max input 112,669 |
| no_pre_send_over_bound_outside_tool_loop | T | none |
| no_real_over_bound | T | 0 |
| no_failure_events | T | `failure_events` {}: `retained_stale` 0, `turn_room_low` 0 |
| no_bound_too_small | T | 0 |
| every_request_has_usage | T | 62/62 |
| zero_4xx | T | model-path 4xx 0 |
| no_local_5xx | T | local 5xx none; upstream 5xx none |

There were 62 Messages requests, all 200, and 7 epochs. All 6 rotations were `min_turn_room` (A12),
at content turns 4, 7, 10, 13 and 16 and at probe turn 2; there was no `window_minimal`. The
requests were 1 title side request, 44 in content turns and 17 in probe turns. The pre-usage
request's estimate was 33,151 against a real 50,341 (34% low), far under the 135,000 of the 10
percent rule. The CLI exited 0.

**Acceptance 2, per category (both arms, correct):**

| Category | H1 | Baseline |
|---|---|---|
| chain | 29/30 | 30/30 |
| distractor | 6/6 | 6/6 |
| multihop | 8/8 | 8/8 |
| obligations | 6/6 | 6/6 |
| paged_only | 4/4 | 4/4 |
| **Total** | **53/54** | **54/54** |

The pass rule is an overall gap of at most 2 and no category more than 1 below the baseline. The
overall gap is 1 and chain is 1 below.

**Acceptance 3:** `recall_credited` is true (with the turn-end correction below). The run made 38
`contexto_search` calls and 0 `contexto_recall` calls. Search returned 25 `in_view` spans and 129
paged spans; no `in_view` span credits.
- q_p_03 is credited through one search call whose paged span covers its plant.
- q_p_01, q_p_02 and q_p_04 were answered correctly but not credited: no search or recall returned
  their plant lines. In content turn 1, while the plants were in view, the model pinned a ledger
  record of kind `event` that holds all four paged-only config keys with their values (checked by
  substring match, booleans only). Pinned records stay in the window, so that is consistent with
  how the three answers reached the model, through Contexto's pin ledger. The credit rule counts
  only recall and search returns, so they are not credited.

**Acceptance 4:** the one miss is request 2, the main conversation's first request. It wrote the
cache (50,339 tokens) because nothing was cached yet. It counts as eligible only because Claude
Code's title side request came first and opened epoch 0. Every later request read the cache.

## Misses (ids only)

| Probe | Category, tier | Probe turn | Kind |
|---|---|---|---|
| H1 only: q_a_014 | chain, depth6-previous | 5 | stale |

The baseline missed none.

## Probe turns (H1)

The room is taken at each probe turn's first request, as in run #3: bound 150,000 - input - 200 -
25,000.

| Probe turn | Room | Requests | Searches | Spans in_view / paged | Epoch | Rotation at start |
|---|---|---|---|---|---|---|
| 1 | 18,959 | 5 | 12 | 6 / 44 | 5 | none |
| 2 | 60,519 | 2 | 6 | 3 / 19 | 6 | min_turn_room |
| 3 | 55,819 | 2 | 4 | 1 / 8 | 6 | none |
| 4 | 53,252 | 3 | 8 | 8 / 28 | 6 | none |
| 5 | 47,064 | 3 | 6 | 4 / 26 | 6 | none |
| 6 | 42,098 | 2 | 2 | 3 / 4 | 6 | none |

Every room is at least `min_turn_room` (12,000), so `turn_room_low` is 0. The probe turns took 11
tool-loop rounds after their first request (4/1/1/2/2/1).

**Content turns.** Unlike run #3, the model called Contexto's tools in every content turn: 47
`contexto_pin` calls (35 recorded, 12 refused with `ToolError`: 10 for a verified record without a
provenance source, 2 for an unknown source type) and 1 `contexto_state` call. Each content turn
took 2 to 4 requests, 44 in all, against 18 in run #3.

## Compared with live run #3

| Figure | Run #3 (`claude-haiku-5-5`) | This run (`claude-opus-5-5`) |
|---|---|---|
| Code; client | fbb1303; 2.1.301 | 9d2949a; 2.1.302 |
| Fidelity, H1 / baseline | 51 / 48 | 53 / 54 |
| chain | 28 / 25 | 29 / 30 |
| distractor | 6 / 6 | 6 / 6 |
| multihop | 8 / 8 | 8 / 8 |
| obligations | 5 / 5 | 6 / 6 |
| paged_only | 4 / 4 | 4 / 4 |
| Misses, H1 / baseline | 3 / 6, all stale | 1 / 0, stale |
| Recall: paged-only correct; credited | 4/4; 4 | 4/4; 1 |
| `contexto_search` / `contexto_recall` calls | 78 / 0 | 38 / 0 |
| Spans in_view / paged | 94 / 188 | 25 / 129 |
| Probe-turn tool rounds | 20 (4/2/1/4/5/4) | 11 (4/1/1/2/2/1) |
| `contexto_pin` calls | not recorded | 47 (35 ok) |
| Cache | 36/37 (0.973) | 54/55 (0.982) |
| Max upstream input | 109,868 | 112,669 |
| Messages requests; epochs; rotations | 44; 7; 6 | 62; 7; 6 |
| CLI exit | 143 (spend watcher, after the last turn) | 0 |
| H1 tokens | 3,748,084 | 4,726,388 |
| Content turns / probe turns | 1,348,490 / 2,399,594 | 3,242,892 / 1,459,473 (+ 24,023 side request) |
| Baseline tokens | 2,634,365 | 2,749,815 |
| Baseline count_tokens per request | 435,590 to 438,406 over 56 to 71 messages | 456,403 to 459,219 over 134 to 149 messages |
| **Total tokens** | **6,382,449** | **7,476,203** (10,097,561 with attempt 1) |

- **Fidelity.** Opus scored higher on both arms. H1 rose from 51 to 53 and the baseline from 48 to
  54. Run #3's H1 led its baseline by 3; here the baseline leads by 1, inside the pass rule. One run
  on each model, so these are measurements, not claims about the models in general.
- **Recall.** Opus searched half as often and in fewer rounds, and pinned in every content turn.
  Three of the four paged-only answers came from its own early pin rather than from search, so
  fewer are credited, while all four are correct.
- **Tokens.** H1 cost 978,304 more than run #3. Content turns cost 1,894,402 more, because each pin
  round re-sends the window (44 content requests against 18). Probe turns cost 940,121 less (11
  rounds against 20). Opus wrote 23,755 output tokens in H1.

## Spend

| Line | Tokens | Limit |
|---|---|---|
| H1 (record: driver 4,702,365, proxy log 4,726,388, in-flight 0) | 4,726,388 | cap in force 6,567,279 |
| Baseline (6 requests, each 200 end_turn) | 2,749,815 | worst 2,872,721; may spend 4,713,612 |
| **Total** | **7,476,203** | approval 9,440,000 |
| Attempt 1, H1 only (not a verdict) | 2,621,358 | its own approval 9,440,000 |
| **All live spend for this task** | **10,097,561** | |

- The approval table at this main gives expected 6,565,607 and worst 9,433,809 (H1 3,880,890 /
  6,561,088, baseline 2,684,717 / 2,872,721); the table has no model term. The approval was the worst
  case rounded up to 10,000, as in run #3: 9,440,000, under the 25,000,000 limit for this task.
- H1 was 845,498 (21.8%) over the expected H1 and under the worst H1.
- H1's usage: input 24,123, cache read 4,120,522, cache write 557,988, output 23,755.
- Zero-spend checks first, at 9d2949a with the model set to Opus: `check-credential` passed (key
  present and equal to the seat helper's, no OAuth beta header). The stub dry runs (clean,
  `--fault auth:3`, `--fault raise:2`) took every live-only path (`check-paths`: none unexercised);
  the clean run had 48 requests, 0 over the bound, max input 115,649 (modelled), 4/4 paged-only
  plants in pages, and reach 54/54.

## Attempt 1 and the fixes (not a verdict)

**Attempt 1** (`acc20261025T015440Z`, 01:54:40Z to 01:56:59Z) ran all 24 turns with every
acceptance 1 verdict true: 35 requests, max input 105,506, 5 rotations, 6 epochs, cache 28/29, no
failure events. But every Contexto tool call was refused by Claude Code before it reached the
server: 23 calls (10 search, 10 pin, 3 state), each answered "Claude requested permissions to use
... but you haven't granted it yet", and the session log has 0 `mcp_call` events. Without its tools
H1 scored 18/54. No baseline was run for it.

- **Cause (zero-spend stub matrix).** A local stub answered the first main request with a
  `mcp__contexto__contexto_state` call, through `contexto run claude -p` with an isolated config:
  - Claude Code 2.1.301, either model, no allow rule: the call went to auto mode, which asks a model
    whether the tool is safe (the stub cannot answer that, so the call failed there). Run #3 logged
    78 successful searches on 2.1.301, so its calls most likely ran through auto mode [inferred].
  - Claude Code 2.1.302, either model, no allow rule: the call was denied as unpermitted.
  - Both versions, both models, with the allow rule `mcp__contexto`: the call ran and the server
    logged an `mcp_call`.
  So it is a Claude Code 2.1.302 change in `-p` mode, not a model effect. Attempt 1's transcript
  records `permissionMode` `default`.
- **Fix, setup only.** The isolated config's settings gained `"permissions": {"allow":
  ["mcp__contexto"]}` beside the key helper, through the same wrapper. Contexto, the launcher and
  the harness files are unchanged. The run was retried once: the verdict run above.
- **Product follow-up.** On Claude Code 2.1.302, `contexto run claude -p` gets Contexto's tools
  refused unless the user allows `mcp__contexto`, and the proxy's tool check does not report it
  (the tools are listed; the CLI refuses each call). In an interactive session the CLI asks the user
  instead. Whether the launcher should add the allow rule to its own `--settings`, or the
  documentation should say so, is the owner's call.

**Two scoring corrections for Claude Code 2.1.302** (harness errors, not product failures). Both
are applied at run time by scripts outside the repository; no harness file changed.

1. **Turn ends.** `run.py check` found 25 turn answers against 24 expected and so credited nothing.
   Claude Code 2.1.302 sends one tool-less title request at the start, with 1 message; it is not in
   the CLI's own `api-request` records (61 against 62 proxy requests), and it ends no user turn.
   Acceptance 3 above is `recall_credit.credit` exactly as `check` calls it, with `turn_ends` passed
   explicitly: the harness's `turn_answer_indices` minus every request whose
   `original_message_count` is 1. That removes 1 of 25; attempt 1 had the same single title request.
2. **Transcript decoder.** `run.py replay` refused ("a request record names a message the JSONL
   does not hold"). Claude Code 2.1.302 writes each parallel tool result as its own JSONL record
   but sends them as one user message. The baseline used `replay._known_messages` plus one
   addition: for each run of consecutive user records holding only `tool_result` blocks, the merged
   message of each prefix of the run is also registered by its hash. It only adds candidates. On
   this run's JSONL: 61 request records, 182 message hashes, 31 unresolved before and 0 after. On
   attempt 1's: 0 before and 0 after.

## Off-path events

- `HEAD /api/hello` returned 401 once, Claude Code's unauthenticated connectivity probe on a
  non-model path, as in earlier runs.
- 2 `estimate_error` events, on the title request and the first main request (pre-usage), reported
  only.
- The replay's recorded betas were 8, not run #3's 10: 2.1.302 sent neither
  `afk-mode-2026-01-31` nor `dangerous-tool-use-2026-09-03`. The replay changed `thinking.display`
  from `updates` to `omitted`, as before.
