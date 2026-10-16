# M2 A27: Codex usage with Contexto against plain Codex (the A22 run)

This compares one Codex session through Contexto with what plain Codex would have used. It uses only
the A22 live run's own records: Codex acceptance step 1, main `ca05b29`, `gpt-6-astra` at low effort,
bound 64,000, 21 turns. Nothing was sent to a model to produce it.

It follows lane B's B25 method (`docs/research/M2-usage-savings-run3.md`). Where the Codex data differs,
the method diverges, and each divergence is stated. Every figure is labeled **measured** (from the
run's logs) or **modeled** (computed, with the method and error stated). Numbers only.

The per-request numbers are in [`data/M2-codex-usage-vs-plain.csv`](data/M2-codex-usage-vs-plain.csv).
Its columns are: request; user turn; held stream events (above 0 for a tool round); the user turns
paged when it was sent (from the rotation events); the bytes Codex
sent and the bytes Contexto sent; measured input, cached input and output tokens; and plain input at
the same point (**modeled**).
Reproduce them with `uv run --with tiktoken python spikes/m2/acceptance/codex_usage_vs_plain.py <run dir>
--o200k --csv <file>`.

## Headline

| | Input tokens | Requests | Peak request | Cost, USD (**modeled**) |
|---|---|---|---|---|
| Contexto (**measured**) | 919,998 | 44 | 26,066 | 3.78 |
| Plain Codex, plain-run shape (**modeled**) | 1,672,525 (1,667,862 to 1,705,855) | 21 | 116,731 | 3.05 (best-case cache) |
| Plain Codex, same request points (**modeled**) | 4,789,302 (4,742,328 to 4,893,461) | 44 | 148,079 | 6.60 (best-case cache) |

- **Tokens, against the plain-run shape** (the fair comparison): Contexto used **45.0% fewer input
  tokens** (44.8% to 46.1% over the band).
- **Cost, against the plain-run shape:** Contexto cost **24% more** (**modeled**: 3.78 against 3.05),
  even though it used fewer tokens.
  - The plain side assumes a best-case prefix cache.
  - With uncached input priced at the input rate instead of the cache-write rate, it is still 15% more
    (3.18 against 2.76).
  - This holds for this run's setup: a new proxy for every turn, which made each content request from
    turn 3 rotate, and bound 64,000 (see "Why every content request rotated" and Limits).
- **Content turns: Contexto saves.** 284,788 tokens against 981,040 plain (**modeled**), or −71.0%. The
  cost is 2.00 USD against 2.29 (−12.5%). Per content request, plain is cheaper up to turn 5 and Contexto
  from turn 6 on.
- **Probe turns: Contexto costs more.** 635,210 tokens against 691,485 plain (**modeled**), or −8.1%. The
  cost is 1.78 USD against 0.76. Contexto's search loops made 29 requests where plain makes 6. Each
  probe turn's first request also rotates the window, which breaks the cache.
- **Compaction:** Codex's own compaction (about 244,800 tokens) would not have fired. The plain history
  peaks at 116,731 (plain-run shape) and 148,079 (same points), both **modeled**.

## Method

### (a) Contexto, measured

Each of the 44 Responses requests' logged `usage` gives input tokens, of which `cached_tokens` were
cached, and output tokens. The Responses API reports no cache-write class, so input splits into cached
and uncached only.

| Class | Tokens |
|---|---|
| Cached input | 680,832 |
| Uncached input | 239,166 |
| Output | 2,203 |
| **Input total** | **919,998** |

### (b) Plain Codex, modeled

The proxy logs two sizes for every request:
- `original_request_bytes`: what Codex sent, its full history, its head and the current turn;
- `request_bytes`: what Contexto sent upstream.

The paged turns at each request come from the logged rotation events, carried forward to requests
that do not rotate. The engine counts Codex's opening context as its turn 0, so `turns_paged` = *p*
pages user turns 1 to *p*−1.
- In content turns 3 to 15, each rotation paged every completed turn.
- In probe turns it did not: the previous turn stayed in the window with its tool loop. From probe
  turn 1 on, `retained_messages` was 3, then 14, 10, 9, 12 and 12.

The difference is the paged turns, net of Contexto's preamble. As in B25, only that difference is
converted; the part both sides share, including a retained turn's tool loop, stays measured:

`plain = measured input + (paged bytes − paged tool-loop bytes) / text rate + paged tool-loop bytes / JSON rate`

**Divergence 1: two rates.** B25 converts the whole difference at one text rate and notes that paged
tool JSON overstates plain. Here the paged bytes split exactly:
- the tool loops of the paged probe turns, which are the bytes Codex appended inside each probe turn;
- everything else, which is content-turn text, probe questions and answers, and the preamble, net.

**Text rate.** 2.9805 bytes per token (**measured**). It is the one added turn between the two epoch-0
requests, which Contexto passed through unchanged: 20,336 bytes over 6,823 tokens.
- One pair gives no spread, so the band uses the o200k_base tokenizer on the script's own generated
  content turns: 2.9042 to 2.9971 bytes per token, from turn to turn.
- This is plan text from the seed, not run content.
- o200k counts turn 2 at 6,812 tokens plus 1 for the reply. The measured delta is 6,823, so o200k is
  within 10 tokens (0.15%), the items' wrappers.

**JSON rate.** 3.5329 bytes per token (**measured**: pooled, all bytes over all tokens). It comes from
the 23 append-only requests inside probe turns, each with one tool call and its output appended, no
rotation, and the same growth on both sides.
- Quartiles of the 23 pairs: 3.4097, 3.7633, 3.9526.
- Range: 3.109 to 8.895. The one outlier is request 28; its items were not read.
- Residuals of the pooled fit, per pair: mean 0, mean absolute 165 tokens, range −534 to +609.
- The band uses the first and third quartiles.

**Error band.** Both rates are moved to their band ends together:

| | Low | Central | High |
|---|---|---|---|
| Plain, same points | 4,742,328 | 4,789,302 | 4,893,461 |
| Plain, plain-run shape | 1,667,862 | 1,672,525 | 1,705,855 |

The plain-run shape depends mainly on the text rate. Removing a paged tool loop cancels the JSON rate
it was added at. Only the retained previous loop is removed at the JSON rate while it entered as
measured tokens. With the text rate held, the JSON band alone moves the plain-run shape by −0.05% to
+0.15%.

**Validation** (divergence 2: B25 used Anthropic `count_tokens` from its baseline arm; this run has no
baseline arm and no zero-spend OpenAI counter). At probe turn 1's first request (request 16):
- modeled plain is 113,760;
- an independent count gives 113,551, so the model is 0.18% high.
- The independent count is request 1's measured input, plus o200k counts of turns 2 to 16, plus the
  11-token per-turn overhead measured on the pass-through pair.

**Codex's head.** The head (instructions, tools) is in the measured tokens on both sides.
- Request 1 measured 17,843 tokens for the head plus turn 1. o200k counts turn 1 at 6,911, which puts
  the head near 10,900 (**modeled**).
- 17 of the 19 rotated requests had exactly 9,088 tokens cached (**measured**), the prefix that
  survives a rotation. Request 16 had 18,944 and request 39 none.
- The head includes the Contexto MCP server's exposure in Codex. Plain Codex without Contexto would not
  have it. The logs cannot separate it, so plain is overstated by that part.

**Plain output.** At the same points, Contexto's 44 **measured** outputs. In the plain-run shape, each
turn's final answer as Contexto **measured** it: 662 tokens.

**Two plain shapes:**
- **Same request points** (44 requests). Plain Codex's history wherever Contexto sent a request. This
  overstates plain: without Contexto there are no search loops, and the history here carries their
  tool results.
- **Plain-run shape** (21 requests, the fair comparison).
  - Content turns are at the same points.
  - Each probe turn is one request with no tool loop: the same-points plain at the turn's first
    request, less every earlier probe turn's tool loop.
  - **Divergence 3:** B25 measured these 6 requests in its baseline arm. Here they are modeled, because
    step 2 was not run.

### (c) Compaction, both scenarios

- **Trigger:** `min(model_auto_compact_token_limit, 90% of the context window)`, about 244,800 tokens for
  gpt-6-astra's 272,000 window. Sources: `docs/research/M0-codex-measurements.md` section 5, and
  `CODEX_COMPACT_AT` in `spikes/m2/acceptance/codex_budget.py`.
- **Without compaction:** the plain history peaks at 116,731 (plain-run shape) and 148,079 (same
  points), both **modeled**.
- **With compaction:** both peaks are under the trigger, so it never fires. The two scenarios are
  identical here, with no sawtooth, and no post-compaction size is needed.
- **Distance:** at about 6,820 tokens per content turn (**modeled**), the plain-run shape would need about
  19 more such turns to reach the trigger. A post-compaction size would then be an assumption, because
  Codex's compaction has not been measured on this setup.

### (d) Cache for plain, and cost

**Cache assumption** (divergence 4: Responses usage has no write class). Best case: each plain request
reads the previous request's whole prompt from cache, and the rest is uncached. Sensitivity:

| Plain scenario | Best case | At the run's measured prefix-hit fraction | No cache |
|---|---|---|---|
| Plain-run shape, USD (**modeled**) | 3.05 | 3.19 | 20.94 |
| Same request points, USD (**modeled**) | 6.60 | 7.04 | 59.98 |

The measured prefix-hit fraction is 0.9919: cached tokens over the previous request's input, on
Contexto's 23 append-only requests.

**Prices** (`docs/research/M0-cost-model.md`, gpt-6-astra row, vendor-documented 2026-10-15), USD per
million tokens:

| Input | Output | Cache read | Cache write |
|---|---|---|---|
| 10 | 50 | 1 | 12.5 |

- The M0 model's OpenAI rule is that writes cost 1.25 times input. Every uncached input token is
  counted as written (an assumption).
- With uncached input at the input rate instead: Contexto 3.18, plain-run shape 2.76, same points 6.23
  (best-case cache).
- Above 272,000 input tokens, input doubles and output rises 1.5 times. No request here reaches that.

## By cache class (plain-run shape, best-case cache)

| Class | Contexto (**measured**) | Plain (**modeled**) |
|---|---|---|
| Cached input | 680,832 | 1,555,793 |
| Uncached input | 239,166 | 116,731 |
| Output | 2,203 | 662 |

- **Uncached:** Contexto's is twice plain's.
  - 19 requests cached only the 9,088-token head (17 rotated requests) or nothing (requests 1 and 39).
    They account for 196,275 of Contexto's 239,166 uncached tokens (82%) (**measured**). The 19
    rotated requests (request 16 among them, which cached 18,944) hold 178,923 (74.8%).
  - A rotation changes the preamble right after Codex's head, so everything after the head is re-sent
    uncached.
  - Plain pays uncached only for each new turn, about 6,800 tokens (**modeled**).
- **Cached:** plain reads 2.3 times more, re-reading its growing history.
- At gpt-6-astra's prices, an uncached token costs 12.5 times a cached one. That outweighs the smaller
  reads in this 21-turn run.

## Per-turn curve (tokens per turn; USD modeled)

| Turn | Kind | Contexto requests | Contexto input (**measured**) | Contexto uncached (**measured**) | Contexto USD | Plain same points (**modeled**) | Plain-run shape (**modeled**) | Plain-run shape uncached | Plain-run shape USD |
|---|---|---|---|---|---|---|---|---|---|
| 1 | content | 1 | 17,843 | 17,843 | 0.2233 | 17,843 | 17,843 | 17,843 | 0.2233 |
| 2 | content | 1 | 24,666 | 7,002 | 0.1054 | 24,666 | 24,666 | 6,823 | 0.1034 |
| 3 | content | 1 | 18,164 | 9,076 | 0.1228 | 31,245 | 31,245 | 6,579 | 0.1071 |
| 4 | content | 1 | 18,236 | 9,148 | 0.1237 | 38,052 | 38,052 | 6,807 | 0.1166 |
| 5 | content | 1 | 18,317 | 9,229 | 0.1247 | 44,854 | 44,854 | 6,802 | 0.1233 |
| 6 | content | 1 | 18,399 | 9,311 | 0.1257 | 51,658 | 51,658 | 6,803 | 0.1301 |
| 7 | content | 1 | 18,473 | 9,385 | 0.1267 | 58,472 | 58,472 | 6,814 | 0.1371 |
| 8 | content | 1 | 18,561 | 9,473 | 0.1278 | 65,285 | 65,285 | 6,812 | 0.1439 |
| 9 | content | 1 | 18,640 | 9,552 | 0.1287 | 72,100 | 72,100 | 6,815 | 0.1507 |
| 10 | content | 1 | 18,722 | 9,634 | 0.1298 | 78,928 | 78,928 | 6,828 | 0.1577 |
| 11 | content | 1 | 18,790 | 9,702 | 0.1306 | 85,728 | 85,728 | 6,800 | 0.1642 |
| 12 | content | 1 | 18,894 | 9,806 | 0.1319 | 92,679 | 92,679 | 6,950 | 0.1729 |
| 13 | content | 1 | 18,954 | 9,866 | 0.1327 | 99,588 | 99,588 | 6,909 | 0.1793 |
| 14 | content | 1 | 19,024 | 9,936 | 0.1335 | 106,512 | 106,512 | 6,925 | 0.1864 |
| 15 | content | 1 | 19,105 | 10,017 | 0.1346 | 113,430 | 113,430 | 6,917 | 0.1932 |
| 16 | probe 1 | 6 | 136,768 | 7,104 | 0.2373 | 702,717 | 113,760 | 330 | 0.1221 |
| 17 | probe 2 | 4 | 84,943 | 13,903 | 0.2602 | 489,689 | 114,005 | 245 | 0.1215 |
| 18 | probe 3 | 3 | 60,088 | 13,240 | 0.2313 | 383,262 | 114,920 | 916 | 0.1311 |
| 19 | probe 4 | 5 | 108,440 | 14,488 | 0.2893 | 669,270 | 115,930 | 1,010 | 0.1322 |
| 20 | probe 5 | 5 | 106,188 | 14,540 | 0.2890 | 692,461 | 116,139 | 209 | 0.1232 |
| 21 | probe 6 | 6 | 138,783 | 26,911 | 0.4717 | 870,863 | 116,731 | 593 | 0.1288 |

- Turns 1 and 2 are equal: Contexto passes requests through until its first rotation.
- After that, plain grows by about 6,800 tokens per content turn (**modeled**), while Contexto's content
  requests stay between 18,164 and 19,105 (**measured**).

## Findings for the product

- **Rotation every turn breaks the prompt cache after Codex's head.**
  - In this run every content request from turn 3 on rotated, so it re-sent its window uncached (about
    9,100 to 10,000 tokens) where plain pays only the new turn (about 6,800).
  - At OpenAI's cache pricing, that decides the cost.
  - The rotations came mainly from a new proxy for every turn (next section). Resuming epoch state
    across restarts is the main lever; a preamble that does not change the prefix is another.
- **Tool-loop rounds**, as B25 found. Contexto's probe turns cost 2.3 times the plain-run shape's,
  because every search round re-sends the window (29 requests against 6).

## Why every content request rotated (A29)

The main cause was a new Contexto proxy for every `codex exec` turn, not bound 64,000 alone.

- **A new proxy per turn** (**measured**). The session log has 21 `proxy_start` events for 21 turns and
  44 requests.
- **A new engine kept only the sizing ratio** (**measured**).
  - Its `sizing_restored` event, on 20 requests, carries `bytes_per_token` and `weight_tokens` only.
  - It had no epoch state, so each engine started again at epoch 0 and rotated on its first request.
  - Every request's logged epoch is 0 (requests 1 and 2, the pass-through) or 1.
  - All 19 rotations are on the first request after a restart: requests 3 to 16, 22, 26, 29, 34 and
    39. Their `k` runs 10, 12, ..., 34, 34, 36, 49, 58, 66, 77.
- **Reproduced offline.** A28's baseline replay (`docs/research/M2-cache-aware-rotation.md`, PR #174)
  reproduces every logged rotation: 19, at the same requests, with bytes within 597 of the log.
- **Bound 64,000 is a secondary factor.** In A28's model (**modeled**), bound 100,000 still rotates 11
  times on restarts and costs 65.7% more. Bound 150,000 never rotates, which is plain Codex at the same
  request points, and costs 40.0% more than Contexto at 64,000.
- **Resuming epoch state across restarts** (A28, **modeled**: one engine kept across restarts):
  - rotations fall from 19 to 9;
  - the cached share rises from 75.5% to 83.5% (A28's cache model, whose baseline gives 75.5% against
    the measured 74.0%);
  - cost falls 16.2%.

## Limits

- **Measured against modeled.**
  - Measured: everything about Contexto, and both calibration rates.
  - Modeled: every plain figure, the cache split for plain, every cost, and the compaction conclusion.
- **Counter error.**
  - The text rate rests on one pass-through pair, banded by o200k's per-turn range (−2.6% to +0.6% on
    the rate) and validated to 0.18% at one point.
  - The JSON rate's pairs spread widely (quartiles 3.41 to 3.95).
  - The totals' bands are −1.0% to +2.2% (same points) and −0.3% to +2.0% (plain-run shape).
