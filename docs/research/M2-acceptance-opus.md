# M2 acceptance on Claude Opus 5.5: the record (2026-10-25)

This is the full M2 acceptance run, D27 protocol, on `claude-opus-5-5` instead of
`claude-haiku-5-5`, at the default bound 150,000, through the owner's proxy. Everything else follows
live run #3 ([M2-acceptance-run3.md](M2-acceptance-run3.md)). The owner approved the run on
2026-10-25. The record gives numbers, ids, counts and booleans only. No prompts, answers, page text or
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
assignment are identical to run #3's: from fbb1303 to 9d2949a, `spikes/m2/acceptance/script.py` and
the probe generator in `spikes/m0/fidelity/` changed only in lint. A new seed would add a content
change to the comparison. The content is the only thing held constant. The other changes are listed
under "Compared with live run #3".

**Model.** The harness pins its model in `driver.MODEL`. A wrapper outside the repository set it
to `claude-opus-5-5` at run time; no file in the checkout changed. The replay takes the model
from the H1 request Claude Code recorded, which was `claude-opus-5-5`.

## Departures from the contract

The contract runs acceptance with `claude-haiku-5-5` pinned and on **default Claude Code settings**
(`docs/milestones/M2-contexto-proxy.md`, Acceptance). This run departs from it in two ways:

