# M2: cache-aware rotation, offline replay (packet A28)

Proposal only. Zero spend: every number here comes from replaying recorded requests through the engine
offline. Nothing was sent to a model. Numbers only: no run content appears in this document, the
script's output, or Git.

Script: `spikes/m2/acceptance/cache_rotation.py` (tests: `spikes/m2/acceptance/tests/test_cache_rotation.py`).

## Headline

The Codex A22 run did not rotate on every turn because of its small bound alone. Each Codex turn
ran as a new `codex exec`, which started a new proxy: 21 `proxy_start` events for 21 turns and 44
requests. A new proxy's engine has no epoch state, so its first request rotated from epoch 0 again:
19 rotations, all on the first request after a restart. Each rotation re-sent the retained window
uncached behind the 9,088-token head.

| Run, model | Candidate | Rotations | Input tokens | Cached | Uncached | Modeled cost (USD) | vs baseline |
|---|---|---|---|---|---|---|---|
| Codex A22, gpt-6-astra, bound 64,000 | baseline (as run) | 19 | 919,998 | 75.5% | 225,723 | 3.626 | |
| | (a) append-only index | 19 | 919,998 | 75.5% | 225,723 | 3.626 | 0% |
| | (b) window target 0.25 | 19 | 794,149 | 73.6% | 210,019 | 3.320 | -8.5% |
| | (c) resume epoch state across restarts | 9 | 1,009,027 | 83.5% | 166,888 | 3.038 | -16.2% |
| | (c) + (b) | 9 | 958,122 | 83.3% | 159,641 | 2.904 | -19.9% |
| | reference: bound 100,000 (modeled) | 11 | 1,700,866 | 78.5% | 365,005 | 6.009 | +65.7% |
| | reference: bound 150,000, no rotation (modeled) | 0 | 3,655,890 | 96.9% | 113,834 | 5.075 | +40.0% |
| Claude run #3, claude-haiku-5-5, bound 150,000 | baseline (as run) | 6 | 3,725,391 | 85.3% | 548,954 | 0.1581 | |
| | (a) append-only index | 6 | 3,725,391 | 85.3% | 548,954 | 0.1581 | 0% |
| | (b) window target 0.25 | 6 | 3,420,435 | 84.5% | 532,012 | 0.1531 | -3.2% |
| | (b) window target 0.10 | 6 | 3,328,023 | 84.2% | 526,878 | 0.1516 | -4.1% |
| | (c) resume epoch state | 6 | 3,725,391 | 85.3% | 548,954 | 0.1581 | 0% (no restarts in this run) |
| | reference: bound 100,000 (modeled) | 19 | 2,481,850 | 71.6% | 705,480 | 0.1173 | -25.8% |

Cached shares and costs are modeled for every row, baselines included (see "Cache model"). Window
target 0.10 equals 0.25 on Codex: both take the minimal window at every restart rotation.

## Inputs

Read in place, never copied:

- **Codex A22** (bound 64,000, `contexto run codex`, gpt-6-astra). Builder-2's durable run directory:
  the session log, the store, and Codex's rollout in the session's `codex-home`.
- **Claude acceptance run #3** (main fbb1303, bound 150,000, claude-haiku-5-5). Builder-4's run
  directory: the session log and Claude Code's session JSONL.

## Method

- **Requests, Codex.**
  - Each request's `input` is rebuilt from Codex's rollout: every response item before the request's
    first output item, without the rollout-only `internal_chat_message_metadata_passthrough` field.
  - The rollout does not keep the head (instructions, tools, and parameters), so it is a
    constant-size filler, sized to request 1's logged `original_request_bytes`.
  - The rebuilt requests are 0 to 15 bytes from their logged sizes.
- **Requests, Claude.** Lane B's `classa.decode_requests` and the body shape of
  `classa.original_bodies`, for all 44 requests, as builder-4's B27 replay does. Every request is
  288 bytes from its logged size, the known wire-only constant.
- **Engine.**
  - Each candidate runs main's `RewriteEngine` with a fresh `SessionStore` in a temporary directory.
  - A new engine on the same store starts at every logged proxy restart, as in the run. Candidate
    (c) keeps one engine instead.
  - The store has a fresh key, since run #3's `page.key` is not copied (manager-1's ruling). Page ids
    therefore differ from the original run but have the same length (`pg_` plus 16 hex), so byte
    and token counts are unchanged.
- **Usage fed back** after each request: the run's measured input plus the byte difference from the
  baseline replay, at the run's bytes per token (4.2702 on Codex, 2.399 on Claude). This is builder-4's
  B27 method, so a candidate's decisions see what it would have sent. The model's behaviour (tool
  calls, results, replies) is held fixed.
- **Baseline check.** At each run's own config the replay reproduces every logged rotation:
  - Codex: 19 rotations, at the same requests (3 to 16, 22, 26, 29, 34, 39), with bytes within 597
    of the log.
  - Claude: 6 rotations at requests 4, 7, 10, 13, 16, and 27, at the 288-byte constant.

## Cache model

This is an assumption, checked against both runs. A request reads one of two things from cache:

