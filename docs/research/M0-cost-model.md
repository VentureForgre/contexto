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
- Under the owner's bar (no compaction, a hard working-context bound), only (iii) meets a 100k or 250k bound without a lossy reset: (i) and (ii) at the CLI default exceed it, (ii) configured to reset inside the bound compacts, H4 hands off lossily, and tool-result clearing still grows past it. No strategy meets 30k with a 42.0k head. Sized to 100k, (iii) costs $73.83 over 300 turns with a 99.6k maximum working context.
- Cost, second: over 300 turns on Claude Opus 5.5 with API-key billing (5 min TTL), (iii) costs $87.95 against $382.10 for (ii) built-in compaction, $337.07 for H4 rollover, and $716.23 for (i) plain, which would pass the 1.00M window at turn 157. Across the five models (iii) costs 0.07 to 0.76 of (ii) (Claude Haiku 5.5 to gpt-6-astra).
- On the inferred think-time mix (35% of gaps over 5 min), cache expiry is the largest single cost in (i): 65% of its cost is the premium (write price minus read price) for re-writing prefixes after idle gaps over the 5 minute TTL. The 1 h TTL cuts (i) to $337.42 and (ii) to $173.31; (iii) gains less ($72.84) because its prefix is small. Keepalive pings bring (iii) to $65.72.
- Bounding the prompt is most of the cost saving: compaction configured at 100k costs $100.79, within 15% of (iii), but only by compacting 61 times, which the owner's bar rules out.
- (iii) costs more than (ii) for very short sessions: it first beats (ii) at turn 14. Its fixed overhead is the pinned block, stubs, and recall round trips.
- Under the profile's think time, rebases mostly ride on cache expiry, so epoch length barely matters and window size sets the cost. With no idle gaps the analytic rule E* = sqrt(2 (w - r) R / (r n g)) lands within 44% of the simulated optimum on the tested grid.
<!-- END GENERATED: findings -->

How strategies are judged (owner principle, relayed by manager-2 on 2026-10-15): compaction is a
failure mode, and the success bar is no compaction ever with accuracy equal to the full-context
baseline. A maximum working context is a hard constraint. Strategies rank first by whether they stay
inside that bound without a lossy reset, then by working-context size, and only then by token cost.
Quality is not scored by this model; packet L2b's fidelity test and the quality-vs-size probe (L2d)
supply that, and the bound values used here (30k, 100k, 250k) are placeholders until L2d measures the
real one.

What this means for the study, inferred from the tables:

1. Only (iii) meets a working-context bound without a lossy reset. Every compaction, rollover
   handoff, or tool-result clear fails the owner's bar, so the (ii) and H4 cost columns are a
   reference, not options.
2. The hypothesis is also cheaper, because its prompt is bounded, not because of anything clever in
   the cache. The cache-friendly layout keeps (iii) from losing that advantage to rebases; it does
   not create it.
3. For today's CLIs, the cheapest single cost change is the cache TTL. Long sessions with an API key
   and idle gaps over five minutes pay mostly for re-writing the same prefix. Claude Code already uses
   the 1 hour TTL for subscription users and accepts `promptCacheTtl` for API keys
   (vendor-documented).

## Working-context bound

<!-- BEGIN GENERATED: ranking -->
Bound 30.0k. No strategy fits: the 42.0k head alone exceeds or nearly fills the bound. Rows are listed for reference, not ranked.

| Rank | Strategy | Max working context | Within bound | Requests over bound | Lossy resets | Cost | Verdict |
|---|---|---|---|---|---|---|---|
| none | (i) plain | 2.06M | no | 0 | 0 | $716.23 | fails: over the bound |
| none | H5 tool-result clearing, vendor defaults | 757.1k | no | 0 | 781 | $308.68 | fails: over the bound |
| none | (ii) compaction at the CLI default (967.0k) | 967.3k | no | 0 | 2 | $382.10 | fails: over the bound |

Bound 100.0k.

| Rank | Strategy | Max working context | Within bound | Requests over bound | Lossy resets | Cost | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | (iii) cvm sized to 100.0k (window 12.8k) | 99.6k | yes | 0 | 0 | $73.83 | meets the bound with no lossy reset; fidelity depends on recall (L2b) |
| 2 | H4 rollover at 75.0k | 88.2k | yes | 0 | 163 | $103.85 | fails: 163 lossy handoffs |
| 3 | (ii) compaction configured at 75.0k | 91.4k | yes | 0 | 205 | $132.44 | fails: 205 compactions |
| 4 | (i) plain | 2.06M | no | 0 | 0 | $716.23 | fails: over the bound |
| 5 | H5 tool-result clearing, vendor defaults | 757.1k | no | 0 | 781 | $308.68 | fails: over the bound |
| 6 | (ii) compaction at the CLI default (967.0k) | 967.3k | no | 0 | 2 | $382.10 | fails: over the bound |

