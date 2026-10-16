# M0 harder probes: design, calibration pilot, and run budget (L2f)

Status: phase 1 design plus a calibration pilot (lane 2, packet L2f). Author: build.builder-4.
Date: 2026-10-15. Code: `spikes/m0/fidelity/l2f.py`, tests in
`spikes/m0/fidelity/tests/test_l2f.py`. Claims are tagged **[measured]**, **[vendor-documented]**, or
**[inferred]**.

## Why

Every condition measured so far hit the ceiling:
- the seat at about 99k and 257k: 96/96 each;
- the replay at about 41k and 92k: 96/96 in both arms.

Those probes cannot locate where quality slips (`docs/research/M0-quality-vs-context.md`). This
set is built to sit near a 75% to 92% baseline at the smallest size, intake's target, so that drops
become visible.

## Probe types

Every probe carries a type and a difficulty tier. The pilot used two disjoint groups (one fork
each), with 34 probes per group.

| Type | What is planted | What is asked | Tiers |
|---|---|---|---|
| multihop | service to owner; owner to team; team to region, each link in a different turn | the region reached by following the links | 2-hop, 3-hop |
| chain | a value set, then revised 2 or 5 times, spread over the session | the current value, or the one right before it | depth 3 or 6, current or previous |
| obligations | items opened; some closed; some closed then reopened; some closed late | one item's status, or which items in a set of 3 are still open | single, set |
| distractor | one true config line among 2 or 5 look-alikes (same service, near-equal shard numbers, same value stem) | the value of the exact key | 2-dups, 5-dups |
| reasoning | a 12-row table with an instruction to compute a checksum and reply only `OK` | the checksum, later | 1-step, 2-step |

**Controls**
- **Scoring** is deterministic and exact after normalization. Set answers compare as sorted id
  lists. A wrong answer equal to an earlier chain value, or to a distractor's value, counts as
  **stale**.
- **Contamination.** `contamination()` checks that no subject is restated outside its own
  statements and question, and that no probe turn contains an answer. The plan has 0 violations,
  and a test confirms an injected restatement is caught.
- **Restatement.** Build replies are fixed (`OK`). The pilot reports output tokens per request,
  and restatement inside thinking cannot be checked.

## Calibration pilot [measured, 2026-10-15]

**Setup**
- `claude-haiku-5-5`, adaptive thinking, effort `medium`, direct API through the rig proxy.
- The credential came from the existing helper at runtime only.
- Three build turns, about 12k tokens of context, then one fork per probe group.
- The binding header was sent with the mismatch field unset, so every response reports dropped
  thinking.
- Output: `.contexto/spikes/l2f/pilot-*/summary.json`.

**Spend:** 46,031 tokens, under the 60,000 allowance.

| Request | Total tokens | Thinking blocks sent | `input_transformations` |
|---|---|---|---|
| B1 | 1,799 | 0 | none |
| B2 | 6,376 | 0 | none |
| B3 | 10,355 | 1 | none |
| P_a (group a probes) | 13,770 | 1 | none |
| P_b (group b probes) | 13,731 | 1 | `thinking_dropped`, `end_user_binding_mismatch` |

**Accuracy**

| Type, tier | Group a | Group b | Both |
|---|---|---|---|
| multihop 2-hop / 3-hop | 4/4, 4/4 | 4/4, 4/4 | 16/16 |
| chain depth 3, current / previous | 2/2, 2/2 | 2/2, 2/2 | 8/8 |
| chain depth 6, current / previous | 2/2, 2/2 | 0/2, 1/2 (3 stale) | 5/8 |
| obligations single / set | 4/4, 4/4 | 4/4, 4/4 | 16/16 |
| distractor 2-dups / 5-dups | 4/4, 4/4 | 4/4, 4/4 | 16/16 |
| reasoning 1-step / 2-step | 1/1, 1/1 | 1/1, 1/1 | 4/4 |
| **Total** | **34/34** (Wilson 0.898-1.00) | **31/34** (0.770-0.970) | **65/68** (95.6%) |

**What it shows**
- At about 12k context, only depth-6 supersession chains miss, and every miss is stale: an older
  value.
- Multi-hop at 3 hops, 5 near-duplicates, open/close sets and the reasoning tasks are all at
  ceiling. The pilot overall is above the 75% to 92% target.
- **Thinking on this route is inconsistent.** Two requests carried a replayed thinking block with no
  drop reported, and one had it dropped (`end_user_binding_mismatch`). That fits an identity that
  varies per request on this route [inferred]. Lane 1 saw the same inconsistency (L1-e, PR #21,
  `docs/research/M0-interception.md` section 4).
  - The drop coincides with group b's three misses, but one fork with different probes is
    confounded, so it supports no claim.
  - **The route does not verifiably keep thinking.**
- **The reasoning probes do not test thinking here.** The table stays visible in an append-only
  history, so the model can recompute at probe time. B2's whole output was 89 tokens, too few to
  have computed four checksums. Reasoning probes become thinking-dependent only when the table is
  paged out, which is phase 2's bounded window [inferred].
- The pilot ran at about 12k context. Difficulty will rise with size, which is what the run is
  for [inferred].

**Deep supersession chains fail first.**
- At about 12k context, the depth-6 chains were the one probe type that failed: 3 of 8 missed, and
  all 3 gave an older value [measured, small sample].