- the whole prompt of the longest earlier request whose prompt is a prefix of its own (at that
  request's tokens);
- otherwise, the head alone, if it is unchanged, at the run's measured head tokens.

The head is what each run read after every rotation: 9,088 tokens on Codex and 24,601 on Claude.
Prompts are compared in the order the provider caches them (head first), without `cache_control`
markers. A message whose content is one text block counts the same as its string form, because
Claude Code sends the newest system message as a cache-marked one-block list and as a string on the
next request.

How well it fits:

| Run | Measured cached | Modeled cached (baseline) |
|---|---|---|
| Codex A22 | 680,832 (74.0%) | 694,275 (75.5%) |
| Claude run #3 | 2,677,349 (71.9%) | 3,176,437 (85.3%) |

The Codex fit is close. Per request, most differences fall within OpenAI's 128-token cache
granularity, and one full miss (request 39) is not modeled.

The Claude gap fits A13's finding that the owner's route spreads requests over at least 3 upstream
cache identities. The model assumes one cache, so candidates are compared with each other within the
model, not with the measured run.

A plain longest-shared-prefix model overstates the reads after a rotation. On Codex it credited the
pinned part of the preamble, about 2,400 tokens a rotation, which both runs' measured reads show was
not cached.

## Prices

USD per million tokens, from `docs/research/M0-cost-model.md`, "Prices" table (vendor-documented
2026-10-15). Every uncached input token is counted as a cache write.

| Model | Input | Output | Cache read | Cache write | Long context |
|---|---|---|---|---|---|
| gpt-6-astra | 10 | 50 | 1 | 12.5 | input ×2, output ×1.5 above 272,000 |
| claude-haiku-5-5 | 0.1 | 0.5 | 0.01 | 0.125 (5 min TTL) | ×5 above 100,000 |

## Candidates

### (a) Prefix stability: an append-only, oldest-first index

The prototype lists pages oldest first, so a rotation appends index lines and the previous index
stays a byte prefix. It changed nothing on either run. Under the measured cache behaviour a rotation
reads the head only, because the retained window starts at a different turn after every rotation,
and no cached checkpoint ends inside the preamble.

Accuracy risk: past the index budget the oldest-first order would omit the newest pages, the ones
most likely needed. **Not recommended.**

### (b) Rotation hysteresis: a lower window target (`window_fraction`)

After a rotation, a smaller retained window means fewer uncached tokens on the rotation request and
more turns before the next trigger.

- **Codex at 0.25:** 8.5% cheaper, 13.7% fewer input tokens.
- **Claude:** 3.2% cheaper at 0.25, 4.1% at 0.10.

Accuracy risk: less recent history in view right after a rotation, so the model needs recall sooner.

| Run | Mean upstream messages, baseline | at 0.25 | at 0.10 |
|---|---|---|---|
| Codex | 13.9 | 8.3 | |
| Claude | 41.0 | 40.2 | 36.1 |

The minimum turn room improved on Codex (12,934 to 14,334) and was unchanged on Claude (15,132).

### (c) Resume epoch state across proxy restarts

Modeled as one engine kept across restarts: a proxy that resumes the session's epoch, `k`, and
preamble instead of rotating from epoch 0.

- **Codex:** rotations fall from 19 to 9, all for the room. Cached share rises from 75.5% to 83.5%,
  and cost falls 16.2%. With (b) at 0.25, cost falls 19.9%.
- **Claude:** no effect, since run #3 had no restarts.

The view is what one long-lived proxy would give. A restart no longer re-pages or reshapes the
window.

Accuracy risk: the larger windows that are no longer rotated away lower the minimum turn room from
12,934 to 9,059 on Codex, below the 12,000 `min_turn_room`. With (b) it is 11,456.

### Reference: larger bounds (modeled)

- **Codex at 100,000** costs 65.7% more: it still rotates 11 times on restarts, with larger windows.
- **Codex at 150,000** never rotates, which is plain Codex at the same request points. It costs 40.0%
  more than Contexto at 64,000 under this cache model.

  A27 compared against a modeled plain-run shape (probe turns without tool loops), so its result
  differs. The two comparisons use different request points.
- **Claude at 100,000** is 25.8% cheaper than at 150,000. No request crosses Haiku's 100,000 long-context
  tier (7 do at 150,000), but it rotates 19 times, 11 of them `window_minimal`. Its minimum turn room
  is 6,745, below `min_turn_room`.

### What was not measured

Whether a probe's paged-only content would leave the view or the index under a candidate was not
evaluated. It needs plant positions in the stored pages, which this replay does not read. Rotations
at the same requests (a, c on Claude) keep the same pages. For (b) and the 100,000 reference, a live
check should score paged-only probes.

## Recommendation

1. **Build (c): a proxy restart resumes the session's epoch state.**
   - Persist `epoch`, `k`, `strip_upto`, and the preamble (pinned and index text) with the session.
     Lane B's `retained.json` (PR #128) already records the retained turns per epoch. On the first
     request after a restart, reuse them instead of rotating from epoch 0.
   - This is the largest gain on Codex (16.2%), adds no model-visible change beyond what one
     long-lived proxy gives, and applies to every per-turn `codex exec` use.
   - It needs a contract change in the engine (lane A) and the store (lane B).
2. **Consider (b) at 0.25 for small bounds only.** It is worth 8.5% on Codex at 64,000 and 3.2% on
   Claude at 150,000, at the cost of fewer recent turns in view after a rotation.
3. **Do not build (a).** It shows no gain under the measured cache behaviour and has a newest-pages
   risk.
4. **Bound choice.** On Haiku the 100,000 long-context tier dominates cost at 150,000. A default bound
   change needs a live accuracy check first, because of the 6,745 minimum turn room and the 11
   `window_minimal` rotations at 100,000.

## What a live check would need

- **(c):** rerun the A22 Codex protocol, per-turn `codex exec` at bound 64,000, with a resume
  prototype behind a flag. Compare rotations and measured `cached_tokens` against A22, and score the
  same probes.
- **(b):** one Codex and one Claude acceptance run at `window_fraction` 0.25, scoring paged-only
  probes and recall calls against the baseline runs.
- **Claude bound:** one H1 run at bound 100,000 on Haiku, checking accuracy against run #3.