Bound 250.0k.

| Rank | Strategy | Max working context | Within bound | Requests over bound | Lossy resets | Cost | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | (iii) cvm sized to 250.0k (window 62.7k) | 193.4k | yes | 0 | 0 | $107.17 | meets the bound with no lossy reset; fidelity depends on recall (L2b) |
| 2 | H4 rollover at 225.0k | 225.5k | yes | 0 | 12 | $129.73 | fails: 12 lossy handoffs |
| 3 | (ii) compaction configured at 225.0k | 225.9k | yes | 0 | 12 | $128.80 | fails: 12 compactions |
| 4 | (i) plain | 2.06M | no | 0 | 0 | $716.23 | fails: over the bound |
| 5 | H5 tool-result clearing, vendor defaults | 757.1k | no | 0 | 781 | $308.68 | fails: over the bound |
| 6 | (ii) compaction at the CLI default (967.0k) | 967.3k | no | 0 | 2 | $382.10 | fails: over the bound |

Ranked by: within the bound, no lossy reset of any kind, smaller working context, then cost. Any lossy reset fails the owner's bar, which also requires accuracy equal to the full-context baseline. Claude Opus 5.5, 5 min TTL. Lossy resets count compactions, rollover handoffs, and tool-result clears. Quality is not scored here; the bound values are placeholders until the quality-vs-size probe (L2d) measures the real one. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: ranking -->

The bounded (iii) policy checks every request before it is sent. If the request would cross the
bound, the policy acts in this order:

1. It pages whole older turns.
2. If the active turn alone is still too big, it strips all thinking from that turn and stubs the
   turn's earlier tool results, oldest first. Each tool_use keeps its tool_result block; only the
   result content shrinks, so pairing stays valid. Editing an earlier message invalidates kept
   thinking, which is why that thinking is stripped too.
3. As a last resort, it cuts the new tool result to an excerpt.

Any request still over the bound is counted in the "Requests over bound" column. The policy also caps
the stub index at the smaller of 15% of the free space or 8k by merging older stubs into coarser index
entries. Those sizes are inferred; L2b owns the policy. The profile's three tool calls per turn rarely
trigger steps 2 and 3. Tool-heavy turns do, and those steps cost more:

<!-- BEGIN GENERATED: bound_stress -->
| Workload | Bound | Max working context | Requests over bound | Active-turn results stubbed | New results cut to an excerpt | (iii) cost |
|---|---|---|---|---|---|---|
| profile (3 calls per turn) | 100.0k | 99.6k | 0 | 0 | 0 | $73.83 |
| profile (3 calls per turn) | 250.0k | 193.4k | 0 | 0 | 0 | $107.17 |
| 12 calls per turn | 100.0k | 100.0k | 0 | 23 | 0 | $168.51 |
| 12 calls per turn | 250.0k | 232.4k | 0 | 0 | 0 | $233.85 |
| 12 calls per turn, 4x tool output | 100.0k | 100.0k | 0 | 1259 | 0 | $365.23 |
| 12 calls per turn, 4x tool output | 250.0k | 249.5k | 0 | 0 | 0 | $287.62 |

Claude Opus 5.5. When a tool-heavy turn would cross the bound, (iii) first pages whole older turns, then strips all thinking from the active turn and stubs its earlier tool results oldest first (each tool_use keeps its tool_result block; only the content shrinks), and as a last resort cuts the new result to an excerpt. A request still over the bound after that is counted. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: bound_stress -->

What the bound guarantees, inferred from the mechanism: every request stays under it as long as head,
pinned state, stub index, and the active turn's stubbed results fit, which holds on every workload
tested. It does not guarantee fidelity. Stubbed results in the active turn must be recalled if the
model needs them again, and that is L2b's test.

## Assumptions behind the (iii) headline

