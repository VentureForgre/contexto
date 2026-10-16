# M2 usage savings: live run #3 against plain Claude Code (2026-10-16)

This answers the owner's question for one run: how much does Contexto save against plain Claude Code?
It uses only live run #3's own records (main `fbb1303`, session `acc20261016T095818Z`,
`claude-haiku-5-5`, bound 150,000). Nothing was sent to a model to produce it. It gives numbers
only. Every figure is labeled **measured** (from the run's logs) or **modeled** (computed, with the
method and error stated). Per-request numbers are in
[`data/M2-usage-savings-run3.csv`](data/M2-usage-savings-run3.csv).

## Headline

| | Input tokens | Cost at Haiku 5.5 prices (USD) | Requests |
|---|---|---|---|
| Contexto, run #3 (**measured**) | 3,725,391 | 0.29 | 44 |
| Plain Claude Code, same request points (**modeled**) | 16,500,083 (16,433,805 to 16,567,050) | 1.11 (modeled) | 44 |
| Plain Claude Code, plain-run shape (**modeled**: 18 content turns modeled, 6 probe turns measured; best-case cache) | 6,972,335 | 0.58 (modeled) | 24 |

- **Against the plain-run shape** (the fair comparison; plain **modeled**: 18 content turns modeled,
  6 probe turns measured): Contexto used **46.6% fewer input tokens** and cost **49.7% less**, even
  with every plain request assumed to hit the prefix cache.
- **Against the same 44 request points** (plain **modeled**): 77.4% fewer tokens and 73.8% less cost.
- **Content turns:** the saving is mostly here. Contexto used 1,347,282 tokens against 4,350,348
  plain (**modeled**) (−69.0%).
- **Probe turns:** nearly even. Contexto used 2,378,109 tokens over 26 requests (its tool loops)
  against 2,621,987 over 6 plain requests (**measured**: the baseline arm's `count_tokens`) (−9.3%).

## Method

### (a) Contexto, measured

Each of the 44 H1 Messages requests' logged `usage` gives uncached input, cache read, cache write and
output. Totals:

| Class | Tokens |
|---|---|
| Uncached input | 88 |
| Cache read | 2,677,349 |
| Cache write | 1,047,954 |
| Output | 22,693 |
| **Input total** | **3,725,391** |

The peak request was 109,868 input tokens. 7 of the 44 requests were over 100,000.

### (b) Plain Claude Code, modeled

The proxy logged two sizes for every request:
- `original_request_bytes`: what Claude Code sent, its full local history at that point;
- `request_bytes`: what Contexto sent upstream.

The difference is the history Contexto paged out, net of its preamble, and it is content-turn text. So:

`plain tokens = Contexto's measured input tokens + (original_request_bytes - request_bytes) / 1.8274`

Only the paged-out difference is converted; the part both runs share comes from measured tokens.

- **Calibration.** The rate 1.8274 bytes per token is fitted on the same run, from the 3 epoch-0
  requests where Contexto passed Claude Code's request through unchanged. Each added content turn's
  bytes over its measured tokens gave 1.8179 and 1.8369; the mean is 1.8274.
- **Error band.** Using the two ends of that range moves the 44-request plain total from 16,433,805
  to 16,567,050 (±0.4%).
- **Validation, against a measured count.** At probe turn 1's first request, the modeled plain input
  is 434,154. The baseline arm's measured `count_tokens` for the same history is 435,590 (its request
  carries no Contexto tools and no thinking blocks), so the model is 0.33% low. (A single whole-body bytes-per-token fit was
  rejected: 8.6% mean error, because log text and tool JSON tokenize differently.)

**Two plain shapes:**
- **Same request points** (44 requests). Plain Claude Code's history at each point where Contexto sent
  a request. This overstates plain: without Contexto there would be no search tool loops, and Claude
  Code's own history here carries their tool results.
- **Plain-run shape** (24 requests). The 18 content turns modeled as above. The 6 probe turns are the
  baseline arm's **measured** `count_tokens`: one full-context request per probe turn, with no tool
  loop and the plain run's own earlier answers. This is the fair comparison.

**Cache assumption for plain**, stated: every plain request reads the previous request's whole prompt
from cache, writes the rest, and leaves 2 tokens uncached. This is the best case for plain, so it
understates the saving. Run #3 itself hit the cache on 36 of 37 eligible requests. The baseline arm
measured 2 full misses in 6 requests. With those measured misses, the plain shape (18 content turns
modeled, 6 probe turns measured) costs 1.08 USD instead of 0.58 (both modeled), and the saving rises
to 73.0%.

**Cost.** Prices are the M0 cost model's Haiku 5.5 rates
([M0-cost-model.md](M0-cost-model.md), vendor-documented 2026-10-15), in USD per million tokens:

| Input | Output | Cache read | Cache write (5 minute TTL) |
|---|---|---|---|
| 0.1 | 0.5 | 0.01 | 0.125 |

Every rate is ×5 when a prompt exceeds 100,000 tokens ([M0-cost-model.md:289](M0-cost-model.md),
the Haiku 5.5 price row, and [:305](M0-cost-model.md), the long-prompt rule). Pricing is **modeled**;
the token counts are the measured or modeled figures above.

### (c) Native compaction, modeled

