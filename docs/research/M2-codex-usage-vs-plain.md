# M2 A27: Codex usage with Contexto against plain Codex (the A22 run)

This compares one Codex session through Contexto with what plain Codex would have used. It uses only
the A22 live run's own records: Codex acceptance step 1, main `ca05b29`, `gpt-6-astra` at low effort,
bound 64,000, 21 turns. Nothing was sent to a model to produce it.

It follows lane B's B25 method (`docs/research/M2-usage-savings-run3.md`). Where the Codex data differs,
the method diverges, and each divergence is stated. Every figure is labeled **measured** (from the
run's logs) or **modeled** (computed, with the method and error stated). Numbers only.

The per-request numbers are in [`data/M2-codex-usage-vs-plain.csv`](data/M2-codex-usage-vs-plain.csv).
Its columns are: request; user turn; held stream events (above 0 for a tool round); the bytes Codex
sent and the bytes Contexto sent; measured input, cached input and output tokens; and plain input at
the same point (**modeled**).
Reproduce them with `uv run --with tiktoken python spikes/m2/acceptance/codex_usage_vs_plain.py <run dir>
--o200k --csv <file>`.

## Headline

| | Input tokens | Requests | Peak request | Cost, USD (**modeled**) |
|---|---|---|---|---|
| Contexto (**measured**) | 919,998 | 44 | 26,066 | 3.78 |
| Plain Codex, plain-run shape (**modeled**) | 1,668,116 (1,661,084 to 1,701,565) | 21 | 115,860 | 3.04 (best-case cache) |
| Plain Codex, same request points (**modeled**) | 4,768,798 (4,710,802 to 4,873,509) | 44 | 147,207 | 6.58 (best-case cache) |

- **Tokens, against the plain-run shape** (the fair comparison): Contexto used **44.8% fewer input
  tokens** (44.6% to 45.9% over the band).
- **Cost, against the plain-run shape:** Contexto cost **24% more** (**modeled**: 3.78 against 3.04),
  even though it used fewer tokens.
  - The plain side assumes a best-case prefix cache.
  - With uncached input priced at the input rate instead of the cache-write rate, it is still 16% more
    (3.18 against 2.75).
- **Content turns: Contexto saves.** 284,788 tokens against 981,040 plain (**modeled**), or −71.0%. The
  cost is 2.00 USD against 2.29 (−12.5%). Per content request, plain is cheaper up to turn 5 and Contexto
  from turn 6 on.
- **Probe turns: Contexto costs more.** 635,210 tokens against 687,078 plain (**modeled**), or −7.5%. The
  cost is 1.78 USD against 0.75. Contexto's search loops made 29 requests where plain makes 6. Each
  probe turn's first request also rotates the window, which breaks the cache.
- **Compaction:** Codex's own compaction (about 244,800 tokens) would not have fired. The plain history
  peaks at 115,860 (plain-run shape) and 147,207 (same points), both **modeled**.

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

Every rotation in this run paged all completed turns. So at a request in turn *t*, the difference is
turns 1 to *t*−1, net of Contexto's preamble. As in B25, only that difference is converted; the part
both sides share stays measured:

`plain = measured input + (paged bytes − paged tool-loop bytes) / text rate + paged tool-loop bytes / JSON rate`

**Divergence 1: two rates.** B25 converts the whole difference at one text rate and notes that paged
tool JSON overstates plain. Here the paged bytes split exactly:
- the tool loops of earlier probe turns, which are the bytes Codex appended inside each probe turn;
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
| Plain, same points | 4,710,802 | 4,768,798 | 4,873,509 |
| Plain, plain-run shape | 1,661,084 | 1,668,116 | 1,701,565 |

The plain-run shape depends on the text rate only. The tool loops it removes and the paged loops it
adds convert at the same JSON rate, so they cancel.

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
  - One artifact: probe turn 2's input comes out 879 tokens below probe turn 1's (−0.8%), where a real
    history only grows. Contexto's view at request 22 is 2,714 bytes larger but 554 tokens smaller than
    at request 16, so some retained items tokenize unlike text; they were not read. Read the per-request
    figures as ±1%.

### (c) Compaction, both scenarios

- **Trigger:** `min(model_auto_compact_token_limit, 90% of the context window)`, about 244,800 tokens for
  gpt-6-astra's 272,000 window. Sources: `docs/research/M0-codex-measurements.md` section 5, and
  `CODEX_COMPACT_AT` in `spikes/m2/acceptance/codex_budget.py`.
- **Without compaction:** the plain history peaks at 115,860 (plain-run shape) and 147,207 (same
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
| Plain-run shape, USD (**modeled**) | 3.04 | 3.19 | 20.88 |
| Same request points, USD (**modeled**) | 6.58 | 7.01 | 59.72 |

The measured prefix-hit fraction is 0.9919: cached tokens over the previous request's input, on
Contexto's 23 append-only requests.

**Prices** (`docs/research/M0-cost-model.md`, gpt-6-astra row, vendor-documented 2026-10-15), USD per
million tokens:

| Input | Output | Cache read | Cache write |
|---|---|---|---|
| 10 | 50 | 1 | 12.5 |

- The M0 model's OpenAI rule is that writes cost 1.25 times input. Every uncached input token is
  counted as written (an assumption).
- With uncached input at the input rate instead: Contexto 3.18, plain-run shape 2.75, same points 6.21
  (best-case cache).
- Above 272,000 input tokens, input doubles and output rises 1.5 times. No request here reaches that.

## By cache class (plain-run shape, best-case cache)

| Class | Contexto (**measured**) | Plain (**modeled**) |
|---|---|---|
| Cached input | 680,832 | 1,551,378 |
| Uncached input | 239,166 | 116,738 |
| Output | 2,203 | 662 |

- **Uncached:** Contexto's is twice plain's.
  - 19 requests cached only the 9,088-token head (17 rotated requests) or nothing (requests 1 and 39).
    They account for 196,275 of Contexto's 239,166 uncached tokens (82%) (**measured**).
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
| 17 | probe 2 | 4 | 84,943 | 13,903 | 0.2602 | 485,194 | 112,881 | 0 | 0.1175 |
| 18 | probe 3 | 3 | 60,088 | 13,240 | 0.2313 | 381,331 | 114,277 | 1,396 | 0.1359 |
| 19 | probe 4 | 5 | 108,440 | 14,488 | 0.2893 | 664,388 | 114,954 | 677 | 0.1274 |
| 20 | probe 5 | 5 | 106,188 | 14,540 | 0.2890 | 688,496 | 115,346 | 392 | 0.1245 |
| 21 | probe 6 | 6 | 138,783 | 26,911 | 0.4717 | 865,632 | 115,860 | 514 | 0.1270 |

- Turns 1 and 2 are equal: Contexto passes requests through until its first rotation.
- After that, plain grows by about 6,800 tokens per content turn (**modeled**), while Contexto's content
  requests stay between 18,164 and 19,105 (**measured**).

## Findings for the product

- **Rotation every turn breaks the prompt cache after Codex's head.**
  - In this run every content request from turn 3 on rotated, so it re-sent its window uncached (about
    9,100 to 10,000 tokens) where plain pays only the new turn (about 6,800).
  - At OpenAI's cache pricing, that decides the cost.
  - Fewer rotations, or a preamble that does not change the prefix, is the lever.
- **Tool-loop rounds**, as B25 found. Contexto's probe turns cost 2.4 times the plain-run shape's,
  because every search round re-sends the window (29 requests against 6).

## Limits

- **Measured against modeled.**
  - Measured: everything about Contexto, and both calibration rates.
  - Modeled: every plain figure, the cache split for plain, every cost, and the compaction conclusion.
- **Counter error.**
  - The text rate rests on one pass-through pair, banded by o200k's per-turn range (−2.6% to +0.6% on
    the rate) and validated to 0.18% at one point.
  - The JSON rate's pairs spread widely (quartiles 3.41 to 3.95).
  - The totals' bands are −1.2% to +2.2% (same points) and −0.4% to +2.0% (plain-run shape). Per request,
    read ±1%.
- **o200k is not gpt-6-astra's documented tokenizer.** It is used only as a cross-check and for the band.
  On the one measured pair it agrees within 0.15%.
- **Compaction model.** Not needed here, because the trigger is never reached. Beyond about 19 more such
  turns it would be an assumption.
- **Cache assumption.** Plain gets the best case. The sensitivity table shows the range.
- **Price assumption.** Uncached input is priced at the cache-write rate. The input-rate variant is given
  above.
- **Single run, probe protocol.** One run of 21 turns: 15 synthetic 20 KB content turns, then 6 probe
  turns built to force retrieval. This is not a representative workload. Longer sessions grow plain's
  history further, and the content-turn saving grows with them.
- **Plain Codex's head.** It is taken as measured, which includes the Contexto MCP server's exposure.
- **What plain would do differently.** Plain Codex has no search tool. It would answer the probes from
  its full history, here about 115,000 tokens (**modeled**). Fidelity is a separate result.