<!-- BEGIN GENERATED: assumptions -->
- **Billing mode.** The headline uses the 5 minute TTL, Claude Code's default with an API key or usage credits. On a subscription within plan usage the main conversation gets the 1 hour TTL (vendor-documented); there (iii) costs $72.84 against $173.31 for (ii), a ratio of 0.42 instead of 0.23. Which mode the owner's sessions use is not known.
- **The CLI never compacts on its own under (iii).** The proxy bounds the upstream prompt, but the CLI's local history still grows past 967.0k. Whether the CLI then compacts depends on how it counts context, which is brief question 2 (lane 1). Lane 1 PR #9 reports that compaction-forcing settings did not force a pre-send compaction in a `-p` resume (lane 1's measurement, not repeated here). If the CLI does compact at 967.0k of local history, (iii) costs $88.94 instead of $87.95 (2 CLI compactions in 300 turns), and each one is a lossy reset the proxy cannot undo. On the 1 hour TTL the CLI's idle compaction (`idleCompaction`, default on, 200k minimum via `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`) also applies, and the CLI's local history passes 200k early: a subscription session under (iii) takes 7 idle CLI compactions in 300 turns ($72.71), which the proxy cannot prevent. Setting `idleCompaction: false` avoids them.
- **Same head and caching as a direct connection.** (iii) runs behind a custom `ANTHROPIC_BASE_URL`. There Claude Code may load tool definitions upfront instead of through tool search, caching works only if the proxy forwards `cache_control` markers unchanged, and the 1 hour TTL needs the `anthropic-beta` header forwarded (vendor-documented, https://code.claude.com/docs/en/prompt-caching). The model gives (iii) the same 42.0k head and working cache as (ii); the head-size table shows the effect of a larger head. Lane 1's rows are captured behind a custom base URL, so measured heads include this effect.
- **Idle compaction on the 1 hour TTL.** Claude Code can compact an idle conversation shortly before a 1 hour cache expires (`idleCompaction`, vendor-documented). Modeled for (ii) with the documented 200k minimum: $95.37 instead of $173.31 (7 idle compactions, each a lossy reset). Compaction requests themselves get the 5 minute TTL (vendor-documented), which changes nothing here because the next request rewrites the short history anyway.
- **Thinking.** Every Anthropic row keeps prior thinking in context and bills it as input, the vendor-documented default for Opus 5.5, Haiku 5.5, and Fable 5.1; (iii) strips it at rebases and pays 9.7% extra output. If Claude Code drops thinking before sending (not shown; inferred alternative), (i) costs $614.27 and (ii) $358.76 instead (thinking table).
- **Think time.** The TTL findings depend on the inferred think-time mix, in which 35% of gaps exceed 5 minutes. With every gap under a minute, (iii) / (ii) is 0.41 instead of 0.23.
<!-- END GENERATED: assumptions -->

Head size, for the custom base URL case:

<!-- BEGIN GENERATED: head_sweep -->
| Head | (ii) at this head | (iii) at this head | (iii) / (ii), same head | (iii) / (ii) at 42.0k |
|---|---|---|---|---|
| 42.0k | $382.10 | $87.95 | 0.23 | 0.23 |
| 60.0k | $393.96 | $101.44 | 0.26 | 0.27 |
| 80.0k | $387.33 | $116.44 | 0.30 | 0.30 |
| 120.0k | $413.70 | $146.43 | 0.35 | 0.38 |

Claude Opus 5.5. Behind a custom `ANTHROPIC_BASE_URL`, Claude Code can load tool definitions upfront, so (iii) may carry a larger head than a direct (ii) session. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: head_sweep -->

Measured on one OpenRig seat (lane 1, pending review): an 82k head, with its first three calls written
to the cache in full and no cache reads.

<!-- BEGIN GENERATED: seat_head -->
| Head | (ii) compaction | (iii) cvm | (iii) / (ii) | (iii) cold-start premium |
|---|---|---|---|---|
| default throwaway head 42.0k (inferred profile) | $382.10 | $87.95 | 0.23 | $0.00 |
| OpenRig seat head 82.0k | $392.60 | $117.94 | 0.30 | $0.00 |
| OpenRig seat head 82.0k, first 3 calls full cache writes | $393.39 | $118.76 | 0.30 | $0.82 |

Claude Opus 5.5, 300 turns. The 82k seat head and the three uncached first calls are measured on one OpenRig seat by lane 1 (pending lane 1 review; cause of the misses unknown) and replace only the head and the first requests' cache state; the rest of the workload is the inferred profile. Lane 1 reports about 46k for a default throwaway session, close to the 42k inferred head. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: seat_head -->

Thinking blocks. Claude Opus 4.5 and later Opus models, Haiku 5.5, and Fable 5.1 keep every prior
turn's thinking in context and bill it as input (vendor-documented,
https://platform.claude.com/docs/en/build-with-claude/thinking). Thinking on Opus 5.5 and Fable 5.1
cannot be turned off. The model therefore keeps thinking in context for every Anthropic row. Whether
Claude Code strips thinking before sending is not shown anywhere and is left for lane 1 to check.
OpenAI rows keep reasoning out of the resent input, tagged inferred.

Kept thinking stays valid only while the system prompt, tools, and every earlier message are
unchanged. Removing all thinking, or thinking at the start or end, is a valid edit, and so is
server-side context editing (https://platform.claude.com/docs/en/build-with-claude/preserved-thinking,
vendor-documented). Builder-4 measured the same rules on claude-haiku-5-5 through the rig proxy for
PR #7: an append is accepted, and so is a rewrite that strips all thinking; changing the system
prompt or an earlier message while keeping thinking returns 400 in error mode. The account date
(2026-08-31) decides only whether a failing block gets a 400 or passes through; it does not decide
whether thinking is kept.

So (iii) is append-only between epochs, strips all thinking at each rebase, and strips the active
turn's thinking when it must stub that turn's results. Re-thinking costs extra output. The vendor
measured +4.6% (low effort), +9.7% (medium), +10.3% (high), and +67.1% (max) output tokens when thinking
was dropped on every turn after the first. The model charges the medium figure on every (iii)
request. That is an upper bound for a policy that strips only at rebases, and the effort level is
inferred. Server-side `context_management` and byte-for-byte reinsertion are the alternatives PR #7
compares.

<!-- BEGIN GENERATED: thinking -->
| Strategy | Thinking preserved and billed (default) | Max prompt | Thinking dropped by the CLI (inferred alternative) |
|---|---|---|---|
| (i) plain | $716.23 | 2.06M | $614.27 |
| (ii) compaction | $382.10 | 967.3k | $358.76 |
| H4 rollover | $337.07 | 870.9k | $342.94 |
| (iii) cvm | $87.95 | 173.2k | $85.02 |
| (iii), strip overhead 4.6% (low effort) | $87.34 | 173.2k |  |
| (iii), strip overhead 67.1% (max effort) | $94.76 | 173.2k |  |

Claude Opus 5.5. (i), (ii), and H4 carry every prior thinking block. (iii) keeps thinking until the next rebase, which strips all of it (257.4k tokens stripped over 300 turns), and charges 9.7% extra output for re-thinking. The vendor measured +4.6% (low effort) to +67.1% (max) output when thinking is dropped every turn after the first; the effort level is inferred. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
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
| Context growth per turn, mean | 6.7k |
| Full history after the last turn | 2.06M |
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
  window, 244.8k tokens (measured by lane 1, PR #9, pending lane 1 review; it matches the Codex source
  tests). Codex reports `model_context_window` 258,400, which is 0.95 x 272,000, its default
  `effective_context_window_percent`. Because Codex runs a 272K window, the OpenAI (i) rows past 272K
  are hypothetical. The summary request reads the warm prefix and keeps the system-prompt layer
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
| Claude Fable 5.1 | (i) plain | 0.0k | 98.38M | 1,062.60M | 589.9k | $1,524.94 | 2.06M | over the 1.00M window from turn 157 |
| Claude Fable 5.1 | (ii) compaction | 0.0k | 54.25M | 493.75M | 605.9k | $831.80 | 967.3k | 2 compactions |
| Claude Fable 5.1 | H4 rollover | 0.0k | 47.48M | 438.46M | 597.9k | $733.05 | 870.9k | 2 rollovers |
| Claude Fable 5.1 | (iii) cvm | 0.0k | 10.80M | 104.48M | 651.3k | $193.74 | 173.2k | 108 rebases |
| Claude Opus 5.5 | (i) plain | 0.0k | 98.38M | 1,062.60M | 589.9k | $716.23 | 2.06M | over the 1.00M window from turn 157 |
| Claude Opus 5.5 | (ii) compaction | 0.0k | 54.25M | 493.75M | 605.9k | $382.10 | 967.3k | 2 compactions |
| Claude Opus 5.5 | H4 rollover | 0.0k | 47.48M | 438.46M | 597.9k | $337.07 | 870.9k | 2 rollovers |
| Claude Opus 5.5 | (iii) cvm | 0.0k | 10.80M | 104.48M | 651.3k | $87.95 | 173.2k | 108 rebases |
| Claude Haiku 5.5 | (i) plain | 0.0k | 98.38M | 1,062.60M | 589.9k | $115.75 | 2.06M | over the 1.00M window from turn 157 |
| Claude Haiku 5.5 | (ii) compaction | 0.0k | 54.25M | 493.75M | 605.9k | $59.49 | 967.3k | 2 compactions |
| Claude Haiku 5.5 | H4 rollover | 0.0k | 47.48M | 438.46M | 597.9k | $52.28 | 870.9k | 2 rollovers |
| Claude Haiku 5.5 | (iii) cvm | 0.0k | 17.45M | 84.41M | 651.3k | $4.03 | 136.2k | 269 rebases |
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
| Claude Fable 5.1 | $1,524.94 | $831.80 | $733.05 | $193.74 | 0.23 | 0.88 |
| Claude Opus 5.5 | $716.23 | $382.10 | $337.07 | $87.95 | 0.23 | 0.88 |
| Claude Haiku 5.5 | $115.75 | $59.49 | $52.28 | $4.03 | 0.07 | 0.88 |
| gpt-6-astra | $2,463.05 | $272.32 | $257.52 | $205.75 | 0.76 | 0.95 |
| gpt-6-luna | $24.63 | $2.72 | $2.58 | $2.06 | 0.76 | 0.95 |

Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: ratios -->

Per turn and cumulative, for the focus model:

<!-- BEGIN GENERATED: per_turn -->
| Turn | (i) plain prompt | (i) plain cumulative | (ii) compaction prompt | (ii) compaction cumulative | H4 rollover prompt | H4 rollover cumulative | (iii) cvm prompt | (iii) cvm cumulative |
|---|---|---|---|---|---|---|---|---|
| 1 | 46.6k | $0.31 | 46.6k | $0.31 | 46.6k | $0.31 | 49.6k | $0.33 |
| 25 | 176.6k | $10.45 | 176.6k | $10.45 | 176.6k | $10.45 | 89.2k | $7.49 |
| 50 | 373.1k | $30.56 | 373.1k | $30.56 | 373.1k | $30.56 | 81.5k | $15.34 |
| 75 | 525.3k | $58.43 | 525.3k | $58.43 | 525.3k | $58.43 | 130.7k | $22.01 |
| 100 | 701.2k | $97.20 | 701.2k | $97.20 | 701.2k | $97.20 | 118.6k | $29.17 |
| 125 | 850.0k | $152.68 | 850.0k | $152.68 | 850.0k | $152.68 | 116.2k | $36.72 |
| 150 | 960.8k | $203.32 | 960.8k | $203.32 | 152.2k | $174.08 | 112.1k | $42.94 |
| 175 | 1.11M | $252.09 | 209.7k | $211.98 | 302.9k | $185.81 | 131.5k | $48.77 |
| 200 | 1.27M | $339.04 | 372.7k | $234.72 | 465.9k | $215.19 | 95.9k | $56.96 |
| 225 | 1.45M | $407.28 | 547.4k | $259.31 | 640.6k | $244.29 | 107.4k | $63.75 |
| 250 | 1.65M | $507.28 | 751.0k | $301.96 | 844.2k | $292.86 | 100.3k | $72.06 |
| 275 | 1.83M | $635.65 | 926.4k | $364.14 | 211.1k | $322.78 | 94.1k | $81.00 |
| 300 | 2.06M | $716.23 | 258.1k | $382.10 | 443.5k | $337.07 | 148.9k | $87.95 |

Claude Opus 5.5. Prompt is the largest request in the turn. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: per_turn -->

<!-- BEGIN GENERATED: per_turn_buckets -->
| Turn | Strategy | Requests | Uncached in | Cache write | Cache read | Output | Turn cost |
|---|---|---|---|---|---|---|---|
| 1 | (i) plain | 5 | 0.0k | 46.6k | 174.8k | 1.9k | $0.307 |
| 1 | (ii) compaction | 5 | 0.0k | 46.6k | 174.8k | 1.9k | $0.307 |
| 1 | H4 rollover | 5 | 0.0k | 46.6k | 174.8k | 1.9k | $0.307 |
| 1 | (iii) cvm | 5 | 0.0k | 49.6k | 186.8k | 2.1k | $0.328 |
| 50 | (i) plain | 5 | 0.0k | 373.1k | 1.49M | 5.0k | $2.263 |
| 50 | (ii) compaction | 5 | 0.0k | 373.1k | 1.49M | 5.0k | $2.263 |
| 50 | H4 rollover | 5 | 0.0k | 373.1k | 1.49M | 5.0k | $2.263 |
| 50 | (iii) cvm | 5 | 0.0k | 81.5k | 319.3k | 5.5k | $0.581 |
| 100 | (i) plain | 4 | 0.0k | 13.3k | 2.76M | 2.9k | $0.677 |
| 100 | (ii) compaction | 4 | 0.0k | 13.3k | 2.76M | 2.9k | $0.677 |
| 100 | H4 rollover | 4 | 0.0k | 13.3k | 2.76M | 2.9k | $0.677 |
| 100 | (iii) cvm | 4 | 0.0k | 13.3k | 431.9k | 3.2k | $0.216 |
| 150 | (i) plain | 3 | 0.0k | 1.5k | 2.88M | 1.1k | $0.605 |
| 150 | (ii) compaction | 3 | 0.0k | 1.5k | 2.88M | 1.1k | $0.605 |
| 150 | H4 rollover | 3 | 0.0k | 1.5k | 453.4k | 1.1k | $0.120 |
| 150 | (iii) cvm | 3 | 0.0k | 1.5k | 333.4k | 1.2k | $0.098 |
| 200 | (i) plain | 2 | 0.0k | 6.1k | 2.54M | 0.9k | $0.556 |
| 200 | (ii) compaction | 2 | 0.0k | 6.1k | 736.2k | 0.9k | $0.195 |
| 200 | H4 rollover | 2 | 0.0k | 6.1k | 922.5k | 0.9k | $0.233 |
| 200 | (iii) cvm | 2 | 0.0k | 6.1k | 182.5k | 1.0k | $0.086 |
| 250 | (i) plain | 4 | 0.0k | 1.65M | 4.94M | 2.6k | $9.304 |
| 250 | (ii) compaction | 4 | 0.0k | 751.0k | 2.23M | 2.6k | $4.254 |
| 250 | H4 rollover | 4 | 0.0k | 844.2k | 2.51M | 2.6k | $4.776 |
| 250 | (iii) cvm | 4 | 0.0k | 100.3k | 282.7k | 2.8k | $0.615 |
| 300 | (i) plain | 6 | 0.0k | 39.5k | 12.15M | 3.1k | $2.691 |
| 300 | (ii) compaction | 6 | 0.0k | 39.5k | 1.34M | 3.1k | $0.528 |
| 300 | H4 rollover | 6 | 0.0k | 39.5k | 2.45M | 3.1k | $0.750 |
| 300 | (iii) cvm | 6 | 0.0k | 39.5k | 684.1k | 3.4k | $0.403 |

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
| (i) plain | ttl | 106 | 96.32M | $462.35 |
| (ii) compaction | compaction | 2 | 46.0k | $0.22 |
| (ii) compaction | ttl | 106 | 52.14M | $250.26 |
| H4 rollover | rollover | 2 | 40.0k | $0.19 |
| H4 rollover | ttl | 106 | 45.38M | $217.83 |
| (iii) cvm | rebase | 2 | 107.2k | $0.51 |
| (iii) cvm | ttl | 106 | 8.58M | $41.19 |

Claude Opus 5.5, 5 min TTL. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: breaks -->

Layout and policy variants:

<!-- BEGIN GENERATED: variants -->
| (iii) variant | Cost | Cache breaks | Break cost | Pings | Max prompt |
|---|---|---|---|---|---|
| default: pinned head, rebase every 8 turns or on expiry | $87.95 | 108 | $41.71 | 0 | 173.2k |
| no rebase on expiry | $107.09 | 130 | $57.96 | 0 | 197.3k |
| pinned state rebuilt at once on every change | $94.13 | 130 | $47.89 | 0 | 173.2k |
| pinned state at the tail, re-sent every request | $115.29 | 1180 | $69.05 | 0 | 173.3k |
| tool results over 8k paged at once | $86.55 | 108 | $42.48 | 0 | 149.5k |
| 1 h cache TTL | $72.84 | 45 | $17.66 | 0 | 170.3k |
| keepalive pings through idle gaps up to 1 h | $65.72 | 45 | $10.90 | 269 | 170.3k |
| CLI still compacts its local history at the CLI threshold (brief Q2) | $88.94 | 110 | $42.26 | 0 | 173.3k |

Claude Opus 5.5. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: variants -->

TTL and keepalive across all strategies:

<!-- BEGIN GENERATED: ttl -->
| Cache setting | (i) plain | (ii) compaction | H4 rollover | (iii) cvm |
|---|---|---|---|---|
| 5 min TTL (Claude Code default with an API key) | $716.23 | $382.10 | $337.07 | $87.95 |
| 1 h TTL (Claude Code main conversation on a subscription) | $337.42 | $173.31 | $165.04 | $72.84 |
| 1 h TTL with Claude Code idleCompaction ((ii) only) |  | $95.37 |  |  |
| 5 min TTL plus keepalive pings up to 1 h idle | $355.05 | $183.23 | $167.03 | $65.72 |

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
| 8.0k | 6.1 | 5 | 8 |
| 16.0k | 7.2 | 5 | 12 |
| 32.0k | 9.1 | 8 | 12 |
| 64.0k | 11.9 | 12 | 12 |
| 128.0k | 16.1 | 12 | 12 |

Claude Opus 5.5: write $5, read $0.2 per MTok. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: epoch_rule -->

Sensitivity over window size and epoch length:

<!-- BEGIN GENERATED: sweep -->
| Window | E=3 | E=5 | E=8 | E=12 | E=20 | E=40 | best / (ii) |
|---|---|---|---|---|---|---|---|
| 8.0k | $72.69 | $71.42 | **$71.14** | $71.17 | $71.17 | $71.17 | 0.19 |
| 16.0k | $78.89 | $76.98 | $76.23 | **$76.14** | **$76.14** | **$76.14** | 0.20 |
| 32.0k | $92.73 | $89.21 | $87.95 | **$87.65** | **$87.65** | **$87.65** | 0.23 |
| 64.0k | $120.38 | $112.70 | $109.86 | **$109.46** | **$109.46** | **$109.46** | 0.29 |
| 128.0k | $174.85 | $160.27 | $154.78 | **$153.65** | **$153.65** | **$153.65** | 0.40 |

(iii) cost for Claude Opus 5.5, rebase on expiry on. Bold is the cheapest epoch length for each window. (ii) costs $382.10. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: sweep -->

## Where (iii) wins and where it does not

Session length:

<!-- BEGIN GENERATED: by_length -->
| Turns | (i) plain | (ii) compaction | H4 rollover | (iii) cvm | (iii) / (ii) | Compactions |
|---|---|---|---|---|---|---|
| 1 | $0.31 | $0.31 | $0.31 | $0.33 | 1.07 | 0 |
| 5 | $1.62 | $1.62 | $1.62 | $1.65 | 1.02 | 0 |
| 10 | $2.93 | $2.93 | $2.93 | $2.94 | 1.00 | 0 |
| 25 | $10.45 | $10.45 | $10.45 | $7.49 | 0.72 | 0 |
| 50 | $30.56 | $30.56 | $30.56 | $15.34 | 0.50 | 0 |
| 100 | $97.20 | $97.20 | $97.20 | $29.17 | 0.30 | 0 |
| 200 | $339.04 | $234.72 | $215.19 | $56.96 | 0.24 | 1 |
| 300 | $716.23 | $382.10 | $337.07 | $87.95 | 0.23 | 2 |
| 600 | $2,804.15 | $726.33 | $691.49 | $186.43 | 0.26 | 4 |
| 1000 | $7,831.55 | $1,247.79 | $1,165.69 | $340.91 | 0.27 | 7 |

Claude Opus 5.5. Basis: modeled on the `synthetic-realistic` profile (inferred), seed 20261015.
<!-- END GENERATED: by_length -->

Tool output size:

<!-- BEGIN GENERATED: by_tool_size -->
| Tool output median | Growth per turn | (i) plain | (ii) compaction | H4 rollover | (iii) cvm | (iii) / (ii) |
|---|---|---|---|---|---|---|
| 0.25x | 3.4k | $399.99 | $362.77 | $302.15 | $82.43 | 0.23 |
| 0.5x | 4.6k | $509.75 | $324.38 | $311.16 | $84.45 | 0.26 |
| 1x (profile) | 6.7k | $716.23 | $382.10 | $337.07 | $87.95 | 0.23 |
| 2x | 10.6k | $1,105.10 | $378.07 | $352.76 | $93.48 | 0.25 |
| 4x | 17.0k | $1,782.84 | $387.25 | $366.18 | $102.94 | 0.27 |

Claude Opus 5.5. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: by_tool_size -->

Think time:

<!-- BEGIN GENERATED: by_think -->
| Think time | Gaps over 5 min | (i) plain | (ii) compaction | H4 rollover | (iii) cvm | (iii) / (ii) |
|---|---|---|---|---|---|---|
| always under 1 min | 0% | $266.40 | $135.59 | $119.90 | $55.81 | 0.41 |
| profile mix | 35% | $716.23 | $382.10 | $337.07 | $87.95 | 0.23 |
| mostly 10 to 40 min | 100% | $1,763.29 | $830.08 | $725.83 | $159.54 | 0.19 |

Claude Opus 5.5, 5 min TTL. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: by_think -->

Compaction and rollover threshold. Lower thresholds make (ii) and H4 much cheaper, at the price of
more lossy resets:

<!-- BEGIN GENERATED: reset_sweep -->
| Reset threshold | (ii) compaction | Compactions | H4 rollover | Rollovers | (iii) / (ii) |
|---|---|---|---|---|---|
| 100.0k | $100.79 | 61 | $91.99 | 56 | 0.87 |
| 200.0k | $124.35 | 14 | $125.24 | 14 | 0.71 |
| 400.0k | $183.90 | 5 | $180.20 | 5 | 0.48 |
| 967.0k | $382.10 | 2 | $380.96 | 2 | 0.23 |

Claude Opus 5.5. Claude Code's `/autocompact` and `--autocompact` accept 100K to 1M; `CLAUDE_CODE_AUTO_COMPACT_WINDOW` takes a token count capped at the model's window (vendor-documented). Every compaction is a failure under the owner's bar; the table shows cost only. (iii) costs $87.95. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: reset_sweep -->

Whether the compaction request reads the cache:

<!-- BEGIN GENERATED: compaction_cache -->
| Threshold | (ii) summary request | Compactions | (ii) cost | (iii) / (ii) |
|---|---|---|---|---|
| 967.0k | reads the warm prefix (vendor-documented) | 2 | $382.10 | 0.23 |
| 967.0k | full miss (as after an idle gap) | 2 | $391.37 | 0.22 |
| 200.0k | reads the warm prefix (vendor-documented) | 14 | $124.35 | 0.71 |
| 200.0k | full miss (as after an idle gap) | 14 | $137.65 | 0.64 |

Claude Opus 5.5. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
<!-- END GENERATED: compaction_cache -->

Recall load. If the model recalls often or recalls large pages, (iii)'s advantage shrinks but holds
on this profile:

<!-- BEGIN GENERATED: by_recall -->
| Recalls per turn | Recall size | Recalls | (iii) cost | (iii) / (ii) |
|---|---|---|---|---|
| 0 | 1.5k | 0 | $86.88 | 0.23 |
| 0.1 | 1.5k | 32 | $87.95 | 0.23 |
| 0.5 | 1.5k | 143 | $91.65 | 0.24 |
| 1 | 1.5k | 300 | $96.50 | 0.25 |
| 0.5 | 8.0k | 143 | $96.79 | 0.25 |
| 2 | 8.0k | 300 | $108.34 | 0.28 |

Claude Opus 5.5. Each recall is one extra upstream round trip that the proxy answers itself. (ii) costs $382.10. Basis: modeled on the `synthetic-realistic` profile (inferred), 300 turns, seed 20261015.
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
| trigger 100k, keep 3, clear_at_least 0 (vendor defaults) | $308.68 | 781 | 757.1k | 0.43 | 0.28 |
| trigger 100k, keep 3, clear_at_least 20k | $299.90 | 55 | 775.4k | 0.42 | 0.29 |
| trigger 100k, keep 3, clear_at_least 50k | $308.67 | 24 | 797.8k | 0.43 | 0.28 |
| trigger 200k, keep 3, clear_at_least 50k | $311.15 | 23 | 797.8k | 0.43 | 0.28 |
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

- The bounded (iii) policy is enforced per request at block granularity. Stubbed active-turn results
  and excerpts must be recalled if needed again, which costs fidelity this model does not score.
- The re-thinking charge is one vendor figure applied uniformly; the real overhead depends on effort
  level and on how often thinking is stripped.
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
