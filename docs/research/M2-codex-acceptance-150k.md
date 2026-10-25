# M2 Codex acceptance at the default bound 150,000, live

Step 1 of the Codex acceptance plan (`docs/research/M2-codex-acceptance-plan.md`), the H1 arm alone
for acceptance 1 and 3, run live at the shipped default bound of 150,000 instead of D30's 64,000.
Everything else follows `docs/research/M2-codex-step1-live.md` (the 64,000 run): the same entry
point, model, effort, turn size, seed, pins and stop rules. The owner approved the run on
2026-10-25. Acceptance 2 is settled as D30 settled it (below).

Numbers and event counts only. No prompts, answers, page text or transcripts. The run's artifacts
stay private, outside any repository.

## Run conditions

| | |
|---|---|
| Code | main `9d2949a2df1e955a78585649e50a8e9ef2490262`, tree `c688622fb57d09c57fc237a81a9a58d9f12848e5`, a clean detached checkout |
| Entry point | `spikes/m2/acceptance/codex_live.py` |
| Codex | codex-cli 0.163.0 (the proxy logged it as a tested version), `gpt-6-astra`, reasoning effort low |
| Engine | bound **150,000**, 20 KB turns, seed 20261016 |
| Auth | API-key mode through the owner's proxy; the key from the seat's own key helper, read at call time, in no file |
| Homes | temporary `CONTEXTO_HOME` and `CODEX_HOME` outside the repository |
| Codex pins | `RETRY_PIN` (request and stream retries 0); `QUIET_PIN` (`analytics.enabled=false`, `features.plugins=false`) |
| Approval | 14,014,001 tokens, equal to codex_budget's step-1 worst case at 150,000 |
| Time | 2026-10-25 01:52:56Z to 01:57:15Z |

Command, from the checkout's root (the seat settings path shortened):

```
uv run python spikes/m2/acceptance/codex_live.py "$PWD/.venv/bin/contexto" <out dir> \
    --upstream <owner's proxy base URL> \
    --key-helper "python3 <checkout>/spikes/m2/acceptance/keyhelper.py <seat>/.claude/settings.local.json" \
    --approved 14014001 --bound 150000
```

### The history target: 375,000

Acceptance 1 (D27) needs a history of at least 2.5 times the bound. At 64,000 that was 160,000. At
150,000 it is 2.5 × 150,000 = **375,000**. `codex_budget.history_turns(150000, 20000)` picks the
fewest 20 KB content turns that reach it: **34** (against 15 at 64,000), for 375,922 harness-priced tokens. The
turn size stays at 20 KB, so only the turn count changes. Both runs count history the same way, in
harness-priced tokens at the dense 1.8 bytes per token.

In real tokens the history is smaller:
- **Measured:** each 20 KB content turn added 6,866 real input tokens on average over requests 1 to
  14, before the first rotation.
