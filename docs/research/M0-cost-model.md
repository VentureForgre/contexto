# M0 cost model: long-session token cost under four context strategies

Status: lane 2 packet L2a, answers brief question 3 (`docs/milestones/M0-infinite-session-study.md`).
Date: 2026-10-15. Author: build.builder-3.

Every number in this document comes from `spikes/m0/cost_model/cost_model.py`. The tables between
`BEGIN GENERATED` and `END GENERATED` markers are written by the script, and a test fails if they
drift from its output. No live model calls were made for this packet, so live token spend is zero.

Claim tags used below:

- **vendor-documented**: read on a vendor page; the URL and retrieval date are in `prices.json`.
- **measured**: observed by lane 1's measuring proxy. Nothing in this document is measured yet (see
  "Lane 1 data").
- **inferred**: an assumption or a consequence of the model. Every session-profile number is inferred.
- **modeled**: an output of the script on inferred inputs. Every cost table below is modeled.

## Summary

<!-- BEGIN GENERATED: findings -->
- Under the owner's bar (no compaction, a hard working-context bound), only (iii) meets a 100k or 250k bound without a lossy reset: (i) and (ii) at the CLI default exceed it, (ii) configured to reset inside the bound compacts, H4 hands off lossily, and tool-result clearing still grows past it. No strategy meets 30k with a 42.0k head. Sized to 100k, (iii) costs $70.13 over 300 turns with a 100.0k maximum working context.
- Cost, second: over 300 turns on Claude Opus 5.5 with API-key billing (5 min TTL), (iii) costs $85.02 against $358.76 for (ii) built-in compaction, $342.94 for H4 rollover, and $614.27 for (i) plain, which would pass the 1.00M window at turn 184. Across the five models (iii) costs 0.07 to 0.76 of (ii) (Claude Haiku 5.5 to gpt-6-astra).
- On the inferred think-time mix (35% of gaps over 5 min), cache expiry is the largest single cost in (i): 64% of its cost is the premium (write price minus read price) for re-writing prefixes after idle gaps over the 5 minute TTL. The 1 h TTL cuts (i) to $290.41 and (ii) to $182.71; (iii) gains less ($68.56) because its prefix is small. Keepalive pings bring (iii) to $62.07.
- Bounding the prompt is most of the cost saving: compaction configured at 100k costs $95.88, within 13% of (iii), but only by compacting 52 times, which the owner's bar rules out.
- (iii) costs more than (ii) for very short sessions: it first beats (ii) at turn 14. Its fixed overhead is the pinned block, stubs, and recall round trips.
- Under the profile's think time, rebases mostly ride on cache expiry, so epoch length barely matters and window size sets the cost. With no idle gaps the analytic rule E* = sqrt(2 (w - r) R / (r n g)) lands within 32% of the simulated optimum on the tested grid.
<!-- END GENERATED: findings -->

How strategies are judged (owner principle, relayed by manager-2 on 2026-10-15): compaction is a
failure mode, and the success bar is no compaction ever with accuracy equal to the full-context
baseline. A maximum working context is a hard constraint. Strategies rank first by whether they stay
inside that bound without a lossy reset, then by working-context size, and only then by token cost.
Quality is not scored by this model; packet L2b's fidelity test and the quality-vs-size probe (L2d)
supply that, and the bound values used here (30k, 100k, 250k) are placeholders until L2d measures the
real one.

What this means for the study, inferred from the tables:

1. Only (iii) meets a working-context bound without compacting or handing off. Every built-in
   compaction is a failure under the owner's bar, so (ii)'s cost column is a reference, not an
   option.
2. The hypothesis is also cheaper, because its prompt is bounded, not because of anything clever in
   the cache. The cache-friendly layout keeps (iii) from losing that advantage to rebases; it does
   not create it.
3. For today's CLIs, the cheapest single cost change is the cache TTL. Long sessions with an API key
   and idle gaps over five minutes pay mostly for re-writing the same prefix. Claude Code already uses
   the 1 hour TTL for subscription users and accepts `promptCacheTtl` for API keys
   (vendor-documented).

## Working-context bound

<!-- BEGIN GENERATED: ranking -->
Bound 30.0k. No strategy fits: the 42.0k head alone exceeds or nearly fills the bound.

| Rank | Strategy | Max working context | Within bound | Lossy resets | Cost | Verdict |
|---|---|---|---|---|---|---|
| 1 | (i) plain | 1.76M | no | 0 | $614.27 | fails: over the bound |
| 2 | H5 tool-result clearing, vendor defaults | 460.0k | no | 738 | $203.55 | fails: over the bound |
| 3 | (ii) compaction at the CLI default (967.0k) | 967.5k | no | 1 | $358.76 | fails: over the bound |

Bound 100.0k.

| Rank | Strategy | Max working context | Within bound | Lossy resets | Cost | Verdict |
|---|---|---|---|---|---|---|
| 1 | (iii) cvm sized to 100.0k (window 12.8k) | 100.0k | yes | 0 | $70.13 | meets the bound with no lossy reset; fidelity depends on recall (L2b) |
| 2 | H4 rollover at 75.0k | 88.1k | yes | 142 | $98.20 | within bound; 142 lossy handoffs |
| 3 | (ii) compaction configured at 75.0k | 91.3k | yes | 181 | $124.34 | fails: compacts |
| 4 | (i) plain | 1.76M | no | 0 | $614.27 | fails: over the bound |
| 5 | H5 tool-result clearing, vendor defaults | 460.0k | no | 738 | $203.55 | fails: over the bound |
| 6 | (ii) compaction at the CLI default (967.0k) | 967.5k | no | 1 | $358.76 | fails: over the bound |

Bound 250.0k.