- The model reached for a stale value even with the current one in context. A pinned block that
  lists only current values (policy doc, section 2) would address this failure directly [inferred,
  intake's reading].
- The sample is small, so the run should confirm it at size.

### Pilot 2 [measured, 2026-10-15]: missed, no result

- **Setup.** The recommended mix scaled to 60 probes at about 15k context, effort `medium` (`harder.py
  pilot`).
- **No answer.** The probe fork reasoned until `max_tokens`: 16,000 output tokens, all thinking,
  and no answer text. There was nothing to score.
- **Over the cap.** Spend was 50,327 tokens, 5,327 over the 45,000 cap. The guard before each call
  reserved only a quarter of `max_tokens` for output; it now reserves all of it.
- **Estimates ran low.** Inputs were 20% to 38% above estimate for this content: 15,841 against
  about 13,200, and 18,482 against 13,391.
- Per intake's rule the run was not started. Options went to intake: a smaller pilot at effort
  `low` with a larger output allowance, exact sizing with the token-counting endpoint, and a run
  cap near 950k.

### Pilot 3 [measured, 2026-10-15]: above target

**Setup** (`harder.py pilot3`):
- 30 probes in the recommended mix (scaled 0.25), effort `low`, `max_tokens` 24,000.
- Context sized with the token-counting endpoint, ending at 15,002 counted tokens for the probe
  request.
- The guard reserved the full output allowance.

**Spend:** 33,142 tokens, under the 60,000 cap. Build turn 13,846; probe fork 19,296, with 4,169
output tokens and an answer this time.

**Sizing.** Character estimates ran about 20% low: 13,509 estimated against 16,169 counted before
rescaling. Counting fixes that before any generation.

**Accuracy: 29/30 (96.7%, Wilson 0.833-0.994)**, against a 75% to 92% target, so the pilot
**missed high**.

| Type, tier | Score |
|---|---|
| multihop 3-hop / 4-hop | 3/3, 3/3 |
| chain depth 6, current / previous | 3/3, 2/2 |
| chain depth 8, current / previous | 3/3, 1/2 |
| distractor, 8 look-alikes | 6/6 |
| obligations single / set of 6 | 3/3, 3/3 |
| reasoning 1-step / 2-step | 1/1, 1/1 |

- **Thinking.** The probe fork's replayed thinking was dropped (`end_user_binding_mismatch`), as
  before on this route.
- **Across pilots 1 and 3,** at 12k to 15k context, the only probes that fail are deep supersession
  chains: 4 misses in 18 depth-6 and depth-8 probes. Every other type, including 4-hop joins, 8
  look-alikes and sets of 6, stays at ceiling.
- At these sizes Haiku 5.5 is near ceiling even on the harder set. A baseline in 75% to 92% would
  need either chains as the main probe type, or larger contexts.
- Per intake's rule, pilot 3 missed and work stops here. Intake takes the best available set.

**Recommended mix for the run** (120 probes per checkpoint), aiming at 75% to 92% at 41k
[inferred, to be confirmed]:

| Type | Count | Tiers |
|---|---|---|
| chain | 40 | depth 6 and depth 8, half current and half previous: the only type that missed |
| distractor | 24 | 8 look-alikes, adjacent lines, values one digit apart |
| multihop | 24 | 3-hop and 4-hop, links far apart |
| obligations | 24 | sets of 6, items with two close and reopen cycles |
| reasoning | 8 | kept for phase 2, where the table is paged out |

A second pilot of about 45k tokens with this mix would confirm the target before the run. It needs
the same approval path.

## Run budget (no run) [inferred]

**Recommended setting.** A direct-API replay for all three sizes, because:
- the seat cannot reach 41k: its fixed head is 81,808;
- only a replay can run the stripped arm.

The seat stays the reference for the CLI as the owner uses it.

**Method.** Same as L2d: one growth request per checkpoint, then one probe fork per arm. Each probe
is about 30 tokens of question and 20 of answer, plus about 1,500 of thinking per fork. Measured
contexts ran 6% to 9% above these estimates in L2d, so the totals carry a 10% margin.

| Scope | Arms | Requests | Total | Total +10% | Worst gate (cumulative + 1.5 x next turn) |
|---|---|---|---|---|---|
| 41k, 92k, 243k | preserved only | 6 | 775,100 | 852,610 | 900,350 |
| 41k, 92k, 243k | preserved + stripped | 9 | 1,173,600 | 1,290,960 | 1,424,100 |
| 41k, 92k | preserved only | 4 | 281,400 | 309,540 | 331,150 |
| 41k, 92k | preserved + stripped | 6 | 429,400 | 472,340 | 528,900 |

**Gates**
- No 243k replay without intake's decision after the seat's 243k result.
- **The stripped arm is conditional on a route that verifiably keeps thinking**: every response's
  `input_transformations` shows no `thinking_dropped` for the preserved fork. The rig route failed
  that check in the pilot (P_b). Until lane 1 finds a stable-identity route, only the preserved
  arm (a baseline) can run, and the strip comparison stays open.

## Open questions

1. A stable-identity route for the strip arm (lane 1's identity check).
2. A second pilot (about 45k) with the recommended mix, to confirm 75% to 92% before the run.
   Done: pilots 2 and 3 (below the pilot 1 section) did not reach it.
3. Reasoning probes need phase 2's paging to depend on thinking. In an append-only replay they
   test only recomputation.
