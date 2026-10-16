# M2 A22: Codex acceptance step 1, live

Packet A22 (lane A). Step 1 of the Codex acceptance plan (`docs/research/M2-codex-acceptance-plan.md`):
the H1 arm alone, for acceptance 1 and 3, run live under intake's go of 2026-10-16 14:27Z. The
zero-spend dry run of the same step is `docs/research/M2-codex-step1-dryrun.md`.

Numbers and event counts only; no transcripts, prompts, tool output or answers.

## Run conditions

| | |
|---|---|
| Code | main `ca05b292b425a8f80944fea0433d723a13d0ba30`, tree `c654fbc73672e16b7a62994fffa081593668d688`, a clean detached checkout |
| Entry point | `spikes/m2/acceptance/codex_live.py` (PR #166) |
| Codex | codex-cli 0.154.0, `gpt-6-astra`, reasoning effort low |
| Engine | bound 64,000, 20 KB turns, seed 20261016 |
| Auth | API-key mode through the owner's proxy (D12, D22); the key from the seat's own key helper, read at call time, in no file |
| Homes | temporary `CONTEXTO_HOME` and `CODEX_HOME` outside the repository |
| Codex pins | `RETRY_PIN` (request and stream retries 0); `QUIET_PIN` (`analytics.enabled=false`, `features.plugins=false`) |
| Approval | 5,385,600 tokens, equal to codex_budget's step-1 worst case |

Command, from the checkout's root (the seat settings path shortened):

```
uv run python spikes/m2/acceptance/codex_live.py "$PWD/.venv/bin/contexto" <out dir> \
    --upstream <owner's proxy base URL> \
    --key-helper "python3 <checkout>/spikes/m2/acceptance/keyhelper.py <seat>/.claude/settings.local.json" \
    --approved 5385600
```

Stop rules armed: any 4xx or 5xx, a request whose output passes 32,000 tokens, a Responses request
that ended other than `completed` (A23), a turn with no assistant message (how Codex 0.154.0 shows a
refusal), Codex's `turn.failed` or `error`, and the fail-closed spend watcher. None fired.

## Result

The run completed: exit 0, 21 of 21 turns (15 content, 6 probe), every Codex exit 0, no failed turn,
no watcher stop.

**Acceptance 1: true.**

| | |
|---|---|
| History | 164,844 tokens against the 160,000 needed (2.5 times the bound) |
| Compaction | none: Codex's rollout holds no `compacted` record |
| Requests | 44 POST `/v1/responses`, all 200, all with `end_event` completed; 0 4xx |
| Bound | largest input 26,066 against 64,000; 0 `over_bound` real; no failure events |
| Rotations | 16 for size (`bound`) and 3 for room (`min_turn_room`) |

**Acceptance 3: true** (`codex_recall_credit`, as recorded by the harness).

1 of the 4 paged-only probes was credited: `q_p_04`, via `contexto_search`. That meets acceptance 3's
contract.

| | |
|---|---|
| Credited | 11, with expected_turns 21 |
| Credited per probe turn | 3, 1, 0, 0, 3, 4 |
| Answered correctly | 54 of 54; 43 of them not credited |
| In-view spans | 23 |

**What the two counts are.** `credit` scores all 54 probes of the script. Its result list is named
`paged_only`, but it holds every probe:
- the 4 paged-only probes `q_p_01` to `q_p_04` (type `paged_only`, planted in an early content turn);
- the 50 probes `q_a_001` to `q_a_050` (chain, distractor, multihop and obligations types).

A probe is credited when it was answered correctly and a `paged` span returned before its probe
turn's answer covers its plant.
- **11 credited** = `q_p_04` plus 10 `q_a` probes: `q_a_001`, `q_a_002`, `q_a_003`, `q_a_004`,
  `q_a_005`, `q_a_006`, `q_a_008`, `q_a_039`, `q_a_040`, `q_a_043`.
- **1 of 4 paged-only** = `q_p_04`, the one paged-only probe among those 11.

**The 3 paged-only probes answered correctly but not credited** (`q_p_01`, `q_p_02`, `q_p_03`).
Checked by page ids, offsets, turn numbers and record kinds, never by reading text.

- *Pinned state (hypothesis): ruled out by timing.*
  - The plants' content turn was paged out at turn 3's request and never came back into view: every
    later `page_out` starts at turn index 0.
  - Content turns 1 to 15 made no MCP call at all.
  - The 9 `contexto_pin` calls came in probe turns 17 (1), 18 (4) and 21 (4), so no pin was made
    while the plants were in view. `q_p_03` was asked in turn 16, before any pin existed.
  - The pins are ledger records of kind `obligation` with empty `provenance`, so none links to a page
    by id or offset.
- *In view at answer time: no.* All 42 requests from turn 3 on were sent rewritten (smaller than
  Codex's original body). The index line of a paged page carries only its first 200 characters of user
  text and key-position identifiers. The plants sit past offset 900 of their page.
- *Codex's own memories: none.* Its `memories_1.sqlite` holds 0 rows of output.
- *Retrieval: it happened.*
  - All 4 plants sit on the same original content page.
  - For each of the 4, a `contexto_search` call returned a `paged` range covering the plant on that
    page before its probe turn's answer.
  - The statements of `q_p_01`, `q_p_02` and `q_p_03` also occur on later pages, of probe turns
    (0-based page turn indices 16, 17 and 19; turn numbers elsewhere in this note are 1-based). How the
    copies got there is unverified: their text was not read. They are consistent with search results
    carried into the probe turns.
  - `recall_credit.plant_spans` takes the first page, in the order given, that holds the statement.
    The harness (`codex_live` and `codex_dryrun`) passes pages sorted by file name, that is by random
    page id. So for these 3 it located the later copy, which no search covered.
- *Recount (not the recorded verdict).* The same `credit` rule, with pages passed in turn order so the
  earliest occurrence is the plant, credits 17: all 4 paged-only probes (adding `q_p_01`, `q_p_02`,
  `q_p_03`) and 13 `q_a` (adding `q_a_007`, `q_a_042`, `q_a_044`). None are lost, and acceptance 3 is
  true either way. Re-running the harness's own order reproduces the recorded 11 exactly. The fixed
  verdict, rescored on main after B30, is in the next section.

### Acceptance 3 rescored with the fix (A25)

Lane B's B30 (PR #169) makes `recall_credit.plant_spans` take the page with the earliest turn as the
plant, whatever order the caller passes the pages in. The recorded run was rescored on main
`24cfe23` from its stored session log, pages and answers. No live request was sent and nothing was
re-run.

| | Recorded under the page-order bug (main `ca05b29`) | Rescored with B30 (main `24cfe23`) |
|---|---|---|
| Acceptance 3 | true | true |
| Credited | 11 | 17 |
| Paged-only credited | 1 of 4 (`q_p_04`) | 4 of 4 (`q_p_01` to `q_p_04`) |
| Credited per probe turn | 3, 1, 0, 0, 3, 4 | 4, 2, 1, 3, 3, 4 |
| `q_a` credited | 10 | 13 (adds `q_a_007`, `q_a_042`, `q_a_044`) |
| Answered correctly, not credited | 43 | 37 |
| expected_turns | 21 | 21 |
| In-view spans | 23 | 23 |

- **Cause.** The harness passed pages in file-name order (random page ids), and the old
  `plant_spans` took the first page holding a statement. For 3 paged-only probes it located a later
  probe turn's copy, which no search covered, instead of the plant.
- **Control.** The same rescore command run from a clean checkout of `ca05b29` reproduces the
  recorded column exactly.
- **Command.** From the repository root of a clean checkout at the named commit, with the run's
  stored directory kept locally outside Git:
  `uv run python <rescore script> <run dir>`. The script calls `codex_dryrun.acceptance_3` with
  `codex_budget.history_turns(64000, 20000)`, the run's `log.jsonl` and pages, and the 6 probe turns'
  last messages. It prints counts and probe ids only.

**Scores by category** (`credit`'s rule: the answer for an id equals the key, ignoring case). All
correct.

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

The run's answers and the plan's answer key are kept locally for an independent rescore, never in
Git.

**Spend** (real usage from the proxy's log):

| | Tokens |
|---|---|
| Input | 919,998 |
| Cached | 680,832 (74.0% of input) |
| Output | 2,203 |
| Total | 922,201 of 5,385,600 (17.1%) |

**Requests and tools:**

| | |
|---|---|
| Requests | 44: 21 that ended a turn and 23 that held a tool call. Content turns used no tool round; the 6 probe turns used 5, 3, 2, 4, 4 and 5 |
| Max input / max output | 26,066 / 136 |
| MCP calls through Codex's `exec` | `contexto_search` 84, `contexto_recall` 0, `contexto_pin` 9, `contexto_state` 1; 0 failed |
| Stream hold | 23 requests held (largest hold 128 events), 21 unheld |

The model also called `contexto_pin` and `contexto_state`, which the stub never did, and never
`contexto_recall`. Neither is a stop rule; both are noted as observed.

## Spend against the estimate

The approval was codex_budget's step-1 worst case: 51 requests at the bound plus the output allowance,
plus a 10% margin. That is 51 × (64,000 + 32,000) × 1.10 = 5,385,600. The run spent 922,201, so the
estimate was 5.84 times the spend. Term by term:

| Term | Priced | Actual | Factor |
|---|---|---|---|
| Requests | 51: 15 content + 6 probe turns × (1 + 5 tool rounds) | 44: 15 + 6 + 23 tool rounds (probe turns used 5, 3, 2, 4, 4, 5) | 1.16 |
| Tokens per request (input + output) | 96,000 | 20,959 mean | 4.58 |
| Margin | 1.10 | none | 1.10 |
| **Product** | | | **5.84** |

Within the per-request term:

| | Priced per request | Actual | Total priced (× 51 × 1.10) | Total actual | Share of the 4,463,399 gap |
|---|---|---|---|---|---|
| Input | 64,000 (the bound) | mean 20,909, median 20,800, range 16,731 to 26,066 | 3,590,400 | 919,998 | 59.8% |
| Output | 32,000 (the allowance) | mean 50, max 136 | 1,795,200 | 2,203 | 40.2% |

- **Input.** The engine kept each request near its retained-window budget, not the bound. In the 19
  rotations, `window_budget` was 24,823 to 25,678 tokens, with a fixed overhead of 13,322 to 13,429.
  The largest input was 40.7% of the bound.
- **Output.** At low effort the model's replies were short: identifiers and values, at most 136
  tokens. The 32,000 allowance is a reserve, because Codex sets no output cap.
- **Cache.** 680,832 of the input tokens (74.0%) were cached. The line counts cached input in full, so
  caching does not lower the counted spend. It matters only for a cost estimate.
- **Requests.** The tool-round allowance of 5 per probe turn was reached in 2 of the 6 probe turns
  (mean 3.8).

For the next estimate table, measured on this run: 20,909 mean input per request (26,066 max) with
this engine and 20 KB turns, 50 mean output (136 max), and 3.8 tool rounds per probe turn (5 max).

## Calls outside the proxy

Two views, neither complete:
- **Codex's local logs:** no host named, with 0 log files and 237 SQLite rows scanned. This is not a
  network observation.
- **Network sample:** 168 samples, every 1 s, of each turn's process group. Short connections can be
  missed.
  - `codex` reached only 127.0.0.1.
  - The proxy process reached 127.0.0.1 and two addresses on port 443. Both are the AAAA records of
    the owner's proxy host (a DNS lookup only), so they are its upstream.
  - No connection outside the proxy path was seen. Without `QUIET_PIN`, a stub run's samples showed
    Codex reaching chatgpt.com and github.com (PR #166).
