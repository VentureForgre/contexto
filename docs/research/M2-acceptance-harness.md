# M2 acceptance harness: design, detectors, and dry-run budget

Status: harness built and tested against synthetic fixtures; nothing has been run live. Packet M2
B5 (lane B). Author: build.builder-4. Date: 2026-10-16. Contract:
`docs/milestones/M2-contexto-proxy.md` on `main`, section Acceptance. Code:
`spikes/m2/acceptance/`, tests in `spikes/m2/acceptance/tests/`.

The harness lives under `spikes/m2/` rather than `src/` or `tests/`. It drives a live CLI, reuses
the M0 probe generator (`spikes/m0/fidelity/`), and is not part of the installed package.

Claims are tagged **[measured]**, **[vendor-documented]**, or **[inferred]**.

## What it checks

| Acceptance | How the harness checks it |
|---|---|
| 1. Long run: zero compaction, every request under the bound except logged over-bound turns, zero 4xx | `detectors.analyze` over the session log and Claude Code's session JSONL |
| 2. Fidelity: within 2 probes of the full-context baseline overall, no category more than 1 below | `run.py score`, using the deterministic L2f scorer on both answer sets plus `compare` |
| 3. Recall: a paged-out-only probe answered, and the answer reached the model through Contexto's tools: a `contexto_recall` of its page or a `contexto_search` hit whose returned lines contain it | 4 paged-only probes; `run.py check --answers` credits each from the `mcp_call` events' returned ranges (`recall_credited`). Whether `contexto_recall` was called is reported under `informational`, never a verdict |
| 4. Cache within an epoch, misses reported as a rate | `Report.cache_hit_rate` and the list of misses |

## The session

**Turns.** `script.build` writes content turns that each paste the next part of a synthetic working
log. Each ends "Reply with exactly: OK". A final probe turn follows, which may use the contexto
tools.

**Probes: the chain-heavy mix intake chose**, from L2f. Deep supersession chains are the only probe
type that failed at small contexts.

| Type | Probes | Tiers |
|---|---|---|
| Supersession chains | 30 | depth 6 and depth 8, half current value and half the value before it |
| Multi-hop | 8 | 3-hop and 4-hop |
| Obligations | 6 | 4 single status, 2 sets of 6 |
| Near-duplicate distractors | 6 | 8 look-alikes each |
| Paged-only | 4 | config lines planted only in turn 1 |
| **Total** | **54** | |

**Paged-only probes (acceptance 3).**
- Each is an unflagged config line planted only in the first turn, which the run is certain to
  page out: rotation starts within the first few turns.
- No later turn restates it, and the probe turn does not contain the answer.
- So a correct answer needs `contexto_recall` or `contexto_search`.

**Checks** [measured, by the tests]:
- No plant subject appears outside its own statements and question.
- The paged-only lines appear only in turn 1.
- Every statement is present.
- Turns land within 3% of their size target.

## Driver

**One process for the whole run**, so the proxy, its epoch state and the MCP server live
throughout:

```
contexto run --bound 150000 --session <id> --upstream <url> claude -p --input-format stream-json \
    --output-format stream-json --verbose --model claude-haiku-5-5
```

- This is lane A's launcher (PR #40). Contexto's options come before `claude`, and everything
  after it passes through unchanged.
- Each user turn is one stream-json line on stdin. A turn ends at the `result` event.
- The scratch directory must not be inside any git work tree, checked with `git rev-parse
  --is-inside-work-tree`, so a nested directory is refused too.
- The child environment drops OpenRig and parent-session variables and `ANTHROPIC_BASE_URL`, and
  sets `CONTEXTO_HOME`. The launcher points Claude Code at its own proxy, so nothing goes around it.
- No `--settings` or `--mcp-config` of the harness's own is passed; the launcher supplies both.
- `--upstream` is passed explicitly: the owner's proxy (D12), from `$ANTHROPIC_BASE_URL` unless
  given.