1. **The model:** `claude-opus-5-5`. This is the change the owner approved for this run.
2. **The permission rule:** the passing run added `"permissions": {"allow": ["mcp__contexto"]}` to
   the isolated Claude Code settings. **On default settings, Claude Code 2.1.302 fails the run.**
   Attempt 1 ran on defaults:
   - every Contexto tool call was refused;
   - H1 scored 18/54 and nothing was credited;
   - every acceptance 1 verdict stayed true, so acceptance 1's detectors and failure events do not
     catch it.

   See "Attempt 1 and the fixes". A product fix is in progress (D44: the launcher allows
   Contexto's own tools). It was not merged when this record was written.

The verdicts below hold for the verdict run with the allow rule.

## Verdicts

| Acceptance | Verdict | Figures |
|---|---|---|
| 1. Long run | PASS | See the first table below |
| 2. Fidelity | PASS | H1 53/54 vs baseline 54/54; overall gap 1; chain 1 below, every other category level |
| 3. Recall | PASS | Paged-only 4/4 answered correctly; q_p_03 had a paged `contexto_search` span returned before its answer. None of the four was answerable only from paged-out content (see below) |
| 4. Cache | PASS, the one miss explained | 54/55 (0.982); the one miss, request 2, is the main conversation's cold first write, not a route miss |
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

There were 62 Messages requests, all 200, and 7 epochs.
- **Rotations:** all 6 were `min_turn_room` (A12), at content turns 4, 7, 10, 13 and 16 and at
  probe turn 2. There was no `window_minimal`.
- **Requests:** 1 title side request, 44 in content turns and 17 in probe turns.
- **Pre-usage estimates.** Two requests went out before any usage arrived. Both are far under the
  135,000 of the 10 percent rule ([M2-acceptance-harness.md](M2-acceptance-harness.md), "Pre-usage
  estimates"):
  - the title side request: estimate 12,484 against a real 24,001, ratio 0.52 (the run's
    `estimate_min_ratio`);
  - the main conversation's first request: 33,151 against 50,341, 34% low.
- **Exit:** the CLI exited 0.

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

- **What the contract needs.** Acceptance 3 needs at least one paged-only probe answered correctly,
  with the session log showing a `contexto_search` or `contexto_recall` return of its paged line
  before the answer. q_p_03 meets that: one search call returned a paged span covering its plant
  before its probe turn's answer, under run #3's cutoff rule. That is why the PASS stands.
- **The pin.** In content turn 1, while the plants were still in view, the model pinned a ledger
  record of kind `event`. It holds all four paged-only config keys with their values, q_p_03's
  included (checked by substring match, booleans only). Pinned records stay in the window.
- **What follows.** None of the four was answerable only from paged-out content in this run, unlike
  run #3, whose content turns made no tool calls. For q_p_03 the log cannot show whether the model
  used the search span or the pin. q_p_01, q_p_02 and q_p_04 were answered correctly and not
  credited: no search or recall returned their lines. That is consistent with the pin; the path is
  inferred.

**Acceptance 4:** the one miss is request 2, the main conversation's first request. It wrote the
cache (50,339 tokens) because nothing was cached yet. It counts as eligible only because Claude
Code's title side request came first and opened epoch 0. That explains the miss: a cold first write,
not a route miss. Every later request read the cache.

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

**Content turns.** The model called Contexto's tools in every content turn:
- 47 `contexto_pin` calls: 35 recorded and 12 refused with `ToolError`. The 10 / 2 split of the
  refusal reasons is in the table of transcript-only figures below.
- 1 `contexto_state` call.

Each content turn took 2 to 4 requests, 44 in all. Run #3's content turns made no tool calls: 18
requests over 18 content turns.

## Compared with live run #3

The content is identical. Four other things changed between the two runs, so no difference below
can be put down to the model alone:

- **The product.** From fbb1303 to 9d2949a there are 80 commits, 77 of them pull-request merges,
  and +4,506 / -473 lines in `src/contexto`. Among them:
  - D34: `contexto_search` moved from an exact case-sensitive substring match to every word in any
    case, ranked by BM25. Its tool description and no-hit text changed with it.
  - D41: ranking changes.
  - B22 (#145): in-view search skips the current turn's own question.
  - D39: the search text cache.
  - D32 and D33: bounded carried content.
  - A28: cache-aware rotation.
  - A33 and A34: headroom rules.
- **The client.** Claude Code 2.1.301 became 2.1.302:
  - it sends a title side request;
  - it no longer uses auto mode for tools in `-p`;
  - it records 8 betas, not 10.
- **The baseline's input.** The baseline replays H1's own transcript, and here that transcript
  holds the pin rounds: 134 to 149 messages per request against run #3's 56 to 71, about 78 more.
  So the baseline is not a clean model comparison either.
- **The route.** Upstream affinity (D36) was deployed on the owner's proxy after run #3.

| Figure | Run #3 (`claude-haiku-5-5`) | This run (`claude-opus-5-5`) | Changed with the model |
|---|---|---|---|
| Code; client | fbb1303; 2.1.301 | 9d2949a; 2.1.302 | - |
| Fidelity, H1 / baseline | 51 / 48 | 53 / 54 | H1: product (D34, D41, B22, D32/D33, rotation and headroom rules), client. Baseline: replayed transcript with the pin rounds |
| chain, H1 / baseline | 28 / 25 | 29 / 30 | as fidelity |
| distractor | 6 / 6 | 6 / 6 | as fidelity |
| multihop | 8 / 8 | 8 / 8 | as fidelity |
| obligations | 5 / 5 | 6 / 6 | as fidelity |
| paged_only | 4 / 4 | 4 / 4 | as fidelity |
| Misses, H1 / baseline | 3 / 6, all stale | 1 / 0, stale | as fidelity |
| Recall: paged-only correct; credited | 4/4; 4 | 4/4; 1 | search (D34, D41, B22); this run's turn-1 pin also held all four answers |
| `contexto_search` / `contexto_recall` calls | 78 / 0 | 38 / 0 | search matching and ranking (D34, D41) and its tool text; B22 |
| Spans in_view / paged | 94 / 188 | 25 / 129 | B22 removes the current question's own lines from in-view hits; D34, D41 |
| Probe-turn tool rounds | 20 (4/2/1/4/5/4) | 11 (4/1/1/2/2/1) | D34, D41, B22 |
| Content-turn tool calls | 0 (18 requests over 18 content turns) | 48 (47 pin, 1 state; 44 requests) | client (2.1.302), product tool descriptions |
| Cache | 36/37 (0.973); the miss route-side | 54/55 (0.982); the miss a cold first write | client title request; A28; D36 on the route |
| Max upstream input | 109,868 | 112,669 | pin rounds in content turns; rotation and headroom rules (A28, A33/A34) |
| Messages requests; epochs; rotations | 44; 7; 6 | 62; 7; 6 | pin rounds, title request, probe rounds |
| CLI exit | 143 (spend watcher, after the last turn) | 0 | H1 cap (4,007,279 against 6,567,279) |
| H1 tokens | 3,748,084 | 4,726,388 | as requests |
| Content turns / probe turns | 1,348,490 / 2,399,594 | 3,242,892 / 1,459,473 (+ 24,023 side request) | as requests |
| Baseline tokens | 2,634,365 | 2,749,815 | replayed transcript size |
| Baseline count_tokens per request | 435,590 to 438,406 over 56 to 71 messages | 456,403 to 459,219 over 134 to 149 messages | replayed transcript size |
| **Total tokens** | **6,382,449** | **7,476,203** (10,097,561 with attempt 1) | |

- **Fidelity.** Both arms scored higher: H1 51 to 53, baseline 48 to 54. In run #3 H1 led its
  baseline by 3; here the baseline leads by 1, inside the pass rule. With the product, the client
  and the replayed transcript all changed alongside the model, this is one measurement per
  configuration, not a model comparison.
- **Recall and search.** This run made fewer searches, fewer probe-turn rounds and fewer in-view
  spans. The search tool itself changed between the runs (D34, D41, B22), so these counts are not
  attributed to the model. Fewer paged-only answers were credited, and all four were correct.
- **Tokens.** H1 cost 978,304 more than run #3:
  - Content turns cost 1,894,402 more, with 44 content requests against 18. Each tool round
    re-sends the window.
  - Probe turns cost 940,121 less, with 11 rounds against 20.
  - H1 wrote 23,755 output tokens.

## Spend

| Line | Tokens | Limit |
|---|---|---|
| H1 (record: driver 4,702,365, proxy log 4,726,388, in-flight 0) | 4,726,388 | cap in force 6,567,279 |
| Baseline (6 requests, each 200 end_turn) | 2,749,815 | worst 2,872,721; may spend 4,713,612 |
| **Total** | **7,476,203** | approval 9,440,000 |
| Attempt 1, H1 only (not a verdict) | 2,621,358 | its own approval 9,440,000 |
| **All live spend for this task** | **10,097,561** | |

- **The approval.** The approval table at this main has no model term. It gives:
  - expected 6,565,607: H1 3,880,890, baseline 2,684,717;
  - worst 9,433,809: H1 6,561,088, baseline 2,872,721.

  The approval was the worst case rounded up to 10,000, as in run #3: 9,440,000, under the
  25,000,000 limit for this task.
- **H1 against the table:** 845,498 (21.8%) over the expected H1, and under the worst H1.
- **H1's usage:** input 24,123, cache read 4,120,522, cache write 557,988, output 23,755.
- **Zero-spend checks first,** at 9d2949a with the model set to Opus:
  - `check-credential` passed: the key was present and equal to the seat helper's, with no OAuth
    beta header.
  - The stub dry runs (clean, `--fault auth:3`, `--fault raise:2`) took every live-only path:
    `check-paths` left none unexercised.
  - The clean run had 48 requests, 0 over the bound, max input 115,649 (modelled), 4/4 paged-only
    plants in pages, and reach 54/54.

## Attempt 1 and the fixes

**Attempt 1** (`acc20261025T015440Z`, 01:54:40Z to 01:56:59Z) ran on default Claude Code settings.
- **Acceptance 1:** all 24 turns ran with every verdict true. There were 35 requests, max input
  105,506, 5 rotations, 6 epochs, cache 28/29 and no failure events.
- **The tools:** Claude Code refused every Contexto tool call before it reached the server. There
  were 23 calls, each answered "Claude requested permissions to use ... but you haven't granted it
  yet", and the session log has 0 `mcp_call` events.
- **Fidelity:** without its tools, H1 scored 18/54. No baseline was run for it.

- **Cause (zero-spend stub matrix).** A local stub answered the first main request with a
  `mcp__contexto__contexto_state` call, through `contexto run claude -p` with an isolated config:
  - Claude Code 2.1.301, either model, no allow rule: the call went to auto mode, which asks a model
    whether the tool is safe. The stub cannot answer that, so the call failed there. Run #3 logged
    78 successful searches on 2.1.301, so its calls most likely ran through auto mode [inferred].
  - Claude Code 2.1.302, either model, no allow rule: the call was denied as unpermitted.
  - Both versions, both models, with the allow rule `mcp__contexto`: the call ran and the server
    logged an `mcp_call`.

  So it is a Claude Code 2.1.302 change in `-p` mode, not a model effect.
- **The retry.** The isolated config's settings gained `"permissions": {"allow": ["mcp__contexto"]}`
  beside the key helper, through the same wrapper. Contexto, the launcher and the harness files are
  unchanged. This is the departure from default settings stated at the top. The run was retried
  once, and that is the verdict run.
- **Product bug** (fix in progress as D44, not merged at the time of writing). It affects
  `contexto run claude -p` on Claude Code 2.1.302, the only Claude Code version in
  `TESTED_VERSIONS`.
  - The launcher's settings set only `ANTHROPIC_BASE_URL`, with no permission rule.
  - Without the user's own allow rule, every Contexto tool call is refused. Contexto still pages
    old turns out, so recall fails silently.
  - Neither `mcp_tools_missing` nor any failure event fires, since the tools are listed and the CLI
    refuses each call.
  - In an interactive session the CLI may ask the user instead. That was not tested [untested].

**Two scoring corrections for Claude Code 2.1.302.** These are harness errors, not product
failures. Both are applied at run time by scripts outside the repository, and no harness file
changed.

1. **Turn ends.** `run.py check` found 25 turn answers against 24 expected and so credited nothing.
   - **Cause:** Claude Code 2.1.302 sends one tool-less title request at the start, with 1 message.
     It ends no user turn.
   - **Correction:** acceptance 3 above is `recall_credit.credit` exactly as `check` calls it, with
     `turn_ends` passed explicitly: the harness's `turn_answer_indices` minus every request whose
     `original_message_count` is 1. That removes 1 of 25. Attempt 1 had the same single title
     request.
2. **Transcript decoder.** `run.py replay` refused: "a request record names a message the JSONL
   does not hold".
   - **Cause:** Claude Code 2.1.302 writes each parallel tool result as its own JSONL record but
     sends them as one user message.
   - **Correction:** the baseline used `replay._known_messages` plus one addition. For each run of
     consecutive user records holding only `tool_result` blocks, the merged message of each prefix
     of the run is also registered by its hash. It only adds candidates, so the replay still sends
     exactly the messages the CLI recorded.

**Open item:** move both corrections into the harness (`recall_credit` turn ends, `replay`
decoder). Until then, `run.py check` reports `recall_credited: false` on a 2.1.302 run, and
`run.py replay` refuses one with parallel tool results.

## Figures from the Claude Code transcripts (not kept)

These figures were read from the Claude Code session transcripts that this run created. The
transcripts were deleted after scoring, so the figures cannot be re-derived:

| Figure | From |
|---|---|
| CLI-side compaction scan: 0 `compact_boundary` and 0 `isCompactSummary` | the verdict run's transcript; the H1 record's 0 stream compactions backs it |
| 61 `api-request` records against 62 proxy requests (title request absent) | the verdict run's transcript |
| Decoder: 182 message hashes, 31 unresolved before the correction and 0 after; attempt 1: 0 and 0 | both transcripts |
| Pin refusal reasons: 10 for a verified record without a provenance source, 2 for an unknown source type | the verdict run's transcript; the session log has only `ToolError` |
| Attempt 1's 23 refused calls by tool: 10 search, 10 pin, 3 state | attempt 1's transcript |
| `permissionMode` `default` on 2.1.302 | attempt 1's transcript and the stub matrix |
| Recorded betas: 8 | the replay result, built from the verdict run's transcript |

## Off-path events

- `HEAD /api/hello` returned 401 once. This is Claude Code's unauthenticated connectivity probe, on
  a non-model path, as in earlier runs.
- 2 `estimate_error` events, on the two pre-usage requests named above, reported only.
- The replay's recorded betas were 8, not run #3's 10: 2.1.302 sent neither
  `afk-mode-2026-01-31` nor `dangerous-tool-use-2026-09-03`. The replay changed `thinking.display`
  from `updates` to `omitted`, as before.
