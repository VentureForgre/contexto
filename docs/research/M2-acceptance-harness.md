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
| 3. Recall: a paged-out-only probe answered, and the `contexto_recall` call in the log | 4 paged-only probes, plus the `mcp_call` events |
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
- The child environment drops OpenRig and parent-session variables and sets `CONTEXTO_HOME`.
- No `--settings` or `--mcp-config` of the harness's own is passed; the launcher supplies both.
- `--upstream` is passed explicitly: the owner's proxy (D12), from `$ANTHROPIC_BASE_URL` unless
  given.

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
- **Unverified requests (acceptance 1).** A `/v1/messages` request whose `usage` is null or
  missing cannot be checked against the bound, so it must not hide an over-bound request. Their
  indexes are reported in `unverified_requests`, and the `every_request_has_usage` verdict fails
  unless the count is 0. The usage comes from the JSON body or, for streamed replies, the SSE
  `message_start` and `message_delta` events.
- **4xx (acceptance 1).** Only model requests count (contract PR #61): `upstream_4xx` events and
  request records on the paths the proxy transforms (`contexto.proxy.TRANSFORMED_PATHS`:
  `/v1/messages`, its `count_tokens`, and `/responses`). Both must be zero. 4xx on any other path, such as Claude Code's
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

## Zero-spend dry run through the launcher (2026-10-16)

```
uv run python spikes/m2/acceptance/stubrun.py <seat settings> .venv/bin/contexto .contexto/spikes/<dir> [turn KB]
```

`stubrun.py` runs the whole acceptance script through lane A's real `contexto run claude` (PR #40)
against a local stub upstream that never forwards. The stub answers `OK`, as SSE when the request
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
| H1 acceptance run through the proxy (live refuses an approval below its worst case) | 2,185,257 | 2,852,497 |
| Full-context baseline, one direct replay request (recommended) | 449,643 | 477,643 |
| Full-context baseline through a plain CLI session (alternative) | 5,054,009 | - |
| **Total, H1 + replay baseline** | **2,634,900** | **3,330,140** |
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

Every acceptance verdict passed in both runs except `recall_called`, since the stub calls no
tools. The stub run's 40 KB total (2,387,214 / 3,082,454) sits about 248,000 tokens under the
approval table, so the guard never stops a run the stub run predicts.

**Recalls in the probe turn.** The probe turn's tool loop cannot rotate, so the budget prices 2
recalls. Intake made this a cost guard only (2026-10-16): the probe turn is product-realistic and
names neither Contexto's tools nor a cap ("If the conversation does not contain it, say unknown."),
so the preamble and page index must lead the model to recall. B9 (PR #56) sizes each recall at `W / 4` tokens times the smaller of the
session's measured bytes per token and 1.5, never above 32,000 characters, and at 12,000 characters
until the engine records `W` with `set_window`. The budget prices each recall at the largest chunk,
32,000 characters of log text, 17,778 tokens; at this run's `W` (62,000 to 85,000) B9 returns
13,000 to 17,600 tokens. `recalls_over_budget` reports recall calls beyond the 2 priced, across the
whole session; no verdict uses it.

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

## Tests

`uv run --with pytest pytest spikes/m2` runs 45 tests with no live calls. Most use synthetic
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

## Open questions

1. **Replay baseline.** It needs the CLI's request shape: either the JSONL's `api-request-shape`
   record, or an opt-in proxy dump of the last full request under the session directory.
2. **Budget and bound.** Decided by intake: bound 150k, budget up to 4,000,000. Pending: the
   zero-spend dry run with lane A's real `contexto run` and the measured head.
