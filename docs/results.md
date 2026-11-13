# Results

[Contexto](../README.md) / [Documentation](README.md)

What the acceptance runs measured, and what each number does and does not show. Every row links the
record it comes from.

## Recall against full context

Each acceptance run is a scripted session whose history grows well past the bound. Its
probe turns ask 54 questions about details spread through that history. The same questions then go
to the same model with the whole history in its context (the baseline).

| Run | Result | Record |
|---|---|---|
| Claude Code 2.1.318, claude-haiku-5-5, bound 150,000 | 52 of 54 answered correctly, against 47 for the baseline. No compaction. Largest request 110,465 tokens. Cache reused on 38 of 38 eligible requests. Claude Code's defaults, tool search on, so Contexto's tools load through it. | [M2-acceptance-run4.md](research/M2-acceptance-run4.md) |
| Claude Code 2.1.301, claude-haiku-5-5, bound 150,000 | 51 of 54 answered correctly, against 48 for the baseline. No compaction. Largest request 109,868 tokens. Prompt cache reused on 36 of 37 eligible requests. | [M2-acceptance-run3.md](research/M2-acceptance-run3.md) |
| Claude Code 2.1.302, claude-opus-5-5, bound 150,000 | 53 of 54, against 54 for the baseline. No compaction. Cache reused on 54 of 55 eligible requests. The run added the permission rule that `contexto run` now sets itself. | [M2-acceptance-opus.md](research/M2-acceptance-opus.md) |
| Codex 0.163.0, gpt-6-astra, bound 150,000 | History of about 233,000 tokens (about 1.56 times the bound; 375,922 as the test harness prices them) with no compaction. Largest request 108,565 tokens. All 61 requests succeeded. No baseline was run. | [M2-codex-acceptance-150k.md](research/M2-codex-acceptance-150k.md) |

Read these scores as no worse than full context on a scripted recall test, not as better.

- **One script, one session per run.** Each run is one synthetic session on one model. The two
  claude-haiku-5-5 runs use the same seed and the same 54 questions.
- **The gap is inside noise.** In the 2.1.318 run Contexto missed 2 questions and the baseline 7,
  with none in common ([the misses](research/M2-acceptance-run4.md#misses-ids-only)). An exact
  McNemar test on those 9 questions gives p = 2 × (1 + 9 + 36) / 512 = 0.18. On claude-opus-5-5 the
  baseline was ahead, 54 against 53. The acceptance rule asks for no more than this: within 2 of the
  baseline overall, and no category more than 1 below it.
- **The baseline is a replay, not a Claude Code session.** It sends the whole history once per
  probe turn, about 426,000 tokens a request in the 2.1.318 run (426,266 to 429,087). That is under
  the roughly 967,000 tokens at which Claude Code compacts a 1M-window model by default, so plain
  Claude Code would not have compacted this session either.
- **What the runs do establish** for their one session: zero compaction, every request under the
  bound (largest 110,465 tokens in the 2.1.318 run), and the prompt cache read on 38 of 38 eligible
  requests.

## Tokens and cost

In the claude-haiku-5-5 run, Contexto used 46.6% fewer input tokens than plain Claude Code would have
for the same session, and cost 49.7% less. The plain Claude Code figures are modeled from measured
requests, not run end to end ([M2-usage-savings-run3.md](research/M2-usage-savings-run3.md)).

Fewer input tokens are sent. In run 3 Contexto sent 3,725,391 input tokens (measured) against
6,972,335 for plain Claude Code (18 content turns modeled, 6 probe turns measured), which is the
46.6%.

The cost saving does not carry over to other models. The 49.7% (about $0.29 against $0.58) exists
because Haiku 5.5 charges 5 times the rate on a prompt over 100,000 tokens. The full-history
requests cross that line and most of Contexto's do not (7 of 44 did). Priced at flat rates, the same
token counts cost more through Contexto:

| Rates | Contexto | Plain Claude Code | Contexto against plain |
|---|---|---|---|
| Haiku 5.5 with its 5 times step over 100,000 tokens | $0.29 | $0.58 | 49.7% less |
| Haiku 5.5 base rates, no step | $0.169 | $0.127 | 34% more |
| Opus 5.5 ($4 input, $20 output, $0.20 cache read, $5 cache write; no step) | $6.23 | $3.76 | 66% more |

Each rotation rewrites the prompt cache, so Contexto wrote 1,047,954 cache tokens against 438,358,
and a cache write costs 25 times a cache read on Opus 5.5. The two flat rows are run 3's counts
repriced, not a measured Opus session, and they give plain Claude Code a cache hit on every request.
With the two cache misses the baseline arm measured, plain would cost more than Contexto in both rows. The
arithmetic is in
[The same token counts at other prices](research/M2-usage-savings-run3.md#the-same-token-counts-at-other-prices-added-2026-11-12-modeled);
the prices are in [M0-cost-model.md](research/M0-cost-model.md#prices-and-cache-rules).

The one Codex session that was compared sent 45.0% fewer input tokens and cost 24% more (modeled),
mainly because that test harness started a new proxy for every turn and each restart rotated
([M2-codex-usage-vs-plain.md](research/M2-codex-usage-vs-plain.md)).
