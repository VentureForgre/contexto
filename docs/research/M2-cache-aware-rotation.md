# M2: cache-aware rotation, offline replay (packet A28)

Proposal only. Zero spend: every number here comes from replaying recorded requests through the
engine offline. Nothing was sent to a model. Numbers only: no run content appears in this document,
the script's output, or Git.

Script: `spikes/m2/acceptance/cache_rotation.py` (tests:
`spikes/m2/acceptance/tests/test_cache_rotation.py`).

## Headline

The Codex A22 run did not rotate on every turn because of its small bound alone. Each Codex turn
ran as a new `codex exec`, which started a new proxy: 21 `proxy_start` events for 21 turns and 44
requests. A new proxy's engine has no epoch state, so its first request rotated from epoch 0 again:
19 rotations, all on the first request after a restart. Each rotation re-sent the retained window
uncached behind the 9,088-token head.

The candidates send different bytes from the baseline, and those differences are converted to
tokens at two bytes-per-token rates, which bound the result (see Method). Each range below is
"head-inclusive rate / content rate":

- Codex: 4.2702 (the run's average, head included) / 3.0193 (A27's calibrated content rates).
- Claude: 2.399 / 1.8274 (builder-4's B27 rate).

**Codex A22, gpt-6-astra, bound 64,000**

| Candidate | Rotations | Cached | Uncached tokens | Modeled cost (USD) | vs baseline | Min turn room |
|---|---|---|---|---|---|---|
| baseline (as run) | 19 | 75.5% | 225,723 | 3.626 | | 12,934 |
| (a) append-only index | 19 | 75.5% | 225,723 | 3.626 | 0% | 12,934 |
| (b) window target 0.25 | 19 | 73.6% / 73.8% | 210,019 / 198,175 | 3.320 / 3.145 | -8.5% / -13.3% | 14,334 |
| (c) resume epoch state | 9 | 83.5% / 82.4% | 166,888 / 183,740 | 3.038 / 3.269 | -16.2% / -9.8% | 9,059 / 7,454 |
| (c) + (b) | 9 | 83.3% / 82.2% | 159,641 / 173,558 | 2.904 / 3.080 | -19.9% / -15.1% | 11,456 / 9,515 |
| ref: bound 100,000 | 11 / 14 | 78.5% / 76.5% | 365,005 / 404,868 | 6.009 / 6.491 | +65.7% / +79.0% | 14,255 / 17,042 |
| ref: bound 150,000 | 0 / 5 | 96.9% / 88.8% | 113,834 / 358,895 | 5.075 / 7.428 | +40.0% / +104.9% | 11,166 / 6,663 |

**Claude run #3, claude-haiku-5-5, bound 150,000**

| Candidate | Rotations | Cached | Uncached tokens | Modeled cost (USD) | vs baseline | Min turn room |
|---|---|---|---|---|---|---|
| baseline (as run) | 6 | 85.3% | 548,954 | 0.1581 | | 15,132 |
| (a) append-only index | 6 | 85.3% | 548,954 | 0.1581 | 0% | 15,132 |
| (b) window target 0.25 | 6 | 84.5% / 84.2% | 532,012 / 526,713 | 0.1531 / 0.1516 | -3.2% / -4.1% | 15,132 |
| (b) window target 0.10 | 6 | 84.2% / 83.8% | 526,878 / 519,973 | 0.1516 / 0.1496 | -4.1% / -5.4% | 15,132 |
| (c) resume epoch state | 6 | 85.3% | 548,954 | 0.1581 | 0% (no restarts in this run) | 15,132 |
| ref: bound 100,000 | 19 | 71.6% / 74.1% | 705,480 / 571,173 | 0.1173 / 0.0991 | -25.8% / -37.3% | 6,745 / 10,220 |

Cached shares and costs are modeled for every row, baselines included (see "Cache model"). On
Codex, window target 0.10 costs the same as 0.25: both take the minimal window at every restart
rotation. (c) + (b) is the cheapest Codex candidate at both rates. Which single change saves more
depends on the rate: (c) at the head-inclusive rate, (b) at the content rate.

## Inputs

Read in place, never copied:

- **Codex A22** (bound 64,000, `contexto run codex`, gpt-6-astra). Builder-2's durable run
  directory: the session log, the store, and Codex's rollout in the session's `codex-home`.
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
  - The store has a fresh key, since run #3's `page.key` is not copied (manager-1's ruling). Page
    ids therefore differ from the original run but have the same length (`pg_` plus 16 hex), so byte
    and token counts are unchanged.
- **Usage fed back** after each request: the run's measured input plus the byte difference from the
  baseline replay, converted to tokens at a rate (`--rate`; builder-4's B27 method), so a
  candidate's decisions see what it would have sent. The model's behaviour (tool calls, results,
  replies) is held fixed.
- **Two rates.** The differences are conversation content, which packs fewer bytes per token than
  the head, so the run's average (head included) understates them. Every table gives both:
  - **Codex:** the average, 4.2702, and A27's calibrated text and tool-JSON rates, which work out to
    3.0193 on the no-rotation difference.
  - **Claude:** the average, 2.399, and the 1.8274 rate that builder-4's B27 replay used.
  - The content rate is the better estimate. The head-inclusive rate is kept as the other end of
    the range.
- **Turn room** in the tables is the bound minus the request's input tokens minus the headroom
  floor. It ignores output: at most 136 tokens per request on Codex A22 and 2,583 on Claude run #3.
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

The Codex fit is close. Per request, the model is never below the measured read:

- 21 of 44 requests are within 128 tokens, OpenAI's cache granularity, and 43 of 44 within 256.
- The exception is request 39, a full miss in the run (0 read), which the model counts as a head
  read (9,088).

On Claude the model is above the measured read by 499,088 tokens. By reviewer-1's breakdown, 17
requests carry 497,405 of that gap. In 9 of them the run's measured read equals the whole prompt of
an older request (2 to 5 back) rather than the newest, while the model reads the newest. This fits
A13's finding that the owner's route spreads requests over at least 3 upstream cache identities, but
the link is inferred, not measured. The model assumes one cache, so candidates are compared with
each other within the model, not with the measured run.

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

- **Codex at 0.25:** 8.5% to 13.3% cheaper, with 13.7% to 17.9% fewer input tokens.
- **Claude:** 3.2% to 4.1% cheaper at 0.25, and 4.1% to 5.4% at 0.10.

Accuracy risk: less recent history in view right after a rotation, so the model needs recall sooner.

| Run | Mean upstream messages, baseline | at 0.25 | at 0.10 |
|---|---|---|---|
| Codex | 13.9 | 8.3 / 8.9 | 8.3 |
| Claude | 41.0 | 40.2 | 36.1 |

The minimum turn room improved on Codex (12,934 to 14,334) and was unchanged on Claude (15,132).

### (c) Resume epoch state across proxy restarts

Modeled as one engine kept across restarts: a proxy that resumes the session's epoch, `k`, and
preamble instead of rotating from epoch 0.

- **Codex:** rotations fall from 19 to 9, all for the room. Cached share rises from 75.5% to between
  82.4% and 83.5%, and cost falls 9.8% to 16.2%. With (b) at 0.25, cost falls 15.1% to 19.9%.
- **Claude:** no effect, since run #3 had no restarts.

The view is what one long-lived proxy would give. A restart no longer re-pages or reshapes the
window.

Accuracy risk: the larger windows that are no longer rotated away lower the minimum turn room on
Codex from 12,934 to between 7,454 and 9,059, below the 12,000 `min_turn_room`. With (b) it is
9,515 to 11,456, still below it at the content rate.

### Reference: larger bounds (modeled)

- **Codex at 100,000** costs 65.7% to 79.0% more. It still rotates 11 to 14 times, on restarts,
  with larger windows.
- **Codex at 150,000** rotates 0 times at the head-inclusive rate and 5 times at the content rate,
  where the larger token counts reach the bound. These are still Contexto's request points,
  including the 23 tool rounds that Contexto's tools drive, so this is not plain Codex. It answers a
  different question from A27. The two figures side by side:
  - **A27:** Contexto costs about 24% more than plain Codex, modeled on the plain-run shape (probe
    turns as one request with no tool loop), which has different request points.
  - **A28:** at Contexto's own request points, bound 150,000 costs 40.0% to 104.9% more than
    Contexto at 64,000, so Contexto is 28.6% to 51.2% cheaper there, under this document's cache
    model.
  - **Why the token totals differ (checked).** At the head-inclusive rate, A28's 150,000 input is
    3,655,890 tokens, against A27's 4,789,302 for plain Codex at the same points (+31%). The
    no-rotation replay sends Codex's own bytes (within 15 bytes of each logged
    `original_request_bytes`), so the bytes agree. The difference is the conversion: 11,682,734
    bytes above the baseline at 4.2702 bytes per token, against 3.0193 for A27's rates. At the
    content rate the input is 3,190,976 tokens, because 5 rotations now page part of the history.
- **Claude at 100,000** is 25.8% to 37.3% cheaper than at 150,000. No request crosses Haiku's
  100,000 long-context tier (7 do at 150,000), but it rotates 19 times. At the head-inclusive rate
  11 of those are `window_minimal`, with a minimum turn room of 6,745. At the content rate all 19
  are for the room, with a minimum of 10,220. Both are below `min_turn_room`.

### What was not measured

Whether a probe's paged-only content would leave the view or the index under a candidate was not
evaluated. It needs plant positions in the stored pages, which this replay does not read. Rotations
at the same requests (a, c on Claude) keep the same pages. For (b) and the 100,000 reference, a live
check should score paged-only probes.

## Recommendation

1. **Build (c) and (b) together for small bounds; build (c) for every session.**
   - (c) + (b) is the cheapest Codex candidate at both rates: 15.1% to 19.9% below the baseline.
   - (c) alone saves 9.8% to 16.2%. It is also a correctness and durability fix: a restarted proxy
     sends what the previous one would have, instead of re-paging and reshaping the window.
   - (b) alone saves 8.5% to 13.3% on Codex and 3.2% to 5.4% on Claude, at the cost of fewer recent
     turns in view after a rotation.
   - Rule 16 (contract PR #176, merged) specifies (c). Its record holds every `EpochState` field
     that decides later bytes or rotations. The replay kept the whole engine, so per-request values
     (`last_input_tokens`, `last_sent_bytes`) also survived there. Rule 16 does not record them, so
     the first rotation decision after a resume sizes from a full estimate.
   - (c)'s minimum turn room (7,454 to 9,059 alone, 9,515 to 11,456 with (b)) is below
     `min_turn_room`. A live check should watch `turn_room_low`.
2. **Do not build (a).** It shows no gain under the measured cache behaviour and has a newest-pages
   risk.
3. **Bound choice.** On Haiku the 100,000 long-context tier dominates cost at 150,000. A default
   bound change needs a live accuracy check first, because the minimum turn room at 100,000 is 6,745
   to 10,220.

## What a live check would need

- **(c):** rerun the A22 Codex protocol, per-turn `codex exec` at bound 64,000, with rule 16's
  resume. Compare rotations and measured `cached_tokens` against A22, and score the same probes.
- **(b):** one Codex and one Claude acceptance run at `window_fraction` 0.25, scoring paged-only
  probes and recall calls against the baseline runs.
- **Claude bound:** one H1 run at bound 100,000 on Haiku, checking accuracy against run #3.