| Rank | Strategy | Max working context | Within bound | Lossy resets | Cost | Verdict |
|---|---|---|---|---|---|---|
| 1 | (iii) cvm sized to 250.0k (window 62.7k) | 188.2k | yes | 0 | $104.24 | meets the bound with no lossy reset; fidelity depends on recall (L2b) |
| 2 | H4 rollover at 225.0k | 225.2k | yes | 10 | $125.22 | within bound; 10 lossy handoffs |
| 3 | (ii) compaction configured at 225.0k | 225.5k | yes | 10 | $124.05 | fails: compacts |
| 4 | (i) plain | 1.76M | no | 0 | $614.27 | fails: over the bound |
| 5 | H5 tool-result clearing, vendor defaults | 460.0k | no | 738 | $203.55 | fails: over the bound |
| 6 | (ii) compaction at the CLI default (967.0k) | 967.5k | no | 1 | $358.76 | fails: over the bound |

Ranked by: within the bound, no compaction, no lossy reset, smaller working context, then cost. Claude Opus 5.5, 5 min TTL. Lossy resets count compactions, rollover handoffs, and tool-result clears. Quality is not scored here; the bound values are placeholders until the quality-vs-size probe (L2d) measures the real one. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: ranking -->

The bounded (iii) policy in this table pages older turns, and an oversized tool result in the
current turn, before any request would cross the bound. It caps the stub index at the smaller of
15% of the free space or 8k by merging older stubs into coarser index entries. Those sizes are
inferred; L2b owns the policy.

## Assumptions behind the (iii) headline