- **Derived from that growth:** 34 × 6,866 ≈ 233,000 real tokens of content, 1.56 times the bound.
  For the 64,000 run, 15 × 6,866 (this run's growth) ≈ 103,000, 1.61 times its bound.
- **Modeled, not measured:** what plain Codex would have sent. Over requests 1 to 14 each added
  token cost 2.978 bytes; this is the marginal ratio, while whole requests there ran at 3.2 to 4.3
  bytes per token. Starting from request 14's measured 107,046 tokens and adding the bytes beyond
  request 14's body at 2.978 bytes per token, Codex's last request (832,493 bytes before Contexto
  rewrote it) comes to about 271,766 tokens. By the same model, plain Codex would first pass its
  compaction trigger of 244,800 at the 34th content turn (about 246,100), just before the probe
  turns. Codex compacts from the `total_tokens` the upstream returns, so this is an estimate of when.
- Sources for the two limits: the 244,800 trigger and the 272,000 window of `gpt-6-astra` are in
  `docs/research/M0-codex-measurements.md` section 5, `docs/research/M2-codex-adapter.md`, and
  `codex_budget.CODEX_COMPACT_AT` and `CODEX_WINDOW`; D30 gives "about 245,000 of a 272,000 window".

### Zero-spend dry run first

`codex_dryrun.py --bound 150000` against the local stub, same commit: 40 of 40 turns, every Codex
exit 0, 46 POSTs all 200, largest input 109,488 stub tokens, 3 rotations (`min_turn_room`),
`acceptance_1` true, `acceptance_3` true (4 of 4 paged-only credited), 54 of 54 (stub answers).

### Worst case

`codex_budget.staged` at 150,000:

| Step | Requests | Worst case |
|---|---|---|
| 1. H1 alone | 34 + 6 × (1 + 5) = 70, each at most 182,000 | 70 × 182,000 × 1.10, which `codex_budget.staged` rounds up from the float product to **14,014,001** |
| 2. Baseline replay | 6 | 2,915,445; `replay_fits` false (below) |

The run brief for this run set a stop rule: report instead of running if the worst case passed
25,000,000 tokens. Step 1's 14,014,001 is under it, and it is the approval and stop cap the run used.

## Result

The run completed: exit 0, 40 of 40 turns (34 content, 6 probe), every Codex exit 0, no failed
turn, no watcher stop, no stop rule fired.

**Acceptance 1: true.**

| | |
|---|---|
| History | 375,922 harness-priced tokens against the 375,000 needed (2.5 times the bound); about 233,000 real, derived above |
| Compaction | none: Codex's rollout holds no `compacted` record |
| Requests | 61 POST `/v1/responses`, all 200, all with `end_event` completed; 0 4xx |
| Usage | on 61 of 61 requests |
| Bound | largest input 108,565 against 150,000 (72.4%); 0 `over_bound` real; no failure events |
| Rotations | 0 for size (`bound`) and 3 for room (`min_turn_room`), at content turns 15, 23 and 31; they paged content turns 1 to 8, 9 to 16, and 17 to 24, 8 each time |
| Window | `window_budget` 106,597 to 107,598; fixed overhead 17,402 to 18,403; 13 messages retained at each rotation (the turn being sent and the 6 before it) |
| In view | 7 to 14 content turns: 7 right after each rotation, growing to 14 before the next |
| Pages | 25: 24 hold content turns 1 to 24, and 1 is empty (the engine's page turn 0, with 0 messages). Page turn indices are 0-based, so page turn n is content turn n |
| Rewritten | 47 of 61 requests, every request from the 15th on |

**Acceptance 3: true** (`codex_recall_credit`, as recorded by the harness; main includes B30's
earliest-page fix, so no rescore is needed).

| | |
|---|---|
| Paged-only credited | 4 of 4 (`q_p_01` to `q_p_04`) |
| Credited | 22, with expected_turns 40 |
| Credited per probe turn | 4, 3, 2, 3, 5, 5 |
| Answered correctly | 54 of 54; 32 of them not credited |
| In-view spans | 14 (never credited) |
| Credited via | `contexto_recall` + `contexto_search` 21, `contexto_search` alone 1 |

- All 4 plants sit on one page, the first content turn's page, which the first rotation paged out.
  No other page holds them.
- The 22 credited are the 4 paged-only probes and 18 `q_a` probes: `q_a_001` to `q_a_008`,
  `q_a_014`, `q_a_015`, `q_a_023`, `q_a_034`, `q_a_039`, `q_a_042`, `q_a_043`, `q_a_044`,
  `q_a_045`, `q_a_049`.

**Acceptance 2: passes by D30's arithmetic. No baseline was run.**

- H1 answered 54 of 54, so no baseline can score above it overall or in any category. The
  contract's test (within 2 overall, no category more than 1 below) holds against any baseline.
  That is how D30 settled acceptance 2 at 64,000.
- D30 also skipped step 2. At 150,000, step 2 does not fit as `codex_budget` prices it
  (`replay_fits` false): its largest request is 421,448 harness-priced tokens, at 1.8 bytes per
  token.
- At the measured rate it is closer. The replay drops H1's tool calls, tool outputs and answers and
  sends no tools (plan section 4), and it goes straight to the upstream, so only the window applies,
  not Codex's compaction trigger. By the model above, the first probe request comes to about 246,700
  real tokens, so the replay's largest request would be about 247,000. Its input alone would fit the
  272,000 window. It fails only with the 32,000 output allowance the harness reserves:
  247,000 + 32,000 > 272,000. This run's largest output was 228 tokens.

**Scores by category** (`l2f.score`; every answer correct, 0 stale):

| Type | Tier | Correct |
|---|---|---|
| chain | depth6-current | 8 of 8 |
| chain | depth6-previous | 7 of 7 |
| chain | depth8-current | 8 of 8 |
| chain | depth8-previous | 7 of 7 |
| distractor | 8-dups | 6 of 6 |
| multihop | 3-hop | 4 of 4 |
| multihop | 4-hop | 4 of 4 |
| obligations | single | 4 of 4 |
| obligations | set6 | 2 of 2 |
| paged_only | turn-1 | 4 of 4 |
| **Total** | | **54 of 54** |

**Cache** (reported only, as at 64,000): 57 of 57 eligible requests read the cache (the first
request and the 3 rotated ones are not counted). All 61 requests, the rotated ones included, had
`cached_tokens` above 0.

**Spend** (real usage from the proxy's log):

| | Tokens |
|---|---|
| Input | 4,985,655 |
| Cached | 4,585,344 (92.0% of input) |
| Output | 2,907 |
| Total | 4,988,562 of 14,014,001 (35.6%) |

**Requests and tools:**

| | |
|---|---|
| Requests | 61: 40 that ended a turn and 21 that held a tool call. Content turns used no tool round; the 6 probe turns used 5, 3, 2, 3, 5 and 3 |
| Input per request | mean 81,732, median 87,308, range 17,784 to 108,565 |
| Max output / mean output | 228 / 48 |
| MCP calls through Codex's `exec` | `contexto_search` 38, `contexto_recall` 38, `contexto_pin` 0, `contexto_state` 0; 0 failed |
| Spans returned | 106 `paged`, 14 `in_view` |
| Stream hold | 21 requests held (largest hold 191 events), 40 unheld |
| Codex memories | `memories_1.sqlite` holds 0 rows of output |

## Spend against the estimate

The run spent 4,988,562 against 14,014,001, so the estimate was 2.81 times the spend.

| Term | Priced | Actual | Factor |
|---|---|---|---|
| Requests | 70: 34 content + 6 probe turns × (1 + 5 tool rounds) | 61: 34 + 6 + 21 tool rounds | 1.15 |
| Tokens per request (input + output) | 182,000 | 81,780 mean | 2.23 |
| Margin | 1.10 | none | 1.10 |
| **Product** | | | **2.81** |

| | Priced per request | Actual | Total priced (× 70 × 1.10) | Total actual | Share of the 9,025,439 gap |
|---|---|---|---|---|---|
| Input | 150,000 (the bound) | mean 81,732 | 11,550,000 | 4,985,655 | 72.7% |
| Output | 32,000 (the allowance) | mean 48, max 228 | 2,464,000 | 2,907 | 27.3% |

The estimate is closer than at 64,000 (5.84 times) for two reasons:
- The retained window is a larger share of the bound: about 107,000 of 150,000, against about
  25,000 of 64,000.
- The 32,000 output allowance is a smaller share of each priced request: 32,000 of 182,000, against
  32,000 of 96,000. Its share of the gap fell from 40.2% to 27.3%.

## Calls outside the proxy

Two views, neither complete:
- **Codex's local logs:** no host named, with 0 log files and 288 SQLite rows scanned. This is not a
  network observation.
- **Network sample:** 245 samples, every 1 s, of each turn's process group. Short connections can be
  missed.
  - `codex` reached only 127.0.0.1.
  - The proxy process reached 127.0.0.1 and two IPv6 addresses on port 443. Both are among the
    addresses DNS gives for the owner's proxy host, so they are its upstream.
  - No connection outside the proxy path was seen.

## Compared with the 64,000 run

| | 64,000 (D30, main `ca05b29`) | 150,000 (this run, main `9d2949a`) |
|---|---|---|
| Codex | 0.154.0 | 0.163.0 |
| Turns | 15 content + 6 probe = 21 | 34 content + 6 probe = 40 |
| History (harness tokens) | 164,844 of 160,000 needed | 375,922 of 375,000 needed |
| Acceptance 1 | true | true |
| Compaction | none | none |
| Requests, all 200 | 44 | 61 |
| Largest input | 26,066 (40.7% of the bound) | 108,565 (72.4% of the bound) |
| Rotations | 16 `bound` + 3 `min_turn_room` | 0 `bound` + 3 `min_turn_room` |
| `window_budget` | 24,823 to 25,678 | 106,597 to 107,598 |
| Fixed overhead at rotation | 13,322 to 13,429 | 17,402 to 18,403 |
| Rewritten requests | 42 of 44, from turn 3 | 47 of 61, from turn 15 |
| Acceptance 3 | true | true |
| Paged-only credited | 1 of 4 recorded; 4 of 4 rescored with B30 | 4 of 4 |
| Credited | 11 recorded; 17 rescored | 22 |
| Credited per probe turn | 4, 2, 1, 3, 3, 4 (rescored) | 4, 3, 2, 3, 5, 5 |
| In-view spans | 23 | 14 |
| In-view spans per search | 0.27 (23 of 84) | 0.37 (14 of 38) |
| Score | 54 of 54 | 54 of 54 |
| Acceptance 2 | by arithmetic (D30) | by arithmetic (D30); no baseline run |
| Tool rounds per probe turn | 5, 3, 2, 4, 4, 5 (23) | 5, 3, 2, 3, 5, 3 (21) |
| MCP calls | search 84, recall 0, pin 9, state 1 | search 38, recall 38, pin 0, state 0 |
| Cached share of input | 74.0% | 92.0% |
| Max / mean output | 136 / 50 | 228 / 48 |
| Spend | 922,201 of 5,385,600 (17.1%) | 4,988,562 of 14,014,001 (35.6%) |
| Estimate over spend | 5.84 | 2.81 |
| Stream hold | 23 held, largest 128 | 21 held, largest 191 |
| Outside the proxy | none seen | none seen |

### What differed between the two runs

The bound is not the only change between the two runs, so most differences cannot be put on it:
- **Code:** main moved 295 commits from `ca05b29` to `9d2949a`. They include search ranking
  changes (#204, which dropped the BM25 length term, and the term-frequency cap of #216 and D41),
  the D39 search text cache, and D36's upstream affinity key.
- **The owner's proxy:** the O6 affinity fix was deployed on it between the runs.
- **Codex:** 0.154.0 against 0.163.0.

What follows from the bound, because the engine's window and rotation rules size from it:
- At 150,000 the engine rotated only for turn room, 3 times, paging 8 content turns each time. At
  64,000 it rotated for size 16 times.
- `window_budget` was about 107,000 against about 25,000. Here 7 to 14 content turns stayed in view
  at a time.
- Spend rose 5.4 times for 1.9 times the turns, because each request carried the larger window.

What the runs differ in but cannot attribute, with the changes that could explain each:
- **Cached share of input, 74.0% against 92.0%.** The larger window (fewer rotations, so longer
  stable prefixes) and the affinity changes (D36 in the code, O6 on the proxy) both bear on it.
- **Tool use.** At 150,000 the model paired each search with a recall (38 and 38) and made no pin
  or state call. At 64,000 it searched 84 times and never recalled. The Codex version, the search
  ranking changes and the larger window could each bear on this.
- **In-view spans, 14 against 23.** The total fell because the model searched less (38 against 84).
  Per search, in-view spans rose: 0.37 against 0.27. That fits more of the conversation being in
  view, but the search ranking changes and the search cache also apply.
- **Accuracy.** Both runs answered all 54 probes, and both retrieved all 4 paged-only plants from
  their page (64,000 after the B30 rescore).

One run per bound, on one model. The figures are measurements, not a general claim.