- **o200k is not gpt-6-astra's documented tokenizer.** It is used only as a cross-check and for the band.
  On the one measured pair it agrees within 0.15%.
- **Compaction model.** Not needed here, because the trigger is never reached. Beyond about 19 more such
  turns it would be an assumption.
- **Cache assumption.** Plain gets the best case. The sensitivity table shows the range.
- **The cost result is specific to this configuration.**
  - Main cause: a new proxy for every turn, whose engine had no epoch state. Every content request from
    turn 3 on rotated, and each cached only the 9,088-token head.
  - Secondary factor: bound 64,000 with 20 KB turns.
  - With epoch state resumed across restarts, A28 models 9 rotations instead of 19 and 16.2% lower cost.
    A larger bound alone does not remove the restart rotations: at 100,000, A28 models 11 of them and a
    higher cost. See "Why every content request rotated" above.
- **Price assumption.** Uncached input is priced at the cache-write rate. The input-rate variant is given
  above.
- **Single run, probe protocol.** One run of 21 turns: 15 synthetic 20 KB content turns, then 6 probe
  turns built to force retrieval. This is not a representative workload. Longer sessions grow plain's
  history further, and the content-turn saving grows with them.
- **Plain Codex's head.** It is taken as measured, which includes the Contexto MCP server's exposure.
- **What plain would do differently.** Plain Codex has no search tool. It would answer the probes from
  its full history, here about 115,000 tokens (**modeled**). Fidelity is a separate result.