<!-- BEGIN GENERATED: assumptions -->
- **Billing mode.** The headline uses the 5 minute TTL, Claude Code's default with an API key or usage credits. On a subscription within plan usage the main conversation gets the 1 hour TTL (vendor-documented); there (iii) costs $68.56 against $182.71 for (ii), a ratio of 0.38 instead of 0.24. Which mode the owner's sessions use is not known.
- **The CLI never compacts on its own under (iii).** The proxy bounds the upstream prompt, but the CLI's local history still grows past 967.0k. Whether the CLI then compacts depends on how it counts context, which is brief question 2 (lane 1). Lane 1 PR #9 reports that compaction-forcing settings did not force a pre-send compaction in a `-p` resume (lane 1's measurement, not repeated here). If the CLI does compact at 967.0k of local history, (iii) costs $85.35 instead of $85.02 (1 CLI compactions in 300 turns), and each one is a lossy reset the proxy cannot undo.
- **Same head and caching as a direct connection.** (iii) runs behind a custom `ANTHROPIC_BASE_URL`. There Claude Code may load tool definitions upfront instead of through tool search, caching works only if the proxy forwards `cache_control` markers unchanged, and the 1 hour TTL needs the `anthropic-beta` header forwarded (vendor-documented, https://code.claude.com/docs/en/prompt-caching). The model gives (iii) the same 42.0k head and working cache as (ii); the head-size table shows the effect of a larger head. Lane 1's rows are captured behind a custom base URL, so measured heads include this effect.
- **Idle compaction on the 1 hour TTL.** Claude Code can compact an idle conversation shortly before a 1 hour cache expires (`idleCompaction`, vendor-documented). Modeled for (ii) with the documented 200k minimum: $87.68 instead of $182.71 (6 idle compactions, each a lossy reset). Compaction requests themselves get the 5 minute TTL (vendor-documented), which changes nothing here because the next request rewrites the short history anyway.
- **Think time.** The TTL findings depend on the inferred think-time mix, in which 35% of gaps exceed 5 minutes. With every gap under a minute, (iii) / (ii) is 0.39 instead of 0.24.
<!-- END GENERATED: assumptions -->

Head size, for the custom base URL case:

<!-- BEGIN GENERATED: head_sweep -->
| Head | (ii) at this head | (iii) at this head | (iii) / (ii), same head | (iii) / (ii) at 42.0k |
|---|---|---|---|---|
| 42.0k | $358.76 | $85.02 | 0.24 | 0.24 |
| 60.0k | $365.80 | $98.51 | 0.27 | 0.27 |
| 80.0k | $383.38 | $113.51 | 0.30 | 0.32 |
| 120.0k | $415.54 | $143.50 | 0.35 | 0.40 |

Claude Opus 5.5. Behind a custom `ANTHROPIC_BASE_URL`, Claude Code can load tool definitions upfront, so (iii) may carry a larger head than a direct (ii) session. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: head_sweep -->

Thinking blocks. The model bills thinking as output and by default keeps none of it in the
conversation, for every strategy. Reviewer-2 found that on 5.x models, editing the prefix (dropping
old turns, stubbing tool results, changing system or tools) invalidates preserved thinking blocks
(https://github.com/VentureForgre/contexto/pull/7#issuecomment-6204245614). So (iii) must strip
prior thinking at each rebase, use server-side `context_management`, or reinsert thinking byte for
byte. This model strips it at rebases and never strips the current turn's thinking. If the CLI does
preserve thinking, (i), (ii), and H4 grow faster than the headline shows:

<!-- BEGIN GENERATED: thinking -->
| Strategy | Thinking dropped from context (default) | Thinking preserved | Max prompt, preserved |
|---|---|---|---|
| (i) plain | $614.27 | $716.23 | 2.06M |
| (ii) compaction | $358.76 | $382.10 | 967.3k |
| H4 rollover | $342.94 | $337.07 | 870.9k |
| (iii) cvm | $85.02 | $86.79 | 173.2k |

Claude Opus 5.5. With thinking preserved, (i), (ii), and H4 carry every prior thinking block; (iii) keeps them only until the next rebase, which strips them from the kept window (257.4k tokens stripped). The current turn's thinking is never stripped. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: thinking -->

## Prices and cache rules

<!-- BEGIN GENERATED: prices -->
| Model | Input | Output | Cache read | Cache write by TTL | Window | Long-context pricing |
|---|---|---|---|---|---|---|
| Claude Fable 5.1 | $10 | $50 | $0.25 | 5m: $12.5, 1h: $20 | 1.00M | none |
| Claude Opus 5.5 | $4 | $20 | $0.2 | 5m: $5, 1h: $8 | 1.00M | none |
| Claude Haiku 5.5 | $0.1 | $0.5 | $0.01 | 5m: $0.125, 1h: $0.2 | 1.00M | x5 input, x5 output above 100.0k |
| gpt-6-astra | $10 | $50 | $1 | 30m: $12.5 | 1.05M | x2 input, x1.5 output above 272.0k |
| gpt-6-luna | $0.1 | $0.5 | $0.01 | 30m: $0.125 | 1.05M | x2 input, x1.5 output above 272.0k |

USD per million tokens, retrieved 2026-10-15, vendor-documented. Source URL per value: `spikes/m0/cost_model/prices.json`.
<!-- END GENERATED: prices -->

Cache behavior the model relies on (all vendor-documented, retrieved 2026-10-15):

- Anthropic: cache writes cost 1.25x input at the 5 minute TTL and 2x at 1 hour. Reads cost 0.05x
  input on Opus 5.5 and Sonnet 5.5, 0.025x on Fable 5.1, and 0.1x on Haiku 5.5. A cache hit refreshes
  the entry at no cost. A request can carry up to 4 breakpoints, and each breakpoint looks back at
  most 20 block positions for an earlier hit
  (https://platform.claude.com/docs/en/build-with-claude/prompt-caching). The docs say a 5 minute
  entry is refreshed on use; they do not say so explicitly for 1 hour entries, which the model
  assumes (inferred).
- Anthropic Haiku 5.5 is the one model priced by prompt length: above 100,000 tokens, cache reads and
  writes included, every rate is 5x. Fable 5.1, Opus 5.5, and Sonnet 5.5 bill a 900K prompt at the
  same rate as a 9K one (https://platform.claude.com/docs/en/about-claude/pricing).
- OpenAI GPT-5.6 and later: caching is automatic, writes cost 1.25x input, reads 0.1x. The only TTL
  is 30 minutes after the last write or reuse, and reuse refreshes it. Above 272K input tokens input
  rates double and output rates rise 1.5x (https://developers.openai.com/api/docs/guides/prompt-caching,
  https://developers.openai.com/api/docs/pricing).
- Claude Code resends the whole conversation every request and orders it system prompt, then project
  context, then conversation. The model assumes its last breakpoint sits on the newest message, so
  every miss token is billed as a cache write (inferred from the same page, which describes the marker
  on the last conversation message). The main conversation uses the 5 minute TTL with an API key or usage credits and the
  1 hour TTL on a subscription within plan usage; `promptCacheTtl` or
  `CLAUDE_CODE_PROMPT_CACHE_TTL` overrides it (https://code.claude.com/docs/en/prompt-caching).
  This is why every "Uncached in" column below is zero.

## Session profile

The profile is a parameter file, `spikes/m0/cost_model/profiles/synthetic_realistic.json`. The head
(everything fixed before the first user turn) and the per-turn growth are separate parameters, so a
measured head and a measured growth shape can be dropped in independently. Each tool call is its own
API request, as in Claude Code. The 42k head is inferred; it is consistent with lane 1's preliminary,
unreviewed first request for Claude Code default mode, which is not cited here as a measurement.

<!-- BEGIN GENERATED: profile -->
| Quantity (inferred profile) | Value |
|---|---|
| Turns | 300 |
| API requests, one per tool round trip | 1149 (3.83 per turn) |
| Head: system prompt and tools | 42.0k |
| Context growth per turn, mean | 5.7k |
| Full history after the last turn | 1.76M |
| Tool results: p50 / p90 / max | 0.7k / 3.6k / 25.0k |
| Output tokens, thinking included | 589.9k |
| Think time over 5 min / 30 min / 1 h | 35% / 8% / 4% of turns |
| Turns with a recall, (iii) only | 32 |
| Turns that change pinned state | 42 |
<!-- END GENERATED: profile -->

## Strategies as modeled

All four strategies replay the same seeded request trace. The cache engine treats the prompt as a
list of blocks. A request reads from cache the longest prefix of blocks unchanged since the last
request, if that request was within the TTL, and writes the rest. A cache break is any token written
that was not new since the last request; breaks are attributed to a cause (TTL expiry, rebase,
compaction, rollover, pinned-state change, or clearing).

- **(i) plain**: the full history every request. It passes the 1M window partway through a 300 turn
  session; the table keeps going so the cost of not resetting is visible.
- **(ii) built-in compaction**: when the next request would reach the CLI's threshold, the session
  sends the current prompt plus a summarization instruction, gets a summary back, and continues from
  the head plus the summary and re-attached files. Thresholds: Claude Code compacts native 1M models
  at about 967K tokens by default. `/autocompact` and `--autocompact` accept 100K to 1M, and
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` takes a token count capped at the model's window
  (vendor-documented, https://code.claude.com/docs/en/model-config). Codex sends the full input on
  every request (`store: false`, no `previous_response_id`) and auto-compacts at about 90% of a 272K
  window, 244.8k tokens (measured by lane 1, PR #9; it matches the Codex source tests). Because Codex
  runs a 272K window, the OpenAI (i) rows past 272K are hypothetical. The summary request reads the warm prefix and keeps the system-prompt layer
  (vendor-documented). Summary size (8k) and re-attached context (15k) are inferred; Claude Code
  documents what it restores (up to 5 recent files at 5K each, skills up to 25K) but not typical sizes.
- **H4 supervisor rollover**: the same mechanism with a supervisor in place of the CLI. At 90% of the
  compaction threshold the outgoing session writes a 4k handoff, and a fresh session starts from the
  head plus a 20k seed (handoff plus onboarding reads). All rollover numbers are inferred. The fresh
  session reuses the cached system head; Claude Code documents that sequential sessions share the
  rest of the prefix only when the startup git snapshot matches, which the seed size absorbs.
- **(iii) context virtual memory**: the layout and policy in the next section. It assumes the CLI
  never compacts its own local history and that (iii) gets the same head and working cache as a
  direct connection; "Assumptions behind the (iii) headline" states both and their effect. Recalls are extra
  upstream round trips the proxy answers itself (brief question 1). Pinned state grows from 3k by 15
  tokens a turn up to 12k; each paged turn leaves a 60 token stub. All inferred; packet L2b owns the
  paging and pinning policy and will replace these sizes.
- **H5 provider-native context management**: costed as variants of (i) and (ii) in "Provider-native
  context management" below.

## Results

<!-- BEGIN GENERATED: headline -->
| Model | Strategy | Uncached in | Cache write | Cache read | Output | Cost | Max prompt | Note |
|---|---|---|---|---|---|---|---|---|
| Claude Fable 5.1 | (i) plain | 0.0k | 84.16M | 908.46M | 589.9k | $1,308.57 | 1.76M | over the 1.00M window from turn 184 |
| Claude Fable 5.1 | (ii) compaction | 0.0k | 49.00M | 508.93M | 597.9k | $769.66 | 967.5k | 1 compactions |
| Claude Fable 5.1 | H4 rollover | 0.0k | 47.81M | 459.76M | 597.9k | $742.42 | 871.0k | 2 rollovers |
| Claude Fable 5.1 | (iii) cvm | 0.0k | 10.54M | 102.29M | 593.8k | $186.97 | 168.0k | 108 rebases |
| Claude Opus 5.5 | (i) plain | 0.0k | 84.16M | 908.46M | 589.9k | $614.27 | 1.76M | over the 1.00M window from turn 184 |
| Claude Opus 5.5 | (ii) compaction | 0.0k | 49.00M | 508.93M | 597.9k | $358.76 | 967.5k | 1 compactions |
| Claude Opus 5.5 | H4 rollover | 0.0k | 47.81M | 459.76M | 597.9k | $342.94 | 871.0k | 2 rollovers |
| Claude Opus 5.5 | (iii) cvm | 0.0k | 10.54M | 102.29M | 593.8k | $85.02 | 168.0k | 108 rebases |
| Claude Haiku 5.5 | (i) plain | 0.0k | 84.16M | 908.46M | 589.9k | $99.16 | 1.76M | over the 1.00M window from turn 184 |
| Claude Haiku 5.5 | (ii) compaction | 0.0k | 49.00M | 508.93M | 597.9k | $57.14 | 967.5k | 1 compactions |
| Claude Haiku 5.5 | H4 rollover | 0.0k | 47.81M | 459.76M | 597.9k | $53.65 | 871.0k | 2 rollovers |
| Claude Haiku 5.5 | (iii) cvm | 0.0k | 16.85M | 84.42M | 593.8k | $3.82 | 134.1k | 259 rebases |
| gpt-6-astra | (i) plain | 0.0k | 20.51M | 972.11M | 589.9k | $2,463.05 | 1.76M | over the 1.05M window from turn 193 |
| gpt-6-astra | (ii) compaction | 0.0k | 5.39M | 171.85M | 661.9k | $272.32 | 245.6k | 9 compactions |
| gpt-6-astra | H4 rollover | 0.0k | 5.33M | 159.45M | 629.9k | $257.52 | 220.8k | 10 rollovers |
| gpt-6-astra | (iii) cvm | 0.0k | 4.76M | 116.62M | 593.8k | $205.75 | 178.6k | 49 rebases |
| gpt-6-luna | (i) plain | 0.0k | 20.51M | 972.11M | 589.9k | $24.63 | 1.76M | over the 1.05M window from turn 193 |
| gpt-6-luna | (ii) compaction | 0.0k | 5.39M | 171.85M | 661.9k | $2.72 | 245.6k | 9 compactions |
| gpt-6-luna | H4 rollover | 0.0k | 5.33M | 159.45M | 629.9k | $2.58 | 220.8k | 10 rollovers |
| gpt-6-luna | (iii) cvm | 0.0k | 4.76M | 116.62M | 593.8k | $2.06 | 178.6k | 49 rebases |

Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: headline -->

<!-- BEGIN GENERATED: ratios -->
| Model | (i) plain | (ii) compaction | H4 rollover | (iii) cvm | (iii) / (ii) | H4 / (ii) |
|---|---|---|---|---|---|---|
| Claude Fable 5.1 | $1,308.57 | $769.66 | $742.42 | $186.97 | 0.24 | 0.96 |
| Claude Opus 5.5 | $614.27 | $358.76 | $342.94 | $85.02 | 0.24 | 0.96 |
| Claude Haiku 5.5 | $99.16 | $57.14 | $53.65 | $3.82 | 0.07 | 0.94 |
| gpt-6-astra | $2,463.05 | $272.32 | $257.52 | $205.75 | 0.76 | 0.95 |
| gpt-6-luna | $24.63 | $2.72 | $2.58 | $2.06 | 0.76 | 0.95 |

Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: ratios -->

Per turn and cumulative, for the focus model:

<!-- BEGIN GENERATED: per_turn -->
| Turn | (i) plain prompt | (i) plain cumulative | (ii) compaction prompt | (ii) compaction cumulative | H4 rollover prompt | H4 rollover cumulative | (iii) cvm prompt | (iii) cvm cumulative |
|---|---|---|---|---|---|---|---|---|
| 1 | 45.5k | $0.30 | 45.5k | $0.30 | 45.5k | $0.30 | 48.5k | $0.32 |
| 25 | 150.9k | $9.33 | 150.9k | $9.33 | 150.9k | $9.33 | 86.4k | $7.26 |
| 50 | 319.0k | $26.65 | 319.0k | $26.65 | 319.0k | $26.65 | 78.1k | $14.84 |
| 75 | 451.4k | $50.69 | 451.4k | $50.69 | 451.4k | $50.69 | 124.4k | $21.30 |
| 100 | 606.0k | $84.21 | 606.0k | $84.21 | 606.0k | $84.21 | 114.1k | $28.23 |
| 125 | 737.0k | $132.28 | 737.0k | $132.28 | 737.0k | $132.28 | 112.9k | $35.60 |
| 150 | 823.6k | $176.00 | 823.6k | $176.00 | 823.6k | $176.00 | 106.6k | $41.60 |
| 175 | 949.1k | $217.80 | 949.1k | $217.80 | 140.4k | $194.22 | 122.5k | $47.15 |
| 200 | 1.09M | $292.21 | 186.3k | $240.90 | 279.6k | $211.04 | 93.8k | $55.12 |
| 225 | 1.23M | $350.55 | 331.8k | $255.58 | 425.1k | $230.25 | 106.1k | $61.63 |
| 250 | 1.41M | $435.80 | 504.3k | $283.47 | 597.6k | $264.07 | 99.0k | $69.64 |
| 275 | 1.56M | $545.26 | 657.9k | $326.72 | 751.2k | $314.17 | 93.5k | $78.36 |
| 300 | 1.76M | $614.27 | 861.5k | $358.76 | 146.5k | $342.94 | 144.8k | $85.02 |

Claude Opus 5.5. Prompt is the largest request in the turn. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: per_turn -->

<!-- BEGIN GENERATED: per_turn_buckets -->
| Turn | Strategy | Requests | Uncached in | Cache write | Cache read | Output | Turn cost |
|---|---|---|---|---|---|---|---|
| 1 | (i) plain | 5 | 0.0k | 45.5k | 173.6k | 1.9k | $0.301 |
| 1 | (ii) compaction | 5 | 0.0k | 45.5k | 173.6k | 1.9k | $0.301 |
| 1 | H4 rollover | 5 | 0.0k | 45.5k | 173.6k | 1.9k | $0.301 |
| 1 | (iii) cvm | 5 | 0.0k | 48.5k | 185.6k | 1.9k | $0.318 |
| 50 | (i) plain | 5 | 0.0k | 319.0k | 1.27M | 5.0k | $1.950 |
| 50 | (ii) compaction | 5 | 0.0k | 319.0k | 1.27M | 5.0k | $1.950 |
| 50 | H4 rollover | 5 | 0.0k | 319.0k | 1.27M | 5.0k | $1.950 |
| 50 | (iii) cvm | 5 | 0.0k | 78.1k | 309.6k | 5.0k | $0.553 |
| 100 | (i) plain | 4 | 0.0k | 12.2k | 2.38M | 2.9k | $0.596 |
| 100 | (ii) compaction | 4 | 0.0k | 12.2k | 2.38M | 2.9k | $0.596 |
| 100 | H4 rollover | 4 | 0.0k | 12.2k | 2.38M | 2.9k | $0.596 |
| 100 | (iii) cvm | 4 | 0.0k | 12.2k | 416.7k | 2.9k | $0.202 |
| 150 | (i) plain | 3 | 0.0k | 1.3k | 2.47M | 1.1k | $0.522 |
| 150 | (ii) compaction | 3 | 0.0k | 1.3k | 2.47M | 1.1k | $0.522 |
| 150 | H4 rollover | 3 | 0.0k | 1.3k | 2.47M | 1.1k | $0.522 |
| 150 | (iii) cvm | 3 | 0.0k | 1.3k | 317.0k | 1.1k | $0.092 |
| 200 | (i) plain | 2 | 0.0k | 6.0k | 2.17M | 0.9k | $0.481 |
| 200 | (ii) compaction | 2 | 0.0k | 6.0k | 363.3k | 0.9k | $0.120 |
| 200 | H4 rollover | 2 | 0.0k | 6.0k | 550.0k | 0.9k | $0.158 |
| 200 | (iii) cvm | 2 | 0.0k | 6.0k | 178.4k | 0.9k | $0.083 |
| 250 | (i) plain | 4 | 0.0k | 1.41M | 4.20M | 2.6k | $7.924 |
| 250 | (ii) compaction | 4 | 0.0k | 504.3k | 1.50M | 2.6k | $2.873 |
| 250 | H4 rollover | 4 | 0.0k | 597.6k | 1.78M | 2.6k | $3.396 |
| 250 | (iii) cvm | 4 | 0.0k | 99.0k | 282.5k | 2.6k | $0.603 |
| 300 | (i) plain | 6 | 0.0k | 36.7k | 10.38M | 3.1k | $2.321 |
| 300 | (ii) compaction | 6 | 0.0k | 36.7k | 4.97M | 3.1k | $1.239 |
| 300 | H4 rollover | 6 | 0.0k | 36.7k | 677.3k | 3.1k | $0.381 |
| 300 | (iii) cvm | 6 | 0.0k | 36.7k | 667.4k | 3.1k | $0.379 |

Claude Opus 5.5. Full per-turn rows for every model: `cost_model.py --per-turn-csv` (written under `.contexto/spikes/l2a/`). Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: per_turn_buckets -->

## Cache-friendly prompt layout for (iii)

```
[system prompt + tool definitions]  breakpoint 1   never changes in a session
[pinned state block][stub index]    breakpoint 2   rebuilt only at an epoch rebase
[verbatim window: whole turns, append-only]        grows within an epoch
[newest message]                    breakpoint 3   moves to the end every request
```

Design rules (inferred from the vendor cache rules above):

- The system head comes first and never changes, so every rebase and every expiry-free request reads
  it from cache. Breakpoint 1 makes that hit independent of the 20-position lookback.
- Pinned state and stubs sit directly after the head and change only at a rebase. A change anywhere
  there invalidates everything after it, so pinned updates are batched to epochs; between rebases a
  new decision or obligation is still visible verbatim in the window.
- The window is append-only within an epoch, so each request writes only what it added. A rebase
  pages the oldest whole turns out (keeping each tool_use and tool_result pair together), keeps at
  most `window_tokens` of recent turns, adds their stubs, and rebuilds pinned state. That rewrites
  pinned state, stubs, and the kept window once.
- Rebase on expiry: when an idle gap has already expired the cache, the next request rewrites the
  prefix anyway, so the policy rebases then at almost no extra cost.
- Price tiers: on a model priced by prompt length (Haiku 5.5 above 100K; OpenAI above 272K) the
  policy rebases before the prompt would cross the threshold.
- Breakpoint budget: three of Anthropic's four breakpoints are used, leaving one spare.

How often the prefix breaks, and what each cause costs:

<!-- BEGIN GENERATED: breaks -->
| Strategy | Cause | Breaks | Tokens rewritten | Cost above a cache read |
|---|---|---|---|---|
| (i) plain | ttl | 106 | 82.39M | $395.49 |
| (ii) compaction | compaction | 1 | 23.0k | $0.11 |
| (ii) compaction | ttl | 106 | 47.22M | $226.63 |
| H4 rollover | rollover | 2 | 40.0k | $0.19 |
| H4 rollover | ttl | 106 | 46.00M | $220.81 |
| (iii) cvm | rebase | 2 | 108.2k | $0.52 |
| (iii) cvm | ttl | 106 | 8.61M | $41.33 |

Claude Opus 5.5, 5 min TTL. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: breaks -->

Layout and policy variants:

<!-- BEGIN GENERATED: variants -->
| (iii) variant | Cost | Cache breaks | Break cost | Pings | Max prompt |
|---|---|---|---|---|---|
| default: pinned head, rebase every 8 turns or on expiry | $85.02 | 108 | $41.85 | 0 | 168.0k |
| no rebase on expiry | $101.92 | 130 | $56.27 | 0 | 188.0k |
| pinned state rebuilt at once on every change | $90.94 | 130 | $47.77 | 0 | 168.0k |
| pinned state at the tail, re-sent every request | $112.36 | 1180 | $69.19 | 0 | 168.0k |
| tool results over 8k paged at once | $83.62 | 108 | $42.62 | 0 | 142.4k |
| 1 h cache TTL | $68.56 | 45 | $17.72 | 0 | 157.5k |
| keepalive pings through idle gaps up to 1 h | $62.07 | 45 | $10.94 | 269 | 157.5k |
| CLI still compacts its local history at the CLI threshold (brief Q2) | $85.35 | 109 | $42.01 | 0 | 168.1k |

Claude Opus 5.5. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: variants -->

TTL and keepalive across all strategies:

<!-- BEGIN GENERATED: ttl -->
| Cache setting | (i) plain | (ii) compaction | H4 rollover | (iii) cvm |
|---|---|---|---|---|
| 5 min TTL (Claude Code default with an API key) | $614.27 | $358.76 | $342.94 | $85.02 |
| 1 h TTL (Claude Code main conversation on a subscription) | $290.41 | $182.71 | $168.65 | $68.56 |
| 1 h TTL with Claude Code idleCompaction ((ii) only) |  | $87.68 |  |  |
| 5 min TTL plus keepalive pings up to 1 h idle | $305.41 | $186.09 | $172.52 | $62.07 |

Claude Opus 5.5. Subscription use is billed against plan limits, not dollars; the dollar column is the API-price equivalent of the same tokens. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: ttl -->

Keepalive pings are ordinary requests that re-read the prefix with a one-token reply; whether a
proxy may send requests the user never typed is an owner decision under brief question 5.

### Epoch policy

Within an epoch of E turns, each of n requests per turn re-reads the growth g appended since the
last rebase. A rebase rewrites R tokens (pinned state, stubs, kept window) at write price w instead
of reading them at r. Per turn that is (w - r) R / E + r n g E / 2, minimized at
E* = sqrt(2 (w - r) R / (r n g)). Larger windows want longer epochs. With idle gaps, rebases on
expiry come more often than E* and set the cadence.

<!-- BEGIN GENERATED: epoch_rule -->
| Window | Analytic E* | Simulated best E, no idle gaps | Simulated best E, profile think time |
|---|---|---|---|
| 8.0k | 6.6 | 5 | 12 |
| 16.0k | 7.8 | 8 | 12 |
| 32.0k | 9.8 | 8 | 12 |
| 64.0k | 12.9 | 12 | 12 |
| 128.0k | 17.5 | 20 | 12 |

Claude Opus 5.5: write $5, read $0.2 per MTok. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: epoch_rule -->

Sensitivity over window size and epoch length:

<!-- BEGIN GENERATED: sweep -->
| Window | E=3 | E=5 | E=8 | E=12 | E=20 | E=40 | best / (ii) |
|---|---|---|---|---|---|---|---|
| 8.0k | $70.01 | $68.59 | $68.21 | **$68.19** | **$68.19** | **$68.19** | 0.19 |
| 16.0k | $76.20 | $74.15 | $73.30 | **$73.16** | **$73.16** | **$73.16** | 0.20 |
| 32.0k | $90.04 | $86.38 | $85.02 | **$84.67** | **$84.67** | **$84.67** | 0.24 |
| 64.0k | $117.69 | $109.87 | $106.93 | **$106.48** | **$106.48** | **$106.48** | 0.30 |
| 128.0k | $172.16 | $157.44 | $151.85 | **$150.67** | **$150.67** | **$150.67** | 0.42 |

(iii) cost for Claude Opus 5.5, rebase on expiry on. Bold is the cheapest epoch length for each window. (ii) costs $358.76. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: sweep -->

## Where (iii) wins and where it does not

Session length:

<!-- BEGIN GENERATED: by_length -->
| Turns | (i) plain | (ii) compaction | H4 rollover | (iii) cvm | (iii) / (ii) | Compactions |
|---|---|---|---|---|---|---|
| 1 | $0.30 | $0.30 | $0.30 | $0.32 | 1.06 | 0 |
| 5 | $1.52 | $1.52 | $1.52 | $1.60 | 1.05 | 0 |
| 10 | $2.71 | $2.71 | $2.71 | $2.85 | 1.05 | 0 |
| 25 | $9.33 | $9.33 | $9.33 | $7.26 | 0.78 | 0 |
| 50 | $26.65 | $26.65 | $26.65 | $14.84 | 0.56 | 0 |
| 100 | $84.21 | $84.21 | $84.21 | $28.23 | 0.34 | 0 |
| 200 | $292.21 | $240.90 | $211.04 | $55.12 | 0.23 | 1 |
| 300 | $614.27 | $358.76 | $342.94 | $85.02 | 0.24 | 1 |
| 600 | $2,403.71 | $735.20 | $642.70 | $180.37 | 0.25 | 3 |
| 1000 | $6,699.47 | $1,227.87 | $1,140.28 | $331.24 | 0.27 | 6 |

Claude Opus 5.5. Basis: modeled on the `synthetic-realistic` profile (inferred), seed 20261015.
<!-- END GENERATED: by_length -->

Tool output size:

<!-- BEGIN GENERATED: by_tool_size -->
| Tool output median | Growth per turn | (i) plain | (ii) compaction | H4 rollover | (iii) cvm | (iii) / (ii) |
|---|---|---|---|---|---|---|
| 0.25x | 2.5k | $298.03 | $298.03 | $298.03 | $79.50 | 0.27 |
| 0.5x | 3.6k | $407.79 | $347.10 | $304.52 | $81.52 | 0.23 |
| 1x (profile) | 5.7k | $614.27 | $358.76 | $342.94 | $85.02 | 0.24 |
| 2x | 9.6k | $1,003.13 | $375.47 | $345.95 | $90.55 | 0.24 |
| 4x | 16.0k | $1,680.88 | $391.87 | $360.82 | $100.01 | 0.26 |

Claude Opus 5.5. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: by_tool_size -->

Think time:

<!-- BEGIN GENERATED: by_think -->
| Think time | Gaps over 5 min | (i) plain | (ii) compaction | H4 rollover | (iii) cvm | (iii) / (ii) |
|---|---|---|---|---|---|---|
| always under 1 min | 0% | $231.79 | $136.06 | $124.54 | $52.47 | 0.39 |
| profile mix | 35% | $614.27 | $358.76 | $342.94 | $85.02 | 0.24 |
| mostly 10 to 40 min | 100% | $1,525.66 | $846.29 | $770.71 | $157.27 | 0.19 |

Claude Opus 5.5, 5 min TTL. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: by_think -->

Compaction and rollover threshold. Lower thresholds make (ii) and H4 much cheaper, at the price of
more lossy resets:

<!-- BEGIN GENERATED: reset_sweep -->
| Reset threshold | (ii) compaction | Compactions | H4 rollover | Rollovers | (iii) / (ii) |
|---|---|---|---|---|---|
| 100.0k | $95.88 | 52 | $89.37 | 47 | 0.89 |
| 200.0k | $113.60 | 12 | $115.55 | 12 | 0.75 |
| 400.0k | $180.53 | 5 | $180.70 | 5 | 0.47 |
| 967.0k | $358.76 | 1 | $357.81 | 1 | 0.24 |

Claude Opus 5.5. Claude Code's `/autocompact` and `--autocompact` accept 100K to 1M; `CLAUDE_CODE_AUTO_COMPACT_WINDOW` takes a token count capped at the model's window (vendor-documented). Every compaction is a failure under the owner's bar; the table shows cost only. (iii) costs $85.02. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: reset_sweep -->

Whether the compaction request reads the cache:

<!-- BEGIN GENERATED: compaction_cache -->
| Threshold | (ii) summary request | Compactions | (ii) cost | (iii) / (ii) |
|---|---|---|---|---|
| 967.0k | reads the warm prefix (vendor-documented) | 1 | $358.76 | 0.24 |
| 967.0k | full miss (as after an idle gap) | 1 | $363.39 | 0.23 |
| 200.0k | reads the warm prefix (vendor-documented) | 12 | $113.60 | 0.75 |
| 200.0k | full miss (as after an idle gap) | 12 | $124.96 | 0.68 |

Claude Opus 5.5. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: compaction_cache -->

Recall load. If the model recalls often or recalls large pages, (iii)'s advantage shrinks but holds
on this profile:

<!-- BEGIN GENERATED: by_recall -->
| Recalls per turn | Recall size | Recalls | (iii) cost | (iii) / (ii) |
|---|---|---|---|---|
| 0 | 1.5k | 0 | $83.97 | 0.23 |
| 0.1 | 1.5k | 32 | $85.02 | 0.24 |
| 0.5 | 1.5k | 143 | $88.64 | 0.25 |
| 1 | 1.5k | 300 | $93.39 | 0.26 |
| 0.5 | 8.0k | 143 | $93.77 | 0.26 |
| 2 | 8.0k | 300 | $105.23 | 0.29 |

Claude Opus 5.5. Each recall is one extra upstream round trip that the proxy answers itself. (ii) costs $358.76. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: by_recall -->

Summary of the boundaries, inferred from the tables: on cost, (iii) loses for sessions shorter than
the crossover turn in the summary; its advantage over (ii) is smallest when (ii) is configured to
compact early, and largest for long sessions, long think times on a short TTL, and small models with
prompt-length pricing. Early compaction buys its cost by compacting, which the owner's bar rules out.

## Provider-native context management (H5)

Packet L2b owns the H5 write-up; this section only costs the cache effect.

- Anthropic context editing (beta header `context-management-2025-06-27`) clears old tool results
  server-side with `clear_tool_uses_20250919`. Defaults: trigger at 100,000 input tokens, keep the 3
  most recent tool uses, `clear_at_least` unset. The docs say clearing "invalidates cached prompt
  prefixes" and that `clear_at_least` exists to make that worthwhile
  (https://platform.claude.com/docs/en/build-with-claude/context-editing, vendor-documented). The
  placeholder text and its size are not documented; the model uses 15 tokens (inferred).
- OpenAI server-side compaction (`context_management` with `compact_threshold`) emits an opaque
  compaction item and prunes context; the prompt caching guide says context management, compaction,
  or truncation "can change the prefix and reset cache reuse"
  (https://developers.openai.com/api/docs/guides/compaction,
  https://developers.openai.com/api/docs/guides/prompt-caching, vendor-documented). The compacted item
  size is not documented.

<!-- BEGIN GENERATED: h5 -->
| H5 provider-native setting | Cost | Clears or compactions | Max prompt | vs (i) plain | (iii) / this |
|---|---|---|---|---|---|
| trigger 100k, keep 3, clear_at_least 0 (vendor defaults) | $203.55 | 738 | 460.0k | 0.33 | 0.42 |
| trigger 100k, keep 3, clear_at_least 20k | $196.53 | 54 | 478.3k | 0.32 | 0.43 |
| trigger 100k, keep 3, clear_at_least 50k | $205.31 | 24 | 500.6k | 0.33 | 0.41 |
| trigger 200k, keep 3, clear_at_least 50k | $209.38 | 22 | 500.6k | 0.34 | 0.41 |
| gpt-6-astra: server-side compaction, compact_threshold 200k | $241.01 | 12 | 201.4k | 0.10 | 0.85 |

Claude Opus 5.5 unless named. Clearing replaces each cleared tool result with a 15-token placeholder (placeholder size inferred) and breaks the cache from the first cleared block. OpenAI's compacted item size is not documented; the row reuses the (ii) summary assumption. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: h5 -->

Tool-result clearing alone leaves user messages and assistant text growing, so it bounds the prompt
less than (iii) does and still breaks the cache every time it fires.

## Lane 1 data

Interface (confirmed by manager-1 through manager-2): one row per upstream request with `turn`,
`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`,
`request_bytes`, `message_count`, plus `cli`, `model`, `ts`, `endpoint`. The loader accepts JSONL or
CSV.

- `input_tokens` is the uncached part. Lane 1 already subtracts cached tokens from Codex totals.
- `cache_creation_input_tokens` may be empty or missing (Codex: the Responses API does not report
  cache writes). The loader reads it as unknown. Pricing then shows a range: as an upper bound every
  uncached token is priced as a cache write (right for GPT-5.6 and later, which bill writes); as a
  lower bound it is priced as plain input.
- `ts` sets the idle gap before each turn, so measured think time drives cache expiry on replay.
- A prompt that shrinks between rows marks a compaction or reset in the measured session; it is
  counted and adds no growth on replay.
- Replay builds a trace from measured rows: the first prompt becomes the head and each later prompt's
  growth becomes that request's addition. That replays (i), (ii), H4, and (iii) on measured growth.

Status: lane 1's first Codex run is in review (PR #10 at 8155abd), and a pinned copy of its
per-request file is used below. The Claude Code numbers have not landed. Until they do, the Claude
headline stays modeled on the inferred profile. `cost_model.py --measured <file>` prices observed
buckets (measured) and replays the four strategies (modeled).

Measured Codex run:

<!-- BEGIN GENERATED: lane1_codex -->
| Codex, measured | Value |
|---|---|
| Source | lane 1 PR #10 at 8155abd (in review), `data/lane1_pr10_8155abd_codex_requests.csv` |
| Basis | measured (pending lane 1 review) |
| Requests / user turns / shrinking prompts | 11 / 6 / 0 |
| Head: first request prompt | 26.5k |
| Growth per later turn, mean | 2.5k |
| Cache reads as a share of input | 90% |
| Observed buckets priced on gpt-6-astra, cache writes unknown: as writes / as input | $0.90 / $0.80 |
| Modeled: measured turns tiled to 300 turns on gpt-6-astra: (i) plain / (ii) compaction / H4 rollover / (iii) cvm | $421.04 / $88.20 / $82.31 / $71.77 |
| Modeled: max working context, (ii) / (iii) | 244.7k / 101.4k |

`input_tokens` is uncached input (Codex input minus cached). `cache_creation_input_tokens` is written as 0 in the file but Codex does not report it, so it is loaded as unknown and priced as a range. `request_bytes` is empty. Lane 1 reports a 32.3k head, the context after the first turn; the 26.5k above is the first request's prompt. Tiling repeats the measured turns after the first; it keeps the measured head, growth shape, and gaps but is a model, not a measurement.
<!-- END GENERATED: lane1_codex -->

Loader check on synthetic files:

<!-- BEGIN GENERATED: lane1 -->
| Quantity | Value |
|---|---|
| Files | `testdata/lane1_sample.jsonl` (Claude Code shape), `testdata/lane1_codex_sample.csv` (Codex shape) |
| Basis | synthetic test input, not measured |
| Claude rows / turns / shrinking prompts | 6 / 4 / 1 |
| Claude rows priced as reported, Claude Opus 5.5 | $0.57 |
| Claude rows replayed: (i) plain / (ii) compaction / H4 rollover / (iii) cvm | $0.57 / $0.57 / $0.57 / $0.60 |
| Codex rows priced on gpt-6-astra, empty cache writes as writes / as input | $0.50 / $0.42 |
<!-- END GENERATED: lane1 -->

## Limits

- The bounded (iii) policy is enforced per request, but only at whole-block granularity: a single
  tool result larger than the free space is cut to a 400 token excerpt and must be recalled.
- Every workload number is inferred, except the Codex rows from lane 1. The largest unknowns are think time (it drives TTL expiry), tool
  output sizes, and requests per turn; the sweeps bound their effect but do not replace measurement.
- (iii) is modeled with no fidelity cost. A bounded window and stubs lose detail that recall may or
  may not recover; packet L2b measures that.
- The engine assumes one cache entry per prompt and exact prefix matching on block boundaries. It
  ignores minimum cacheable lengths (512 or 1,024 tokens, far below these prompts), OpenAI's
  reporting granularity on older models, and output-side effects of a smaller prompt.
- Compaction summaries, rollover handoffs, pinned-state sizes, and stub sizes are assumptions.
- Subscription use is limited by plan usage, not billed per token; dollar figures for subscription
  settings are API-price equivalents.

## Open questions

1. Lane 1, Claude Code rows: does `turn` count user turns, as the Codex file does (with a separate
   `request` index)? The loader groups rows by `turn`.
2. Lane 1: for Codex, does every uncached input token become a cache write on GPT-6 models? That
   decides which end of the pricing range is right.
3. Lane 1 (brief question 2): does Claude Code compact its own local history when a proxy bounds the
   upstream prompt? The assumptions section prices the case where it does.
4. Owner (brief question 5): may a proxy send keepalive requests the user never typed?
5. L2b: pinned-state size, stub size, stub-index cap, and recall rate for the policy it recommends;
   this model takes them as parameters.
6. L2d: the real working-context bound. The ranking uses 30k, 100k, and 250k until then.

## Reproduce

```sh
uv run python spikes/m0/cost_model/cost_model.py                 # print every table
uv run python spikes/m0/cost_model/cost_model.py --write-doc     # regenerate this document's tables
uv run python spikes/m0/cost_model/cost_model.py --check-doc     # exit 1 if this document is stale
uv run python spikes/m0/cost_model/cost_model.py --per-turn-csv  # full per-turn rows, .contexto/spikes/l2a/
uv run python spikes/m0/cost_model/cost_model.py --measured FILE # price and replay lane 1 rows
uv run --with pytest pytest spikes/m0/cost_model
```