- **A hard spend stop inside a turn** (PR #68 review). The guard in `Run` projects spend before each
  turn and checks real spend after it, so it cannot see the probe turn's tool loop, which runs
  inside one Claude Code turn. `SessionWatcher` tails the session log beside the auth stop: after
  each Messages API response it adds the real usage and terminates the run if two more requests of
  the last input size plus the 32,000 output reserve could pass the approval. It counts responses on the billed
  model paths (`/v1/messages` and `/responses`, not their token counts), with the same path helper
  as the 4xx and auth checks, so a trailing slash is the same path. Two, because the proxy
  logs a request when its response ends, when Claude Code may already have sent the next one. The
  model never sees this limit. The run then reports `BudgetExceeded` with the spend so far.

**Credential** (intake-approved, D12). If the scratch run inherits `HOME`, Claude Code falls back
to the owner's claude.ai subscription login, which the rig proxy rejects with a 401 (lane 1
measured this case, PR #9). So:
- **Isolated config.** The run uses an isolated `CLAUDE_CONFIG_DIR` in the scratch directory. Its
  only setting is an `apiKeyHelper` pointing at `keyhelper.py`, whose sole argument is the path of
  the seat's settings file.
- **Read-only helper.** At call time the shim reads that file's `apiKeyHelper` command and runs
  it. No key, and no copy of the helper command, is written to the scratch directory, a log, or a
  commit.
- **Why not `--settings`.** The helper sits in the isolated user settings rather than a second
  `--settings`, because merging with the launcher's own `--settings` is undefined (builder-1).
- **No other credential.** `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and
  `CLAUDE_CODE_OAUTH_TOKEN` are removed from the child environment. Nobody logs in or out, and no
  auth mode changes.
- **Stop on auth failure.** A watcher tails `sessions/<id>/log.jsonl` and stops the run at the
  first 401 or 403.
- **Zero-spend check** (`run.py check-credential --seat-settings PATH`).
  - It runs `claude -p` in default (non-bare) mode with the isolated config and a stub upstream
    that answers 401 and never forwards.
  - It compares the request's key header with the helper's output in memory and records only
    booleans.
  - Result on this seat, 2026-10-16 [measured]: a request reached `/v1/messages`; `x-api-key` was
    present and equal to the seat helper's output; there was no OAuth beta header. **pass**.
  - Once lane A's launcher lands, the same check should run through `contexto run`.

**Guard.**
- Before every turn, the guard projects spend plus that turn's estimated input plus the full
  output reserve (32,000). It stops rather than pass the approved figure, and re-checks after
  every turn.
- `run.py live` refuses to start unless the approved figure covers the expected H1 cost.

## Detectors

**Session log** (lane A's schema, per builder-1, 2026-10-16):
- An upstream call is an `event: "request"` record with `path: "/v1/messages"`. `count_tokens`
  and other paths are ignored.
- **Over-bound (acceptance 1, contract rule 5).** A request's input is `input_tokens +
  cache_read_input_tokens + cache_creation_input_tokens`. Intake set a strict rule for the
  acceptance run (2026-10-16). Acceptance 1 fails on any of:
  - a request over the bound (`over_bound_indexes`, verdict `no_request_over_bound`);
  - an A2b `over_bound` event with `source: "real"` (count and largest `real_tokens`, verdict
    `no_real_over_bound`);
  - a `bound_too_small` request, a configuration failure (verdict `no_bound_too_small`);
  - a pre-send `over_bound` record not marked as a tool loop (`pre_send_other`, verdict
    `no_pre_send_over_bound_outside_tool_loop`).

  A pre-send record with `source: "estimate"` and `in_tool_loop: true` (rule 5's tool-loop growth,
  PR #48) is reported in `pre_send_tool_loop` only. `window_minimal` requests (contract PR #50,
  lane A's A2c) are counted. `estimate_error` events are reported (count, lowest ratio, last
  `mean_ratio`); no verdict uses them.
- **Where the real-usage events live.** Lane A's A2b wrote `over_bound` (`source: "real"`) and
  `estimate_error` as separate session-log lines; A2e (PR #76) moves them onto the request's entry as
  `usage_events`. The detectors read both forms and count an event that appears in both once (same
  event, epoch, `real_tokens`, `estimate`); `estimate` may be null. The entry's `over_bound` flag is
  never read as a real over-bound, because a pre-send tool-loop record sets it too.
- **Failure events (acceptance 1).** Intake (2026-10-16, contract PR at `147b182`): any of
  `degraded_enter`, `store_error`, `page_conflict`, `fallback_failed`, `transform_error`,
  `turn_room_missing`, or `unclassified_system_message` fails it, wherever it appears (a request's
  transform events, its `usage_events`, or a separate session-log line). `failure_events` counts each
  by name, once, and `no_failure_events` fails unless it is empty.
- **Local 5xx (acceptance 1, contract PR #98).** A 5xx Contexto's own proxy generates on a model
  request fails the run; an upstream 5xx passed through is reported (`upstream_5xx`). The proxy at
  main marks every local model-path 5xx in the session log: `_forward`'s unreachable upstream (a
  request entry with status 502 and `error: "upstream unreachable: ..."`, no `response_bytes`),
  `_handle`'s last resort (`proxy_error` with `fallback: "local_502"`, no request entry), and a
  WebSocket upgrade (`websocket_rejected`, answered 501). A passed-through 5xx has `response_bytes`
  and no such error. A model-path 5xx with neither mark fails closed as local. `local_5xx` lists each,
  and `no_local_5xx` is the verdict. The second live H1 run's request 6 (502, upstream unreachable,
  TimeoutError) re-scores as a local 5xx.
- **Unverified requests (acceptance 1).** A `/v1/messages` request whose `usage` is null or
  missing cannot be checked against the bound, so it must not hide an over-bound request. Their
  indexes are reported in `unverified_requests`, and the `every_request_has_usage` verdict fails
  unless the count is 0. The usage comes from the JSON body or, for streamed replies, the SSE
  `message_start` and `message_delta` events.
- **4xx (acceptance 1).** Only model requests count (contract PR #61): `upstream_4xx` events and
  request records on the paths the proxy transforms (`contexto.proxy.TRANSFORMED_PATHS`:
  `/v1/messages`, its `count_tokens`, and `/responses`; a trailing slash counts), including 4xx the
  proxy answers itself, plus rule 15's `encoded_body_rejected` (415). All must be zero. 4xx on any other path, such as Claude Code's
  unauthenticated `HEAD /api/hello` startup probe, are reported in `non_model_4xx` by method and
  path, and no verdict uses them.
- **Epochs and cache (acceptance 4).**
  - The first request seen in each `epoch` starts that epoch.
  - Every later successful request in the epoch is cache-eligible, and it is a hit if
    `cache_read_input_tokens > 0`.
  - The rate and the miss indexes are reported. Explaining misses by the route is left to the
    analyst; see the cache-miss note below.
- **Rotations** are engine events inside `transform_events`, counted only when named exactly
  `rotation`; a `rotation_noop` pages nothing. Requests per epoch are reported too.
- **Paged-only proof.** `check --session-dir` confirms each paged-only plant line is inside a
  stored page under `sessions/<id>/pages/` before the recall criterion is claimed.
- **Recall (acceptance 3).** `event: "mcp_call"` with `tool: "contexto_recall"` and `ok: true`,
  written by the MCP server (PR #24).

**Compaction (acceptance 1).** Claude Code's session JSONL marks a compaction with a `system`
record whose subtype is `compact_boundary`, plus a `user` record with `isCompactSummary`. The driver
also counts stream-json `system` events with subtype `compact_boundary`. All three must be 0.
[Inferred by lane 1 from the 2.1.301 binary; not vendor-documented.]

**When Claude Code would compact.** Its trigger is the token usage the API reports plus a local
estimate of what was added since, not either one alone (lane 1). The proxy returns the bounded
prompt's real usage (rule 7), so the reported part stays under the bound. Rule 4's headroom keeps
the local part small.

**Route-side cache misses** affect acceptance 4.
- On the rig route, lane 1 counted 4 of 14 cache-eligible Anthropic calls as full misses, about
  29%.
- The cost model's 23% is 3 of 13 throwaway calls, counted before a fourteenth call was included.
  The two figures describe overlapping samples, not different behavior.
- The misses are **likely** a route artifact, not something Contexto causes [inferred]. The
  harness reports the rate and the miss positions, so the analyst can set route misses apart.

## Pre-usage estimates (decided 2026-10-16)

The session's logs are pasted as user text, and the engine flags `over_bound` from its pre-send
estimate until the first real usage arrives. Contract PR #44 counts `tool_result` content at 1.5
bytes per token before that, but pasted user text still counts at the 3.5 text rate, while this
content measures near 1.8. The first epoch's estimate can therefore run low.

Manager-2 chose **option (a)**: keep the logs as pasted user text. The dry run reports the largest
gap between the pre-usage estimate and the real token count, and the first epoch's margin to the
bound. **The 10 percent rule:** if any pre-usage request reaches 135,000 tokens (within 10 percent
of the 150,000 bound), the run switches to **option (b)**: the logs become files in the scratch
directory that the model reads with the Read tool, so they arrive as `tool_result` content at the
1.5 seed. The budget is then recomputed before asking for the go.

## D27: the probes over six turns, and a symmetric baseline (intake, 2026-10-16)

**Why (intake).** Answering all 54 probes in one turn needs about 91,000 tokens of returned evidence,
while each probe alone is 54 of 54 answerable (search reachability). The test changed after a failure
(the second live H1 run); the single-turn mega-query is a known limitation, recorded as such.

**Protocol.** The same 54 L2f probes, with the same per-probe question text, are asked over 6 user
turns of 9, each turn worded as a user asking nine questions in one message (no tool or cap named).
The assignment was fixed and written down at 05:39:09Z before anything was computed, and is never
tuned: take the script's probe list in order (`run.plan()`, seed 20261016), shuffle a copy with
`random.Random(20261016).shuffle`, and give probe turn k positions `[9(k-1), 9k)`.

| Turn | Probe ids |
|---|---|
| 1 | q_a_001 q_a_020 q_a_028 q_p_03 q_a_016 q_a_038 q_a_046 q_a_002 q_a_006 |
| 2 | q_a_050 q_a_041 q_a_012 q_a_035 q_a_021 q_a_049 q_a_004 q_a_013 q_p_02 |
| 3 | q_a_022 q_a_044 q_a_026 q_a_031 q_a_036 q_a_009 q_a_047 q_a_045 q_a_024 |
| 4 | q_a_017 q_a_018 q_a_010 q_a_042 q_a_019 q_a_027 q_p_01 q_a_033 q_a_007 |
| 5 | q_p_04 q_a_014 q_a_037 q_a_011 q_a_030 q_a_034 q_a_003 q_a_008 q_a_025 |
| 6 | q_a_015 q_a_048 q_a_023 q_a_040 q_a_039 q_a_029 q_a_032 q_a_043 q_a_005 |

**Symmetric baseline (05:42Z).** The baseline is rerun as 6 sequential full-context requests under
the same assignment. Request k is the recorded history up to the first probe turn, then probe turns
1..k-1, each followed by the baseline's own answer, then probe turn k. It never contains H1's
answers, because decoding stops at the first probe turn. The same replay rules apply
(no-replayed-thinking, Contexto's tools removed, display `omitted` labeled, D23 headers), with a
count_tokens gate before each send against what is left of the approval. Acceptance 2 compares H1's 6x9
answers with the baseline's 6x9 answers per probe id.

**Budget.** `budget.simulate` runs each probe turn as a turn. The rotation rules apply, A9's room is
`max(0, bound - (first input + output) - 25,000)`, and recalls are priced at the largest B9 chunk and
stop at the room. Evidence and answers stay in the history. Every input token counts toward the
approval, cached or not: caching lowers the price, not the token count. The live gate is the
combined worst case (H1 plus baseline); H1's guards stop at the approval minus the baseline's worst.
After H1, `live` writes `<home>/acceptance-<session>-h1.json` with what H1 cost: the larger of the
driver's per-turn figure and the usage the proxy logged for every Messages response (what was
billed; the driver's figure misses a turn stopped midway), plus, after an abnormal stop, an
in-flight allowance of the last logged input and the output reserve. Run the baseline with `run.py replay --approved <combined approval>
--h1-record <that file> ...`: the replay then may spend the combined approval minus H1's recorded
spend, and its count_tokens gate checks each request against what is left of that.
A12 (contract PR #115 at 1ce122c) also rotates a turn whose published room, `bound - estimate -
25,000`, would fall under `min_turn_room`, which the simulation reads from the engine's Config (no
fallback; a guard test fails if the field is missing or differs). The simulation follows the engine's
turn-start order (`rotate_at_turn_start`) and contract PR #120: when the size trigger and the room
condition both fire, the room-sized target is used if it restores the room, and `turn_room_low` is
logged when the room is still low after whatever was decided. A size-triggered turn whose window
cannot hold the newest complete turn and the new one rotates to the minimal window
(`window_minimal`, contract PR #50), with `bound_too_small` as well if even the new turn exceeds `W`. Such a rotation targets
`0.5 * (W - min_turn_room)`, falling back to the minimal window (the new turn alone), and fires only
if it restores a room of at least `min_turn_room`; otherwise there is no room rotation and
`turn_room_low` is logged once per epoch, which fails acceptance 1 (decided at 1eb8a4c). The
simulation reports `room_rotations` and `turn_room_low_epochs`. With 40 KB turns at bound 150,000
and 12,000 there are 11 rotations, 9 of them room rotations (every other content turn, each to the
minimal window, and probe turn 2), and no `turn_room_low`. Each 40 KB turn is about 22,000 tokens,
so every other turn start leaves a room of about 11,000, under 12,000 as well as under 20,000.
Epochs hold one or two turns, which leaves acceptance 4's within-epoch cache reads little to
measure. Rooms at the six probe-turn starts are 29,562, 55,116, 18,551, 55,058, 18,463, and 54,925.
The combined worst case exceeds the earlier 4,000,000 budget, so D27 needs a new approval.

## Zero-spend dry run through the launcher (2026-10-16)

```
uv run python spikes/m2/acceptance/stubrun.py <seat settings> .venv/bin/contexto .contexto/spikes/<dir> [turn KB]
```

`stubrun.py` runs the whole acceptance script through lane A's real `contexto run claude` (PR #40)
against a local stub upstream that never forwards. It calls `run.run_h1`, the function `live`
runs, with the stub as upstream and the live gate's approval, so the guard, its per-turn
estimates, the spend watcher and the exception handling are the live ones (live-path fix, after
the aborted 07:08Z run). `--fault auth:N` answers the N-th Messages request (0-based) with 401, so
the watcher's auth stop fires; `--fault raise:N` raises a harness error after turn N, so the record
is written on the error path. `run_h1` refuses the fault hook unless the upstream is loopback. Each H1 record lists the live-only paths it took
(`run.LIVE_PATHS`); `stubrun.py check-paths <dir>...` fails unless the runs together took all of
them. A clean run alone leaves `stop_handled` and `error_handled` untaken, so an official dry run
is the clean run plus one run of each fault. The stub answers `OK`, as SSE when the request
streams, and reports a **modelled** usage: pasted log text at 1.8 bytes per token and every other
byte at 3.0 [assumed; it errs high against the contract's 3.5]. The real head's token count is
not measured, since that needs the API. Everything else is the real launcher, engine, MCP server
and Claude Code 2.1.301. The stub checks the credential on every request, keeping booleans only.

**Finding: 70 KB turns fail.** At 70 KB the engine logged `bound_too_small` on 8 of 11 requests
and sent 7 over the bound, up to 408,704 modelled tokens, rotating only on the probe turn. The
engine's check needs `W` to hold the newest complete turn **and** the new turn (`window_tokens`
from `starts[-2]`); `W` was about 58,000 to 63,000 and two 70 KB turns are more than 66,000. The
earlier budget simulation checked one turn only. It now matches the engine and reproduces this.

**At 40 KB turns** (18 content turns, 375,000+ tokens of history):

| Measure | Value |
|---|---|
| Requests; rotations; epochs | 19; 5; 6 (4 requests in the first epoch, 3 in each later one) |
| Over-bound requests; `bound_too_small`; usage missing | 0; 0; 0 |
| Head: system bytes, tools, tool bytes | 8,982; 26; 60,306 (about 23,100 tokens at 3.0) |
| Engine's fixed overhead `F` (estimated) | 47,600 at most |
| Pre-usage request: modelled real vs estimate | 48,799 vs 33,668 (31% low), far below 135,000 |
| Later requests: largest estimate gap | 7% low (69,948 vs 65,059) |
| First epoch's largest input; margin to 150,000 | 113,693; 36,307 |
| Largest input overall | 116,662 |
| Probe turn: start; with 2 full recalls; margin | 98,341; 133,941; 16,059 |
| Paged-only plants inside stored pages | 4 of 4 |
| Credential | 19 of 19 requests carried the seat helper's key; no OAuth beta header |

This run used the engine before PR #48, which logged only the unrotated estimate on a rotation
request. Its 5 rotation requests are left out of the gaps above. With PR #48 the engine also logs
`sent_estimate`, and `stubrun.py` compares real usage with that.

**The approval figure.** `run.py live` refuses an approval below `budget.simulate`'s worst-case H1,
so that simulation is the budget to approve. `run.py approval-table` prints it, and the table
between the markers below is that output; a test fails if they differ. Defaults: 18 turns of 40
KB, head 47,600, bound 150,000, 2 recalls sized by B9 (below).

<!-- approval-table:begin -->
| Line | Expected tokens | Worst case (guard reserve) |
|---|---|---|
| H1 acceptance run through the proxy, 6 probe turns (D27) | 2,951,215 | 4,001,059 |
| Full-context baseline, 6 direct requests, one per probe turn (D27) | 2,684,549 | 2,872,553 |
| Full-context baseline through a plain CLI session (alternative) | 5,054,342 | - |
| **Total, H1 + baseline (live refuses an approval below the worst case)** | **5,635,764** | **6,873,612** |
<!-- approval-table:end -->

**Official zero-spend rerun** (2026-10-16, main at 9dc2936: engine with PR #48's records and
A2c's `window_minimal`, B9's recall size). Both runs went through the real launcher against the
stub, with the credential checked on every request.

| Measure | 40 KB plan (18 turns) | 70 KB pair (10 turns) |
|---|---|---|
| Requests; rotations; epochs | 19; 5; 6 | 11; 4 (all `window_minimal`); 5 |
| Over bound, real over_bound, pre-send outside a tool loop, `bound_too_small` | 0, 0, 0, 0 | 0, 0, 0, 0 |
| Usage missing; 4xx; compaction | 0; 0; 0 | 0; 0; 0 |
| Pre-usage request: modelled vs estimate | 48,799 vs 33,668 (31% low) | 65,153 vs 42,243 (35% low) |
| Later requests: estimate gap | 7% low at most; rotation requests 2,700 high | 7% low at most; rotation requests 2,900 high |
| First epoch's largest input; margin to 150,000 | 113,693; 36,307 | 102,719; 47,281 |
| Probe turn: start; engine's `W` | 98,367; 84,303 | 107,301; 62,438 |
| Probe turn with 2 recalls priced at 17,778; margin | 133,923; 16,077 | 142,857; 7,143 |
| Same with B9's size at that `W` | 17,563 each: 133,493; 16,507 | 13,008 each: 133,317; 16,683 |
| Credential | 19 of 19 helper key, no OAuth | 11 of 11 helper key, no OAuth |
| Paged-only plants in pages | 4 of 4 | 4 of 4 |
| H1 expected / worst (stub cross-check) | 1,962,037 / 2,629,277 | 1,220,746 / 1,632,146 |

Every acceptance verdict passed in both runs except `recall_called` (then a verdict, now
informational), since the stub calls no tools. The stub run's 40 KB total (2,387,214 / 3,082,454) sits about 248,000 tokens under the
approval table, so the guard never stops a run the stub run predicts.

**Recalls in the probe turn.** The probe turn's tool loop cannot rotate, so the budget prices 2
recalls. Intake made this a cost guard only (2026-10-16): the probe turn is product-realistic and
names neither Contexto's tools nor a cap ("If the conversation does not contain it, say unknown."),
so the preamble and page index must lead the model to recall. B9 (PR #56) sizes each recall at `W / 4` tokens times the smaller of the
session's measured bytes per token and 1.5, never above 32,000 characters, and at 12,000 characters
until the engine records `W` with `set_window`. The budget prices each recall at the largest chunk,
32,000 characters of log text, 17,778 tokens; at this run's `W` (62,000 to 85,000) B9 returns
13,000 to 17,600 tokens. `recalls_over_budget` reports recall calls beyond the 2 priced per probe turn, 12
over D27's six probe turns, across the whole session; no verdict uses it.

Under option (a) the pre-usage request stays far below 135,000, so the logs stay pasted user
text. `Params.head` is now 47,600 (the engine's `F` here) and the default turn size 40 KB.

**Intake's parameters** (2026-10-16 00:40Z): bound 150,000 with at least 375,000 tokens of
history; the replay baseline, labeled no-replayed-thinking; up to 4,000,000 tokens with the guard
at the worst case.

The rows below are the earlier analysis. Its simulation checked only the newest complete turn
against `W`, so its rows with turns above about 40 KB at 150k understate `bound_too_small`.

**Method**
- The proxy is simulated with contract rules 3 to 5. The window budget is `W = bound - headroom -
  F`, with `F` = head plus preamble, and rotation targets half of `W`. The newest complete turn is
  always kept, and the tool loop never rotates.
- Pasted log text is counted at 1.8 bytes per token, M0's measured rate for numeric log text
  (about 0.55 tokens per character).
  - The contract's estimator uses 2.0 for tool output and 3.5 for other text. 2.0 runs about 10%
    low on this content, as reviewer-2 noted, and 3.5 about half.
  - Token counting replaces the estimate where it is available.
- The head is 54,500, the contract's M0 figure for a throwaway head with pins. Lane 1 relayed
  45,925 without pins, and both are scanned below.
- The probe turn is modeled with 6 `contexto_recall` round trips of about 8,000 tokens each.

| Line | Expected tokens | Worst case (full output reserve) |
|---|---|---|
| H1 acceptance run through the proxy (17 turns + probe turn) | 2,247,441 | 3,009,901 |
| Full-context baseline, one direct replay request (recommended) | 312,848 | 340,848 |
| Full-context baseline through a plain CLI session (alternative) | 3,502,600 | - |
| **Total, H1 + replay baseline** | **2,560,289** | **3,350,749** |

The plan reaches 252,487 tokens of local history, 2.52 times the bound, with 15 rotations and no
planned over-bound requests.

**Scan of bound, head and turn size** (H1 + replay baseline, expected):

| Bound | Head | Turn size | Turns | W | Rotations | Planned over-bound | Total |
|---|---|---|---|---|---|---|---|
| 100,000 | 54,500 | 30 KB | 17 | 20,500 | 15 | 0 | 2,560,289 |
| 100,000 | 54,500 | 20 KB | 25 | 20,500 | 23 | 0 | 2,945,754 |
| 100,000 | 54,500 | 44 KB | 12 | 20,500 | 0 (bound_too_small on 11) | 10 | 5,064,020 |
| 100,000 | 45,925 | 44 KB | 12 | 29,075 | 10 | 0 | 2,215,687 |
| 150,000 | 54,500 | 44 KB | 18 | 70,500 | 11 | 0 | 3,234,226 |
| 150,000 | 54,500 | 30 KB | 25 | 70,500 | 7 | 0 | 3,944,995 |

**What intake needs to decide** [inferred]:
1. **The 100k bound is near its floor.**
   - With a head near 54.5k, `W` is 20,500, below the 25,000 at which the launcher warns (rule
     4). The run works only with turns small enough for `W` to hold one, about 30 KB.
   - Larger turns hit `bound_too_small` and go over the bound, which would fail acceptance 1 for a
     reason that is the bound's, not the design's.
   - The measured head of the actual run decides this. The harness should be re-run in dry-run
     mode with it before approval.
2. **Every turn rotates at a 100k bound.** Each epoch then holds about one turn, so acceptance 4
   has almost nothing to measure outside the probe turn's tool loop. A 150k bound (the contract
   default) gives epochs of several turns and a meaningful cache rate. But 2.5 times the bound
   means 375k of history, which costs about 3.2M to 3.9M.
3. **Baseline.** The recommended baseline sends the CLI's final local history, from its session
   JSONL, as one direct request with the same probe turn: about 0.31M tokens instead of 3.5M for a
   plain CLI rerun.
   - On the rig route it is labeled a no-replayed-thinking baseline, since thinking is stripped or
     dropped either way.
   - The CLI's exact system prompt is not in the JSONL in a replayable form, so the replay uses
     the request shape the CLI recorded (`api-request-shape`). That is not yet built; it is open
     question 3.

## Live H1, attempt 1: aborted (2026-10-16)

- **What ran:** main `dc9ffe3` (PR #60's merge), Claude Code 2.1.301, `claude-haiku-5-5`, bound
  150,000, H1 approval 2,852,611. `check-credential` passed at that SHA just before the start.
- **What stopped it:** the run started at 02:29:18Z and stopped 7 seconds later. The only 401 in
  the session log was `HEAD /api/hello`, Claude Code's unauthenticated connectivity probe, which the
  owner's proxy answers with 401. The harness's auth watcher stopped on any 401 in the session log,
  so it ended the run on the probe. It was not a credential failure. The stub dry runs never saw
  it, because the stub answered `HEAD` with 200.
- **Spend:** the harness recorded 0, and no `/v1/messages` response was logged. Claude Code's JSONL
  shows its first model request had been issued when the process was stopped, so up to one first
  request (about 49,000 to 65,000 input tokens, modelled) may have been processed upstream. Intake
  counts it as spent: the total line becomes 3,395,292.
- **Fix:** the auth watcher stops only on a 401 or 403 for a `POST` to a model path
  (`contexto.proxy.TRANSFORMED_PATHS`), as a request record or an `upstream_4xx` event, and still
  stops on the first one. Acceptance 1's
  zero-4xx counts model requests only, and other 4xx are reported apart (see Detectors).
- **Artifacts** (private, never in Git): `.contexto/runs/acceptance-20261016T022918Z/`.

## Live H1 6x9, 07:08Z: aborted by a harness error (2026-10-16). NOT A VERDICT

- **What ran:** main `7a9fa16` (PR #119's merge), Claude Code 2.1.301, `claude-haiku-5-5`, bound
  150,000, D27 6x9, approval 6,873,612 (the gate accepted it; H1 cap 4,001,059). Start 07:08:59Z,
  stop 07:10:20Z.
- **What stopped it:** `run.py live` built `driver.Run`'s per-turn spend estimates as the content
  turns plus one probe entry, the single-probe-turn shape from before D27: 19 entries for 24 turns.
  `Run.turn` reads `estimates[i]` before sending turn `i`, so probe turn 2 raised `IndexError` before
  it was sent. `live` caught only the guard's own stops, so the error escaped and neither the H1
  record nor the answers file was written. Dry run #3 could not see it: `stubrun.py` sent the turns
  with `transport.send` and never used `driver.Run`.
- **Spend:** logged 1,973,105 (24 `POST /v1/messages`, all 200); 2,110,172 with the in-flight
  allowance `run.h1_spend` adds on an abnormal stop. Nothing was in flight, since the error fires
  before the send. Intake closed this approval line at 2,110,172.
- **Turns done:** the 18 content turns and probe turn 1. Probe turns 2 to 6 were never sent.
- **Partial acceptance 1** (`run.py check` on the partial log): every verdict true; 5 rotations,
  6 epochs, max input 105,076; the one 4xx was `HEAD /api/hello` 401, a non-model path.
  Acceptance 3 was not reached (probe turn 1 searched and did not recall). Cache 17 of 18.
- **Fix (live-path fix, intake 07:11Z):** one estimate per turn, the largest request of each
  turn (`budget.turn_estimates`), and `live` refuses when the count differs from the script's
  turns. The H1 function, `run.run_h1`, writes the record and the finished probe turns' answers
  however the run ends and re-raises a harness error after that. A stopped run's answers go to
  `acceptance-<session>-answers.partial.txt`, never the full-run name, and `score` refuses a
  stopped run unless asked for a PARTIAL score. The stub dry run calls the same
  `run_h1` with the stub as upstream, and checks that its runs took every live-only path (see
  the dry-run section).
- **Artifacts** (private, never in Git): `.contexto/runs/acceptance-20261016T070859Z/`.

## Acceptance 3 credit (contract PR #95)

```
uv run python spikes/m2/acceptance/run.py check --session-log ... --cc-jsonl ... --session-dir ... --answers ...
```

An answer counts as recall when it reached the model through Contexto's tools: a `contexto_recall` of
its page, or a `contexto_search` hit whose returned lines hold it (B15). `recall_credit.py` reads
nothing from the transcript or tool output. It takes each call's returned ranges from the
content-free `mcp_call` events: B15's `returned: [{page_id, start, end}]`, 0-based characters into
`render_page(page)` with the end exclusive, or, for a recall, `[offset, offset + chars)`. It finds
each paged-only plant's line in its stored page's rendered text. A probe is credited when it was
answered correctly and its whole line lies inside the union of the ranges returned for that page
(merged when they overlap or touch, since B16 pages a recall over several calls), from calls logged
before the last Messages request (the answer). `recall_credited` is the verdict.

On the live H1 rerun (`acceptance-20261016T044723Z`): q_p_01 and q_p_02 are credited through the
recall of the turn-0 page (range 0 to 30,651; plants at 27,057 and 9,706). q_p_03 and q_p_04 were
not returned (plants at 33,800 and 38,598) and were answered unknown.

## Search reachability (offline, zero spend)

```
uv run python spikes/m2/acceptance/run.py reach --session-dir <CONTEXTO_HOME>/sessions/<id>
```

Intake (2026-10-16): before a live rerun, show that `contexto_search` can reach every probe's answer,
so a miss can be attributed to the model and not to the index or the search. `reach.py` runs the
store's own search (`search_pages`, exact case-sensitive substring) on a session's stored pages with
each probe's key terms:
- the probe's `subjects` (its config key, chain subject, or action item id; for a multi-hop probe,
  every hop's name, which the model learns hop by hop);
- for a probe with none (an obligation set), the identifiers its question names.

A probe is reachable when every statement it needs is in the context window (on no page) or on a
page one of its terms returns:
- multi-hop: every statement;
- obligation set: each id's last statement;
- otherwise: the statements holding the answer, or the last one.

Pages are mapped to script turns by content (the turn's opening text inside the page), not by the
page's turn number, which can drift when carried text is paged. Drift, unmatched, and ambiguous
pages are reported (H1 rerun store: none), and reach fails closed on them: such a page's number and
candidate turns count as paged, never as in the window, and `run.py reach` exits 1. A statement on no page counts as in the window only if it sits in a
script turn the store did not page out, and hits are cut at `contexto_search`'s limit (`SEARCH_LIMIT`, 50 ids). It reports the rate
per category with counts, the rank of each hit (search lists newest first), and every unreachable
probe. `stubrun.py` also reports it on the dry run's own store, and `--inflate N:FACTOR` makes the
stub report inflated usage from the n-th request so a real over_bound is logged; on main with PR #76
the detectors count it from the request entries' `usage_events`. On the H1 rerun's store (`acceptance-20261016T024733Z`, 15 pages): 54 of 54
reachable.

| Category | Probes | Reachable | Needing a paged statement |
|---|---|---|---|
| chain | 30 | 30 | 13 |
| obligations | 6 | 6 | 6 |
| multihop | 8 | 8 | 8 |
| distractor | 6 | 6 | 6 |
| paged_only | 4 | 4 | 4 |

## Tests

`uv run --with pytest pytest spikes/m2` runs the harness tests with no live calls (the count changes
with each PR, so it is not kept here). Most use synthetic
fixtures; `test_engine_log.py` drives the real proxy and rewrite engine against a stub upstream,
once with JSON replies and once with SSE replies, since the acceptance session streams.
They cover:
- **Script:** determinism, mix, restatement, paged-only placement and sizes.
- **Budget:** rule 4 retention, `bound_too_small`, the estimator, and the worst-case arithmetic.
- **Detectors:** a clean session; compaction, unlogged over-bound, requests without usage, 4xx
  and cache misses all flagged; a torn last line.
- **Driver:** turn sequencing, the guard stopping before the cap, the command shape, refusing a git
  scratch directory, and the environment.
- **Run:** the pass rule, live refusal without budget, and the dry-run output.

## Replay baseline (acceptance 2)

```
uv run python spikes/m2/acceptance/run.py replay --cc-jsonl <H1 session JSONL> --out <private dir> \
    --approved 477681 --seat-settings <seat settings> [--dry]
```

Intake chose this order (2026-10-16): run H1, then send the baseline as one direct request. It is
H1's probe-turn request as Claude Code built it, read from Claude Code's session JSONL in the H1
run's isolated config directory:
- `api-request` records are deltas: `base` (the previous request's id), `keep` (messages kept from
  it), and `tail` (hashes of the new messages). A hash is the sha256 of the message as sorted
  compact JSON, and resolves to an `api-request-blob` (the session-start messages and per-turn
  reminders) or a `user` or `assistant` record. Claude Code writes one assistant record per
  content block (thinking, then text) under one message id; merged, they hash to what it sent.
- `api-request-shape` (matched by `shapeHash`): the system prompt and tools; the request's
  `params`: model, betas, thinking, output config.
- The first request that holds the probe turn is replayed with all its messages, system messages
  included. A hash that resolves to nothing stops the replay; nothing is guessed or synthesized.

It goes straight to the owner's proxy, bypassing Contexto.
- **No replayed thinking.** Thinking blocks are stripped, so it is labeled a no-replayed-thinking
  baseline.
- **No tool loop.** Contexto's tools are removed and `tool_choice` is `none`, so the model answers
  from the context alone. The probe turn's own tool calls in H1 are not part of the baseline.
- **Nothing edits the context.** `context_management` is dropped.
- **Intake's check, no silent trimming.** `count_tokens` on the exact request must fit
  `claude-haiku-5-5`'s 1,000,000-token context window with `max_tokens` (32,000, the guard's
  reserve), and the approval; otherwise the replay stops before sending and reports why.
- **Credential and headers.** The key comes from the seat's apiKeyHelper at call time and is held in
  memory for the request headers only. The owner's proxy refused direct requests with urllib's user
  agent (403) and with the key in `x-api-key` alone (401). Intake approved this set (2026-10-16
  03:24Z), which it accepted: the user agent `contexto-acceptance-harness/0.1
  (+VentureForgre/contexto)`, and the key in both `x-api-key` and `Authorization: Bearer`. Nothing
  presents the harness as Claude Code. The first 401 or 403 stops the replay. Tests check that the key
  never reaches stdout, stderr, or any file the replay writes.
- **Beta set (label for the baseline).** The replay sends the betas H1's probe-turn request recorded,
  minus every `claude-code-*` beta (intake, 2026-10-16: no Claude Code client marker on a direct
  harness send). For H1's run (Claude Code 2.1.301) that is these 10: interleaved-thinking-2025-05-14,
  thinking-token-count-2026-05-13, context-management-2025-06-27, prompt-caching-scope-2026-01-05,
  per-turn-control-2026-07-01, mid-conversation-tool-changes-2026-07-01, effort-2025-11-24,
  dangerous-tool-use-2026-09-03, thinking-display-updates-2026-08-18, afk-mode-2026-01-31. Dropped:
  claude-code-20250219. `replay.harness_betas` applies the same filter to every direct send. Feature
  betas such as afk-mode-2026-01-31 and dangerous-tool-use-2026-09-03 are kept on purpose: D23 removes
  only client identity markers, and the feature betas keep the baseline close to H1's real request
  (intake, 2026-10-16 05:30Z).
  Caveat on record: the recall experiment's four sends (2026-10-16 03:24Z to 03:25Z) still carried
  all 11, claude-code-20250219 included, as H1's own requests did.
- **Output, private only.** `baseline-answers.txt` and `replay-result.json` (sizes, the token
  count, usage, stop reason, block counts). `run.py score --h1 ... --h1-record ... --baseline ...
  --baseline-result ...` compares the two answer sets. It refuses unless both sides are full runs:
  the H1 record shows `stopped` null and all six probe turns completed, and the replay result shows
  no stop and six requests answered 200. A stopped replay writes `baseline-answers.partial.txt`,
  never the full-run name. It also refuses unless each answers file is the one its record names
  (`answers_file`, beside the record), the H1 record's `session` matches its file name, and the
  replay result's `h1_session` (from `--h1-record`) is that session, so no record can vouch for
  another run's or a partial answers file. `--partial` scores a stopped run anyway, labeled
  PARTIAL, with no acceptance 2 verdict; it never loosens the ties (reviewer-2 on PR #123).

`--dry` builds the request and prints its sizes with no calls. On the H1 rerun's JSONL
(`acceptance-20261016T024733Z`) it decodes all 56 messages of the probe-turn request (18 turns, each
with its reminder, then the probe turn and its reminder), with 21 tools and about 814 KB.

**Replay of the live H1 rerun (2026-10-16, main `7da8f78`).**
- **JSONL-copy workaround (disclosed, accepted).** At that SHA the replay decoded every request record
  first, and the probe turn's own tool-loop requests (21 and 22) named assistant messages the JSONL
  did not hold, so it refused. The probe-turn request (20) decoded completely. The baseline ran from a
  private copy of the JSONL without records 21 and 22, which builds the same probe-turn request. The
  replay now stops decoding at the first request that holds the probe turn, so the original JSONL
  builds it directly (verified: same 56 messages, 814,024 bytes).
- **One field changed (intake 04:56Z).** Claude Code recorded `thinking.display: "updates"`, which
  `count_tokens` rejects (`summarized` or `omitted` only). The baseline used `omitted`; display
  controls only what the response returns, not the input or the reasoning. The replay now makes this
  change itself and reports it in `changes_from_recorded`.
- **Result.** count_tokens 437,447 input; spend 447,659; the baseline scored 44 of 54. The beta set
  is the 10 listed above, labeled a no-replayed-thinking baseline.

## Open questions

1. **Tokenizer.** The 1.8 bytes per token rate comes from M0's measurements. The replay's
   `count_tokens` gives the exact figure for this content on `claude-haiku-5-5`; the guard stops H1
   at the approval if real usage runs higher.
