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
contexto run --bound 100000 --session <id> claude -p --input-format stream-json \
    --output-format stream-json --verbose --model claude-haiku-5-5
```

- This is the launcher shape builder-1 proposed for lane A packet A3. Contexto's options come
  before `claude`, and everything after it passes through unchanged. It is confirmed when A3 lands.
- Each user turn is one stream-json line on stdin. A turn ends at the `result` event.
- The scratch directory must not be a git repository; the driver refuses one.
- The child environment drops OpenRig and parent-session variables and sets `CONTEXTO_HOME`.
- No `--settings` or `--mcp-config` of the harness's own is passed; the launcher supplies both.

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
  cache_read_input_tokens + cache_creation_input_tokens`. Any request with input over the bound
  must carry `over_bound: true`. One that does not is reported in `unlogged_over_bound` and fails
  the check.
- **4xx.** `upstream_4xx` events, plus any 4xx status on a request record. Both must be zero.
- **Epochs and cache (acceptance 4).**
  - The first request seen in each `epoch` starts that epoch.
  - Every later successful request in the epoch is cache-eligible, and it is a hit if
    `cache_read_input_tokens > 0`.
  - The rate and the miss indexes are reported. Explaining misses by the route is left to the
    analyst; see the cache-miss note below.
- **Rotations** are engine events inside `transform_events`, with builder-2's names. The harness
  counts any whose name contains "rotat", an assumption to confirm when the engine lands. Over-bound
  and resync events carry the epoch too.
- **Recall (acceptance 3).** `event: "mcp_call"` with `tool: "contexto_recall"` and `ok: true`,
  written by the MCP server (PR #24).

**Claude Code's session JSONL: compaction (acceptance 1).** A compaction writes a `system` record
with subtype `compact_boundary` and a `user` record with `isCompactSummary`. Both are counted, and
the count must be 0. [Inferred by lane 1 from the 2.1.301 binary; not vendor-documented.]

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

## Dry-run budget [inferred estimates]

```
python3 spikes/m2/acceptance/run.py dry-run   # 17 turns of 30 KB, bound 100,000, head 54,500
```

**Method**
- The proxy is simulated with contract rules 3 to 5. The window budget is `W = bound - headroom -
  F`, with `F` = head plus preamble, and rotation targets half of `W`. The newest complete turn is
  always kept, and the tool loop never rotates.
- Pasted log text is counted at 2.0 bytes per token. The contract's estimator gives 2.0 to tool
  output and 3.5 to other text. This content is numeric log text, which M0 measured near 0.55
  tokens per character [measured], so 3.5 would undercount it by about half.
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

## Tests

`uv run --with pytest pytest spikes/m2` runs 19 tests on synthetic fixtures, with no live calls.
They cover:
- **Script:** determinism, mix, restatement, paged-only placement and sizes.
- **Budget:** rule 4 retention, `bound_too_small`, the estimator, and the worst-case arithmetic.
- **Detectors:** a clean session; compaction, unlogged over-bound, 4xx and cache misses all
  flagged; a torn last line.
- **Driver:** turn sequencing, the guard stopping before the cap, the command shape, refusing a git
  scratch directory, and the environment.
- **Run:** the pass rule, live refusal without budget, and the dry-run output.

## Open questions

1. **Rotation names.** Builder-2's engine event names for rotation and resync, to replace the
   "rotat" match.
2. **Launcher flags.** The A3 flags (`--bound`, `--session`) are proposed, not landed.
3. **Replay baseline.** It needs the CLI's request shape: either the JSONL's `api-request-shape`
   record, or an opt-in proxy dump of the last full request under the session directory.
4. **Budget and bound.** Intake to approve the budget line and the bound (100k with small turns,
   or 150k), after a dry-run with the measured head.