Claude Code compacts at about 967K tokens by default on 1M-window models
([M0-cost-model.md](M0-cost-model.md), vendor-documented; [M0-recommendation.md](M0-recommendation.md)).
The plain history peaks at 493,902 tokens at the same request points (**modeled**) and at 438,406 in
the plain-run shape (**measured**: the baseline arm's last `count_tokens`). Both are far under that threshold.

**So native compaction would not have fired anywhere in this run.** The "plain with compaction" model
equals the "plain without compaction" model here: no sawtooth. The plain run simply grows to about
440k to 490k tokens per request.

## By cache class (plain-run shape: 18 content turns modeled, 6 probe turns measured; best-case cache)

| Class | Contexto (measured) | Plain (modeled) |
|---|---|---|
| Uncached input | 88 | 48 |
| Cache read | 2,677,349 | 6,533,929 |
| Cache write | 1,047,954 | 438,358 |
| Output | 22,693 | 12,836 |

- **Cache writes:** Contexto writes more, because each of its 6 rotations rewrites the window's
  prefix.
- **Cache reads:** plain reads far more, because it re-reads an ever-growing history.
- **Output:** Contexto's is higher, from its tool-loop requests.

## Per-turn curve (input tokens; plain at the same request points)

| Turn | Kind | Contexto requests | Contexto input (measured) | Plain input (modeled) | Contexto USD (modeled) | Plain USD (modeled) |
|---|---|---|---|---|---|---|
| 1 | content | 1 | 50,906 | 50,906 | 0.0065 | 0.0065 |
| 2 | content | 1 | 73,769 | 73,769 | 0.0034 | 0.0034 |
| 3 | content | 1 | 96,485 | 96,485 | 0.0036 | 0.0036 |
| 4 | content | 1 | 51,450 | 117,925 | 0.0065 | 0.0185 |
| 5 | content | 1 | 74,268 | 140,743 | 0.0034 | 0.0202 |
| 6 | content | 1 | 96,926 | 163,401 | 0.0036 | 0.0212 |
| 7 | content | 1 | 51,901 | 184,629 | 0.0037 | 0.0218 |
| 8 | content | 1 | 74,555 | 207,283 | 0.0094 | 0.0238 |
| 9 | content | 1 | 97,359 | 230,087 | 0.0036 | 0.0246 |
| 10 | content | 1 | 52,379 | 252,142 | 0.0038 | 0.0259 |
| 11 | content | 1 | 75,168 | 274,931 | 0.0034 | 0.0269 |
| 12 | content | 1 | 97,646 | 297,409 | 0.0094 | 0.0278 |
| 13 | content | 1 | 52,832 | 319,838 | 0.0038 | 0.0292 |
| 14 | content | 1 | 75,605 | 342,611 | 0.0034 | 0.0303 |
| 15 | content | 1 | 98,331 | 365,337 | 0.0036 | 0.0313 |
| 16 | content | 1 | 53,226 | 388,276 | 0.0038 | 0.0326 |
| 17 | content | 1 | 75,893 | 410,943 | 0.0038 | 0.0337 |
| 18 | content | 1 | 98,583 | 433,633 | 0.0036 | 0.0348 |
| 19 | probe 1 | 5 | 514,430 | 2,189,680 | 0.0747 | 0.1213 |
| 20 | probe 2 | 3 | 323,296 | 1,328,446 | 0.0797 | 0.0784 |
| 21 | probe 3 | 2 | 138,092 | 926,006 | 0.0088 | 0.0680 |
| 22 | probe 4 | 5 | 394,138 | 2,363,923 | 0.0182 | 0.1300 |
| 23 | probe 5 | 6 | 521,345 | 2,885,087 | 0.0180 | 0.1649 |
| 24 | probe 6 | 5 | 486,808 | 2,456,593 | 0.0098 | 0.1352 |

Turns 1 to 3 are equal: Contexto passes the request through until its first rotation. After that the
plain input grows by about 22,700 tokens per turn, while Contexto's content requests stay between
50,906 and 98,583.

## Two findings for the product

- **Haiku 5.5's 100K price line** ([M0-cost-model.md:289](M0-cost-model.md) and
  [:305](M0-cost-model.md)). 7 Contexto requests crossed 100,000 tokens, where every rate is ×5: the tool-loop requests of probe turns 1 and 2 (101,390 to 109,868). In probe
  turn 2 that made Contexto's cost slightly higher than plain's at the same points. A bound or room
  that kept requests under 100,000 on Haiku 5.5 is a cost lever worth a look.
- **Tool-loop rounds.** Contexto's probe turns cost about as much as the plain probe requests
  because every search round re-sends the window (26 requests against 6). Fewer, larger search
  rounds is the lever (intake's note on the tool_rounds calibration).

## Reference: the baseline arm

The baseline arm was 6 direct full-context requests, one per probe turn, measured:
- `count_tokens` per request: 435,590 / 436,155 / 436,710 / 437,280 / 437,846 / 438,406;
- spend: 2,634,365 tokens.

It is the plain-run shape's probe phase, used above. It is not a Contexto figure.

## Limits

- **Measured vs modeled.** Measured: everything about Contexto, and the 6 plain probe-turn inputs
  (`count_tokens`). Modeled: the 18 plain content-turn inputs, the plain cache split, every cost
  figure, and the compaction conclusion.
- **Counter error.** The paged-text rate comes from 2 requests (1.818 to 1.837 bytes per token). It is
  validated to within 0.33% at one point (probe turn 1). The band on the totals is ±0.4%.
- **Cache assumption.** Plain gets the best case, a full prefix hit on every request. Real routes
  miss sometimes: the baseline arm missed on 2 of 6, which raises plain's cost about 1.9×.
- **Synthetic session.** Run #3 pastes about 40 KB of log per turn, larger than typical Claude Code
  turns. The plain history grows faster than usual, and the saving per turn may be smaller in real
  sessions.
- **Single run, single model.** One run on Haiku 5.5, whose ×5 price above 100K dominates the cost
  comparison. Other models do not have that price step.
- **What plain would do differently.** Without Contexto, the model would have no search tool and
  would answer probes from the full history (the baseline arm scored 48/54, against Contexto's 51/54
  in run #3). Cost and fidelity are separate results.
